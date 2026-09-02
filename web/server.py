"""Web Shell：FastAPI 服务，复用现有 LangGraph 全链路（CLI 的孪生客户端）。

架构原则：
  - 图/checkpoint/provider 全量复用 devflow.orchestrator / cli 辅助
  - 事件协议与 CLI 共用 devflow.events.events_from_astream（单一事件源）
  - SSE 下行（POST + fetch 流式读取），POST 上行（消息 / 门禁决策）

启动：
  export LLM_API_KEY=... LLM_MODEL=deepseek-v4-flash TEST_GEN_PROVIDER=llm
  uvicorn web.server:app --port 8000
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import HumanMessage
from langgraph.types import Command
from pydantic import BaseModel

from devflow.cli import _config, _list_threads, _thread_exists, apply_assignments
from devflow.doc_reader import read_doc
from devflow.events import events_from_astream
from devflow.orchestrator import build_graph_with_providers, initial_state

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="DevFlow Web Shell", docs_url=None, redoc_url=None)


# ═══════════════════════════════════════════════════════════════════
# 请求体
# ═══════════════════════════════════════════════════════════════════


class CreateSession(BaseModel):
    thread_id: str | None = None
    text: str = ""            # 初始需求文本（创建后由前端走 /messages 喂入）
    from_doc: str | None = None  # 文本格式文档路径（.txt/.md）
    set_fields: list[str] = []   # 等价 --set：["project_root=/workspace", ...]


class SendMessage(BaseModel):
    text: str


class GateDecision(BaseModel):
    decision: str  # approve / reject


# ═══════════════════════════════════════════════════════════════════
# 会话
# ═══════════════════════════════════════════════════════════════════


@app.post("/api/sessions")
def create_session(body: CreateSession) -> dict[str, Any]:
    tid = body.thread_id or f"w-{uuid.uuid4().hex[:8]}"
    if _thread_exists(tid):
        raise HTTPException(409, f"会话已存在: {tid}")
    graph = build_graph_with_providers()
    state = initial_state()
    if body.set_fields:
        state["requirement"] = apply_assignments(state["requirement"], body.set_fields)
    next(graph.stream(state, _config(thread_id=tid)))  # 触发 initial checkpoint
    return {"thread_id": tid, "stage": "clarify"}


@app.get("/api/sessions")
def list_sessions() -> list[dict[str, Any]]:
    return _list_threads()


@app.get("/api/sessions/{tid}")
def session_state(tid: str) -> dict[str, Any]:
    graph = build_graph_with_providers()
    try:
        snap = graph.get_state(_config(thread_id=tid))
    except Exception as e:
        raise HTTPException(404, f"会话不存在或读取失败: {e}")
    return _serialize_state(snap.values)


def _serialize_state(values: Any) -> dict[str, Any]:
    """checkpoint values → JSON-safe dict（messages 转 {type, content}）。"""
    if not isinstance(values, dict):
        return {"raw": str(values)}
    out: dict[str, Any] = {}
    for k, v in values.items():
        if k == "messages" and isinstance(v, list):
            out[k] = [
                {"type": getattr(m, "type", "message"), "content": str(getattr(m, "content", ""))}
                for m in v
            ]
        elif isinstance(v, dict):
            out[k] = _serialize_state(v)
        elif isinstance(v, list):
            out[k] = [_serialize_state(i) if isinstance(i, dict) else i for i in v]
        else:
            out[k] = v
    return out


# ═══════════════════════════════════════════════════════════════════
# SSE 流（消息 / 门禁决策）
# ═══════════════════════════════════════════════════════════════════


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"


@app.post("/api/sessions/{tid}/messages")
async def send_message(tid: str, body: SendMessage) -> StreamingResponse:
    if not _thread_exists(tid):
        raise HTTPException(404, f"会话不存在: {tid}")
    graph = build_graph_with_providers()
    input_msg = {"messages": [HumanMessage(content=body.text)]}

    async def gen():
        try:
            async for event in events_from_astream(
                graph.astream(input_msg, _config(thread_id=tid), stream_mode="updates")
            ):
                yield _sse(event)
        finally:
            yield _sse({"type": "stream_end", "stage": "paused"})

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/api/sessions/{tid}/gates")
async def decide_gate(tid: str, body: GateDecision) -> StreamingResponse:
    if not _thread_exists(tid):
        raise HTTPException(404, f"会话不存在: {tid}")
    graph = build_graph_with_providers()

    async def gen():
        try:
            async for event in events_from_astream(
                graph.astream(Command(resume=body.decision), _config(thread_id=tid), stream_mode="updates")
            ):
                yield _sse(event)
        finally:
            yield _sse({"type": "stream_end", "stage": "paused"})

    return StreamingResponse(gen(), media_type="text/event-stream")


# ═══════════════════════════════════════════════════════════════════
# 静态资源
# ═══════════════════════════════════════════════════════════════════


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")