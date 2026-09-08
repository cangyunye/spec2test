"""Web Shell：FastAPI 服务，复用现有 LangGraph 全链路（CLI 的孪生客户端）。

架构原则：
  - 图/checkpoint/provider 全量复用 devflow.orchestrator / cli 辅助
  - 事件协议与 CLI 共用 devflow.events.events_from_stream（单一事件源）
  - SSE 下行（GET + EventSource），POST 上行（消息 / 门禁决策 / 文档解析）
  - 同一线程同一时刻只允许一个 SSE 流（per-tid 锁），防止 checkpoint 并发写

启动：
  cp .env.example .env   # 填 key；不填则 Mock 兜底模式
  uvicorn web.server:app --port 8000
"""
from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import HumanMessage
from langgraph.types import Command
from pydantic import BaseModel

from devflow.cli import _config, _thread_exists, apply_assignments
from devflow.config import settings
from devflow.doc_reader import read_doc
from devflow.events import events_from_stream
from devflow.orchestrator import _get_sqlite_conn, build_graph_with_providers, initial_state

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="DevFlow Web Shell", docs_url=None, redoc_url=None)

# per-thread 流锁：同一会话串行推进，避免并发 stream 写同一 checkpoint
_tid_locks: dict[str, asyncio.Lock] = {}


def _tid_lock(tid: str) -> asyncio.Lock:
    if tid not in _tid_locks:
        _tid_locks[tid] = asyncio.Lock()
    return _tid_locks[tid]


# ═══════════════════════════════════════════════════════════════════
# 请求体
# ═══════════════════════════════════════════════════════════════════


class CreateSession(BaseModel):
    thread_id: str | None = None
    set_fields: list[str] = []   # 等价 --set：["project_root=/workspace", ...]


class SendMessage(BaseModel):
    text: str


class GateDecision(BaseModel):
    decision: str                # approve / reject
    comment: str | None = None   # reject 时的修改意见（可选）


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
    # 只写初始 checkpoint，不跑任何节点（省一次空澄清 LLM 调用）；
    # 首条用户消息经由 /stream 推进 compress → clarify_extract
    graph.update_state(_config(thread_id=tid), state)
    return {"thread_id": tid, "stage": "clarify"}


@app.get("/api/sessions")
def list_sessions() -> list[dict[str, Any]]:
    """会话列表：thread_id + 标题（首条用户消息）+ 阶段 + 是否有图。按最近活动排序。"""
    conn = _get_sqlite_conn()
    try:
        rows = conn.execute(
            "SELECT thread_id, MAX(checkpoint_id) FROM checkpoints GROUP BY thread_id"
        ).fetchall()
    except Exception:
        rows = []
    # checkpoint_id 是时间有序 UUID，字符串序即时间序
    rows.sort(key=lambda r: str(r[1] or ""), reverse=True)
    graph = build_graph_with_providers()
    out: list[dict[str, Any]] = []
    for tid, _ in rows[:50]:
        entry: dict[str, Any] = {
            "thread_id": tid, "title": "", "stage": "clarify", "has_graph": False,
        }
        try:
            vals = graph.get_state(_config(thread_id=tid)).values or {}
            entry["stage"] = vals.get("current_stage", "clarify")
            entry["has_graph"] = bool(vals.get("logic_graph"))
            for m in vals.get("messages") or []:
                mtype = getattr(m, "type", None) or (m.get("type") if isinstance(m, dict) else "")
                if mtype == "human":
                    content = str(getattr(m, "content", "") or (m.get("content") if isinstance(m, dict) else ""))
                    entry["title"] = content.strip().splitlines()[0][:60] if content.strip() else ""
                    break
        except Exception:
            pass
        out.append(entry)
    return out


@app.get("/api/sessions/{tid}")
def session_state(tid: str) -> dict[str, Any]:
    """会话快照：values + next（挂起节点，用于前端恢复门禁 UI）。"""
    if not _thread_exists(tid):
        raise HTTPException(404, f"会话不存在: {tid}")
    graph = build_graph_with_providers()
    try:
        snap = graph.get_state(_config(thread_id=tid))
    except Exception as e:
        raise HTTPException(404, f"会话不存在或读取失败: {e}")
    return {
        "thread_id": tid,
        "stage": (snap.values or {}).get("current_stage", "clarify"),
        "next": list(snap.next or []),
        "values": _serialize_state(snap.values),
    }


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


@app.delete("/api/sessions/{tid}")
def delete_session(tid: str) -> dict[str, Any]:
    if not _thread_exists(tid):
        raise HTTPException(404, f"会话不存在: {tid}")
    conn = _get_sqlite_conn()
    for table in ("checkpoints", "writes"):
        try:
            conn.execute(f"DELETE FROM {table} WHERE thread_id = ?", (tid,))
        except Exception:
            pass  # 表不存在则跳过
    conn.commit()
    return {"deleted": tid}


# ═══════════════════════════════════════════════════════════════════
# 文档解析（Web 端 from-doc 等价能力）
# ═══════════════════════════════════════════════════════════════════


@app.post("/api/doc/extract")
async def extract_doc(file: UploadFile = File(...)) -> dict[str, Any]:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in (".docx", ".txt", ".md"):
        raise HTTPException(415, f"不支持的格式 {suffix}（仅 .docx / .txt / .md）")
    data = await file.read()
    if len(data) > 5 * 1024 * 1024:
        raise HTTPException(413, "文件超过 5MB 上限")
    import tempfile

    fd, tmp_path = tempfile.mkstemp(suffix=suffix)
    try:
        with __import__("os").fdopen(fd, "wb") as f:
            f.write(data)
        text = read_doc(tmp_path)
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    return {"name": file.filename, "chars": len(text), "text": text}


# ═══════════════════════════════════════════════════════════════════
# 配置自检
# ═══════════════════════════════════════════════════════════════════


def _mask(key: str) -> str:
    # 占位/示例 key 一律视为未配置
    if not key or key.startswith(("sk-your", "sk-dummy")):
        return ""
    return f"{key[:6]}…{key[-4:]}" if len(key) > 14 else "已配置"


@app.get("/api/health")
def health() -> dict[str, Any]:
    providers = [
        {
            "name": p.get("name", ""),
            "model": p.get("model", ""),
            "base_url": str(p.get("base_url", "")),
            "key": _mask(str(p.get("api_key", ""))),
        }
        for p in settings.LLM_PROVIDERS
    ]
    db_path = settings.CHECKPOINT_SQLITE_PATH
    return {
        "llm": {
            "providers": providers,
            "mock_fallback": settings.LLM_USE_MOCK_FALLBACK,
            "mode": "providers" if providers else "legacy",
        },
        "pipeline": {
            "test_gen": __import__("os").getenv("TEST_GEN_PROVIDER", "llm"),
            "code_search": __import__("os").getenv("CODE_SEARCH_PROVIDER", "mock"),
            "code_edit": __import__("os").getenv("CODE_EDIT_PROVIDER", "mock"),
        },
        "checkpoint_db": {"path": str(db_path), "exists": db_path.exists()},
    }


# ═══════════════════════════════════════════════════════════════════
# 导出（CLI export 的 Web 等价）
# ═══════════════════════════════════════════════════════════════════


@app.get("/api/sessions/{tid}/export")
def export_session(tid: str, format: str = "md") -> Any:
    if not _thread_exists(tid):
        raise HTTPException(404, f"会话不存在: {tid}")
    graph = build_graph_with_providers()
    vals = graph.get_state(_config(thread_id=tid)).values or {}
    if format == "json":
        return _serialize_state(vals)  # FastAPI 自动 JSON 化
    md = _build_export_md(vals)
    return _download(md, filename=f"devflow-{tid}.md", media_type="text/markdown")


def _download(text: str, *, filename: str, media_type: str) -> StreamingResponse:
    from urllib.parse import quote

    return StreamingResponse(
        iter([text.encode("utf-8")]),
        media_type=f"{media_type}; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


def _build_export_md(vals: dict[str, Any]) -> str:
    req = vals.get("requirement") or {}
    graph = vals.get("logic_graph") or {}
    changes = vals.get("code_changes") or []
    report = vals.get("test_report") or {}
    lines: list[str] = [f"# DevFlow 产物导出 · {vals.get('current_stage', '?')}", ""]

    lines += ["## 需求清单", ""]
    for k, v in req.items():
        if v in (None, "", [], {}):
            continue
        lines.append(f"- **{k}**: `{json.dumps(v, ensure_ascii=False)}`")

    if graph:
        lines += ["", "## 逻辑图", "", f"graph_id: `{graph.get('graph_id', '?')}`", "", "```mermaid",
                  graph.get("mermaid_source", ""), "```"]
    if changes:
        lines += ["", "## 代码变更", ""]
        for ch in changes:
            lines.append(f"### {ch.get('file_path')} [{ch.get('action')}]")
            lines += ["```diff", ch.get("diff", ""), "```"]
    if report:
        run = report.get("run") or {}
        cases = report.get("test_cases") or []
        lines += ["", "## 测试用例文档", ""]

        # ── 总文档：概述 + 公共口径（方法论来自 doc-based/functional testcase-generator skills）──
        if report.get("overview"):
            lines += [report["overview"], ""]
        lines += [
            f"用例总数：{len(cases)}"
            f"（passed={run.get('passed', 0)} failed={run.get('failed', 0)}）",
            "- 优先级口径：P0 核心路径与关键校验 · P1 边界与重要异常 · P2 次要异常与体验",
            "- 类型口径：正向 / 反向 / 边界值 / 等价类 / 状态流转 / 场景法 / 性能 / 安全",
            "",
        ]

        # ── 分文档：按所属模块分组的用例清单 ──
        groups: dict[str, list[dict[str, Any]]] = {}
        for c in cases:
            groups.setdefault(str(c.get("target") or "通用"), []).append(c)
        for gi, (mod, mod_cases) in enumerate(groups.items(), start=1):
            lines += [f"### 2.{gi} {mod}", "",
                      "| 标识 | 层级 | 优先级 | 类型 | 标题 | 前置 | 步骤 | 预期 | 依据 |",
                      "|---|---|---|---|---|---|---|---|---|"]
            for c in mod_cases:
                cells = [str(c.get(k, "") or "").replace("|", "\\|").replace("\n", " ")
                         for k in ("case_id", "tier", "priority", "case_type", "title",
                                   "precondition", "steps", "expected", "rationale")]
                lines.append("| " + " | ".join(cells) + " |")
            lines.append("")

        # ── 质量自检结论 ──
        checks = report.get("self_check") or []
        if checks:
            lines += ["### 质量自检", ""]
            lines += [f"- {s}" for s in checks]
            lines.append("")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
# SSE 流（消息 / 门禁决策）
# ═══════════════════════════════════════════════════════════════════


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"


async def _sse_from_sync_stream(graph: Any, input_msg: dict[str, Any], tid: str):
    """同步 stream（同步 SqliteSaver 不支持 astream）→ SSE 事件流。

    to_thread 跑生成器，asyncio.Queue 桥接回事件循环，零新依赖。
    双流模式 ["updates", "messages"]：节点完成事件 + LLM token 增量。
    """
    lock = _tid_lock(tid)
    if lock.locked():
        yield _sse({"type": "error", "error": "该会话正在推进中，请稍候"})
        yield _sse({"type": "stream_end", "stage": "busy"})
        return
    async with lock:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        def run() -> None:
            try:
                for event in events_from_stream(
                    graph.stream(
                        input_msg, _config(thread_id=tid),
                        stream_mode=["updates", "messages"],
                    )
                ):
                    q.put_nowait(event)
            except Exception as e:
                q.put_nowait({"type": "error", "error": str(e)})
            finally:
                q.put_nowait({"type": "stream_end", "stage": "paused"})

        task = asyncio.create_task(asyncio.to_thread(run))
        try:
            while True:
                event = await asyncio.wait_for(q.get(), timeout=600)
                yield _sse(event)
                if event.get("type") == "stream_end":
                    break
        except asyncio.CancelledError:
            raise  # 客户端断开：让 Starlette 取消，to_thread 任务继续无害运行
        finally:
            if not task.done():
                await asyncio.shield(task)


@app.post("/api/sessions/{tid}/messages")
async def send_message(tid: str, body: SendMessage) -> StreamingResponse:
    if not _thread_exists(tid):
        raise HTTPException(404, f"会话不存在: {tid}")
    graph = build_graph_with_providers()
    return StreamingResponse(
        _sse_from_sync_stream(graph, {"messages": [HumanMessage(content=body.text)]}, tid),
        media_type="text/event-stream",
    )


@app.get("/api/sessions/{tid}/stream")
async def stream_sse(
    tid: str, op: str, text: str = "", decision: str = "", comment: str = ""
) -> StreamingResponse:
    """浏览器 EventSource 用：GET + query 参数。

    op=message&text=<用户消息> ；op=gate&decision=approve|reject[&comment=<意见>] 。
    """
    if not _thread_exists(tid):
        raise HTTPException(404, f"会话不存在: {tid}")
    graph = build_graph_with_providers()
    if op == "gate":
        input_msg: Any = (
            Command(resume={"decision": decision, "comment": comment})
            if comment else Command(resume=decision)
        )
    else:
        input_msg = {"messages": [HumanMessage(content=text)]}
    return StreamingResponse(
        _sse_from_sync_stream(graph, input_msg, tid), media_type="text/event-stream"
    )


@app.post("/api/sessions/{tid}/gates")
async def decide_gate(tid: str, body: GateDecision) -> StreamingResponse:
    if not _thread_exists(tid):
        raise HTTPException(404, f"会话不存在: {tid}")
    graph = build_graph_with_providers()
    input_msg: Any = (
        Command(resume={"decision": body.decision, "comment": body.comment or ""})
        if body.comment else Command(resume=body.decision)
    )
    return StreamingResponse(
        _sse_from_sync_stream(graph, input_msg, tid), media_type="text/event-stream"
    )


# ═══════════════════════════════════════════════════════════════════
# 静态资源
# ═══════════════════════════════════════════════════════════════════


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
