"""Web Shell：FastAPI 服务，复用现有 LangGraph 全链路（CLI 的孪生客户端）。

架构原则：
  - 图/checkpoint/provider 全量复用 devflow.orchestrator / cli 辅助
  - 事件协议与 CLI 共用 devflow.events.events_from_stream（单一事件源）
  - 推进请求（POST message/gate/revert）立即返回，图在后台线程跑完；
    进度经 per-tid RunBus 缓冲（seq 单调递增），GET /events 订阅：
    先回放缓冲（after=游标，断线重连零丢失），再实时 tail 到 stream_end。
    客户端断开/换会话/关浏览器都不影响运行，回来按游标续看。
  - 同一会话同一时刻只允许一个 run（per-tid 锁），防 checkpoint 并发写
  - 进行中的 run 落 data/runs/<tid>.json 标记：服务重启时扫描，对停在
    非门禁节点（半途）的会话自动 stream(None) 续跑

启动：
  cp .env.example .env   # 填 key；不填则 Mock 兜底模式
  uvicorn web.server:app --port 8000
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import HumanMessage
from langgraph.types import Command
from pydantic import BaseModel

from devflow.cli import _config, _thread_exists, apply_assignments
from devflow.checklist.library import validate_rel_dir
from devflow.config import settings
from devflow.doc_reader import read_doc
from devflow.doctor import first_run_notice
from devflow.events import events_from_stream
from devflow.orchestrator import _get_sqlite_conn, build_graph_with_providers, initial_state
from devflow.timetravel import list_steps, revert as revert_thread

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="DevFlow Web Shell", docs_url=None, redoc_url=None)

# per-thread 流锁：同一会话串行推进，避免并发 stream 写同一 checkpoint
_tid_locks: dict[str, asyncio.Lock] = {}


def _tid_lock(tid: str) -> asyncio.Lock:
    if tid not in _tid_locks:
        _tid_locks[tid] = asyncio.Lock()
    return _tid_locks[tid]


# ═══════════════════════════════════════════════════════════════════
# RunBus：per-tid 运行事件总线（缓冲重放 + 多客户端实时订阅）
# ═══════════════════════════════════════════════════════════════════


@dataclass
class RunBus:
    seq: int = 0                                    # 会话内单调递增游标（跨 run 不清零）
    buffer: deque = field(default_factory=deque)    # [(seq, event)]，本 run 全部事件
    subscribers: set = field(default_factory=set)   # 实时订阅者的 asyncio.Queue
    running: bool = False
    task: asyncio.Task | None = None


_buses: dict[str, RunBus] = {}


def _bus(tid: str) -> RunBus:
    if tid not in _buses:
        _buses[tid] = RunBus()
    return _buses[tid]


def _session_running(tid: str) -> bool:
    return _bus(tid).running or _tid_lock(tid).locked()


# ── run 落盘标记：服务重启时扫描续跑 ────────────────────────────────


def _runs_dir() -> Path:
    return Path(settings.CHECKPOINT_SQLITE_PATH).parent / "runs"


def _mark_run(tid: str) -> None:
    try:
        d = _runs_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{tid}.json").write_text(
            json.dumps({"started_at": time.time(), "pid": os.getpid()}), encoding="utf-8"
        )
    except OSError:
        pass  # 标记尽力而为；失败只影响重启续跑


def _clear_run_mark(tid: str) -> None:
    try:
        (_runs_dir() / f"{tid}.json").unlink(missing_ok=True)
    except OSError:
        pass


# interrupt 门禁节点：停在这些位置 = 等用户决策，重启后不自动跑
_GATE_NODES = {"requirement_review", "graph_type_select", "graph_review",
               "checklist_route_gate", "review"}


def _gate_pending(graph: Any, tid: str) -> bool:
    """会话是否停在门禁 interrupt 上（有挂起 task 且带 interrupts）。

    不能只看 snap.next 非空——新建会话 update_state 后 next 也非空。
    此时从消息通道再发输入会从 START 重跑一轮、脱离挂起点的恢复上下文，
    必须先由门禁通道（Command(resume)）完成决策。
    """
    try:
        snap = graph.get_state(_config(thread_id=tid))
    except Exception:  # noqa: BLE001 - 状态读取失败不拦消息，交给正常流程暴露问题
        return False
    for task in getattr(snap, "tasks", ()) or ():
        if getattr(task, "interrupts", None):
            return True
    return False


async def start_run(tid: str, input_msg: Any) -> dict[str, Any]:
    """把一次图推进放到后台执行，事件进 RunBus；立即返回。

    同一会话已有 run 在跑 → accepted=False。消费进度走 GET /events 订阅。
    """
    bus = _bus(tid)
    lock = _tid_lock(tid)
    if bus.running or lock.locked():
        return {"accepted": False, "reason": "该会话正在推进中"}
    await lock.acquire()
    bus.running = True
    bus.buffer.clear()  # seq 继续递增，旧游标依然有效
    graph = build_graph_with_providers()
    bus.task = asyncio.create_task(_run_graph(graph, tid, input_msg, bus, lock))
    _mark_run(tid)
    return {"accepted": True, "last_seq": bus.seq}


async def _run_graph(graph: Any, tid: str, input_msg: Any, bus: RunBus, lock: asyncio.Lock) -> None:
    """后台推进图：同步 stream → to_thread → 事件写总线并广播。客户端断开无感。"""
    q: asyncio.Queue = asyncio.Queue()

    def run() -> None:
        try:
            for event in events_from_stream(
                graph.stream(input_msg, _config(thread_id=tid),
                             stream_mode=["updates", "messages", "custom"])
            ):
                q.put_nowait(event)
        except Exception as e:  # noqa: BLE001 — 事件流内任何异常都要落到前端
            q.put_nowait({"type": "error", "error": str(e)})
        finally:
            q.put_nowait({"type": "stream_end", "stage": "paused"})

    thread_task = asyncio.create_task(asyncio.to_thread(run))
    try:
        while True:
            event = await asyncio.wait_for(q.get(), timeout=600)
            bus.seq += 1
            bus.buffer.append((bus.seq, event))
            for sub in list(bus.subscribers):
                sub.put_nowait((bus.seq, event))
            if event.get("type") == "stream_end":
                break
    except asyncio.TimeoutError:
        event = {"type": "error", "error": "推进超时（单事件等待超过 600s），已结束本次运行"}
        bus.seq += 1
        bus.buffer.append((bus.seq, event))
        for sub in list(bus.subscribers):
            sub.put_nowait((bus.seq, event))
    except Exception as e:  # noqa: BLE001
        event = {"type": "error", "error": str(e)}
        bus.seq += 1
        bus.buffer.append((bus.seq, event))
        for sub in list(bus.subscribers):
            sub.put_nowait((bus.seq, event))
    finally:
        bus.running = False
        bus.task = None
        _clear_run_mark(tid)
        if not thread_task.done():
            await asyncio.shield(thread_task)  # 跑完落盘（断线/超时后的收尾与旧版一致）
        lock.release()


async def _events_gen(bus: RunBus, after: int):
    """SSE 生成器：先回放缓冲 seq>after，再实时 tail；run 结束（stream_end）后关闭。

    空闲 15s 发 SSE 注释行保活；首帧 stream_meta 让客户端对齐游标与运行态。
    """
    q: asyncio.Queue = asyncio.Queue()
    bus.subscribers.add(q)  # 先订阅再回放：间隙事件进 q，按 seq 去重，零丢失
    seen = after
    try:
        yield _sse({"type": "stream_meta", "seq": bus.seq, "running": bus.running})
        for seq, ev in list(bus.buffer):
            if seq > after:
                seen = seq
                yield _sse({**ev, "seq": seq})
        if not bus.running and bus.buffer and bus.buffer[-1][1].get("type") == "stream_end":
            return  # 上一轮已完整结束，回放完即收
        while True:
            try:
                seq, ev = await asyncio.wait_for(q.get(), timeout=15)
            except asyncio.TimeoutError:
                yield ": ping\n\n"
                continue
            if seq <= seen:
                continue
            seen = seq
            yield _sse({**ev, "seq": seq})
            if ev.get("type") == "stream_end":
                return
    finally:
        bus.subscribers.discard(q)


# ═══════════════════════════════════════════════════════════════════
# 请求体
# ═══════════════════════════════════════════════════════════════════


class CreateSession(BaseModel):
    thread_id: str | None = None
    set_fields: list[str] = []   # 等价 --set：["project_root=/workspace", ...]


class SendMessage(BaseModel):
    text: str


class GateDecision(BaseModel):
    decision: str                # approve / reject / confirm / skip
    comment: str | None = None   # reject 时的修改意见（可选）
    selected: list[str] | None = None  # checklist_route 门禁：确认加载的业务路径
    fields: dict[str, Any] | None = None  # requirement_review 门禁：就地修改的字段（点路径 → 值）


class ActiveProviderBody(BaseModel):
    name: str                    # LLM_PROVIDERS_JSON 里的条目名（模型池展开后含 :model 后缀）
    model: str | None = None     # 可选；条目本身已含具体模型时可不传


class CheckProviderBody(BaseModel):
    name: str | None = None      # 不传 = 检测全部


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
    # 首条用户消息经由 POST /messages 推进 compress → clarify_extract
    graph.update_state(_config(thread_id=tid), state)
    return {"thread_id": tid, "stage": "clarify"}


def _session_title(vals: dict[str, Any]) -> str:
    """会话展示名的确定性优先级：
    state.session_title（澄清阶段 LLM 起名）→ project_context 截断 → 首条用户消息（调用方兜底）。
    """
    t = str(vals.get("session_title") or "").strip()
    if t:
        return t
    req = vals.get("requirement") or {}
    ctx = str(req.get("project_context") or "").strip()
    if ctx:
        return ctx[:30]
    return ""


@app.get("/api/sessions")
def list_sessions() -> list[dict[str, Any]]:
    """会话列表：thread_id + 标题（需求名 → 背景摘要 → 首条用户消息）+ 阶段 + 是否有图。按最近活动排序。"""
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
            "running": _session_running(tid), "last_seq": _bus(tid).seq,
        }
        try:
            vals = graph.get_state(_config(thread_id=tid)).values or {}
            entry["stage"] = vals.get("current_stage", "clarify")
            entry["has_graph"] = bool(vals.get("logic_graph"))
            entry["title"] = _session_title(vals)
            if not entry["title"]:
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
    bus = _bus(tid)
    return {
        "thread_id": tid,
        "stage": (snap.values or {}).get("current_stage", "clarify"),
        "next": list(snap.next or []),
        "values": _serialize_state(snap.values),
        "running": _session_running(tid),
        "last_seq": bus.seq,
    }


@app.get("/api/sessions/{tid}/history")
def session_history(tid: str) -> dict[str, Any]:
    """回退锚点清单（新→旧）：每条 = 一次节点落盘的检查点。

    前端用它给对话流里的步骤分隔线/产物卡挂「从此步重来」入口。
    """
    if not _thread_exists(tid):
        raise HTTPException(404, f"会话不存在: {tid}")
    graph = build_graph_with_providers()
    try:
        return {"steps": list_steps(graph, tid), "running": _session_running(tid)}
    except Exception as e:
        raise HTTPException(500, f"读取检查点历史失败: {e}")


class RevertBody(BaseModel):
    checkpoint_id: str
    fields: dict[str, Any] | None = None  # 需求就地编辑（dot-path → 值），随回退生效


@app.post("/api/sessions/{tid}/revert")
async def revert_session(tid: str, body: RevertBody) -> dict[str, Any]:
    """回退到某步骤（节点刚落盘的检查点）并自动续跑下游。

    时序：先开新分支（清下游字段 / 可选改需求 / 跨落盘点还原备份文件），
    再从新分支 tip 以 stream(None) 推进——进度走 GET /events 订阅。
    """
    if not _thread_exists(tid):
        raise HTTPException(404, f"会话不存在: {tid}")
    if _session_running(tid):
        raise HTTPException(409, "该会话正在推进中，请等当前流程结束或暂停后再回退")
    graph = build_graph_with_providers()
    try:
        info = revert_thread(graph, tid, body.checkpoint_id, fields=body.fields)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"回退失败: {e}")
    started = await start_run(tid, None)  # 从新分支 tip 续跑；轮末锚点会立即 stream_end
    return {**info, "run": started}


@app.get("/api/sessions/{tid}/graph-type-candidates")
def graph_type_candidates(tid: str) -> dict[str, Any]:
    """制图前图种类候选：与 graph_type_select 门禁同源的规则推断。

    会话恢复（刷新/重开页面）时前端重放选择卡用；pending=true 表示门禁仍在等待选择。
    """
    if not _thread_exists(tid):
        raise HTTPException(404, f"会话不存在: {tid}")
    graph = build_graph_with_providers()
    try:
        snap = graph.get_state(_config(thread_id=tid))
    except Exception as e:
        raise HTTPException(404, f"会话不存在或读取失败: {e}")
    vals = snap.values or {}
    from devflow.graph_types import suggest_graph_types

    return {
        "candidates": suggest_graph_types(vals.get("requirement")),
        "graph_type": vals.get("graph_type"),
        "pending": "graph_type_select" in list(snap.next or []),
    }


@app.get("/api/sessions/{tid}/requirement-review")
def requirement_review_payload_endpoint(tid: str) -> dict[str, Any]:
    """制图前需求确认门禁载荷：与 requirement_review 节点同源重算（读 state.requirement）。

    interrupt 载荷不落 checkpoint，会话恢复时前端用它重放确认卡；
    pending=true 表示门禁仍在等待确认。
    """
    if not _thread_exists(tid):
        raise HTTPException(404, f"会话不存在: {tid}")
    graph = build_graph_with_providers()
    try:
        snap = graph.get_state(_config(thread_id=tid))
    except Exception as e:
        raise HTTPException(404, f"会话不存在或读取失败: {e}")
    vals = snap.values or {}
    from devflow.nodes import requirement_review_payload

    return {
        **requirement_review_payload(vals),
        "pending": "requirement_review" in list(snap.next or []),
    }


@app.get("/api/sessions/{tid}/checklist-candidates")
def checklist_candidates(tid: str) -> dict[str, Any]:
    """清单路由候选树：与 checklist_route_gate 门禁同源（读 match 节点落盘的 state）。

    会话恢复时前端重放确认卡用；pending=true 表示门禁仍在等待确认。
    零 LLM 重算——候选树在 interrupt 前已由 checklist_route_match 写入 checkpoint。
    """
    if not _thread_exists(tid):
        raise HTTPException(404, f"会话不存在: {tid}")
    graph = build_graph_with_providers()
    try:
        snap = graph.get_state(_config(thread_id=tid))
    except Exception as e:
        raise HTTPException(404, f"会话不存在或读取失败: {e}")
    vals = snap.values or {}
    route = vals.get("checklist_route") or {}
    candidates = route.get("candidates") or []
    return {
        "root": route.get("root", ""),
        "candidates": candidates,
        # matched=有候选 / no_match=库有内容但无匹配 / empty_library=空库；
        # 旧 checkpoint 无 status 时按候选有无推断
        "status": route.get("status") or ("matched" if candidates else "empty_library"),
        "business_count": int(route.get("business_count") or 0),
        "decision": route.get("decision"),
        "selected": route.get("selected") or [],
        "pending": "checklist_route_gate" in list(snap.next or []),
    }


def _serialize_state(values: Any) -> dict[str, Any]:
    """checkpoint values → JSON-safe dict（messages 转 {id, type, content}）。

    带上消息 id：前端把对话气泡锚到回退步骤（最早包含该 id 的检查点）。
    """
    if not isinstance(values, dict):
        return {"raw": str(values)}
    out: dict[str, Any] = {}
    for k, v in values.items():
        if k == "messages" and isinstance(v, list):
            out[k] = [
                {
                    "id": getattr(m, "id", None),
                    "type": getattr(m, "type", "message"),
                    "content": str(getattr(m, "content", "")),
                }
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
    if _session_running(tid):
        raise HTTPException(409, "会话正在推进中，先等流程暂停再删除")
    conn = _get_sqlite_conn()
    for table in ("checkpoints", "writes"):
        try:
            conn.execute(f"DELETE FROM {table} WHERE thread_id = ?", (tid,))
        except Exception:
            pass  # 表不存在则跳过
    conn.commit()
    _clear_run_mark(tid)
    _buses.pop(tid, None)
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


def _provider_chain() -> list[dict[str, Any]]:
    """当前生效的 provider 链：已停用的排末尾并标记，其余按「粘性成功者优先」排序。"""
    from devflow.llm_client import _disabled_providers, _sticky_provider, _candidates_providers

    live = [
        {
            "name": p.get("name", ""),
            "model": p.get("model", ""),
            "models": list(p.get("models") or [p.get("model")]),
            "disabled": False,
            "sticky": p.get("name") == _sticky_provider,
        }
        for p in _candidates_providers()
    ]
    disabled = [
        {
            "name": p.get("name", ""),
            "model": p.get("model", ""),
            "models": list(p.get("models") or [p.get("model")]),
            "disabled": True,
            "sticky": False,
        }
        for p in settings.LLM_PROVIDERS
        if p.get("name") in _disabled_providers
    ]
    return live + disabled


@app.get("/api/providers")
def list_providers() -> dict[str, Any]:
    """provider 链（生效顺序：粘性成功者优先；已确认无效的排末尾并标记）+ mock 兜底开关。

    供首页供应商面板展示与选择；链顺序可在运行期通过 /api/providers/active 调整。
    """
    chain = _provider_chain()
    sticky = next((c["name"] for c in chain if c.get("sticky")), None)
    return {"chain": chain, "sticky": sticky, "mock_fallback": settings.LLM_USE_MOCK_FALLBACK}


@app.post("/api/providers/active")
def set_active_provider(body: ActiveProviderBody) -> dict[str, Any]:
    """把指定 provider(+model) 条目挪到 fallback 链首位。

    进程内全局生效（不改 .env，重启后回到 LLM_PROVIDERS_JSON / LLM_ACTIVE_MODEL 的顺序）。
    """
    for i, p in enumerate(settings.LLM_PROVIDERS):
        if p.get("name") == body.name and (
            body.model is None or p.get("model") == body.model
        ):
            settings.LLM_PROVIDERS.insert(0, settings.LLM_PROVIDERS.pop(i))
            # 模型实例按 spec 内容缓存，与顺序无关，无需清缓存
            return {"chain": _provider_chain(), "active": {"name": body.name, "model": body.model}}
    raise HTTPException(404, f"未找到 provider 条目: {body.name!r} (model={body.model!r})")


@app.post("/api/providers/check")
async def check_providers(body: CheckProviderBody | None = None) -> dict[str, Any]:
    """对 provider 做一次最小连通性调用（name 缺省 = 检测全部），返回诊断报告。

    检测通过自动解除停用；鉴权失败（401/403）自动停用——服务端确认无效后就不再使用。
    """
    from devflow.llm_client import check_llm_all, check_llm_provider, disable_provider, enable_provider

    if body and body.name:
        spec = next((p for p in settings.LLM_PROVIDERS if p.get("name") == body.name), None)
        if spec is None:
            raise HTTPException(404, f"未找到 provider 条目: {body.name!r}")
        reports = [await check_llm_provider(spec, timeout_sec=12)]
    else:
        reports = await check_llm_all()
    for rep in reports:
        if rep.get("ok"):
            enable_provider(rep.get("name", ""))
        elif rep.get("error_code") == "HTTP.AUTH":
            disable_provider(rep.get("name", ""), str(rep.get("error_message") or ""))
    return {"reports": reports, "chain": _provider_chain()}


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
# Checklist 库：树浏览 + 用例沉淀（distill 预览 → commit 落盘）
# ═══════════════════════════════════════════════════════════════════


@app.get("/api/sessions/{tid}/checklist-tree")
def checklist_tree_api(tid: str) -> dict[str, Any]:
    """库全树（业务/子业务、描述、条目数）。按会话的 project_root 解析库根，
    供沉淀弹窗选择业务类型；库未创建时返回空树（前端可新建业务）。"""
    if not _thread_exists(tid):
        raise HTTPException(404, f"会话不存在: {tid}")
    graph = build_graph_with_providers()
    vals = graph.get_state(_config(thread_id=tid)).values or {}
    from devflow.checklist.library import checklist_tree, resolve_root

    req = vals.get("requirement") or {}
    root = resolve_root(str(req.get("project_root") or ""))
    return {"root": str(root), "tree": checklist_tree(root)}


class DistillRequest(BaseModel):
    # 用户标记的业务类型：rel_dir 必填（已有业务或新建英文目录名）
    business: dict[str, Any] | None = None
    case_ids: list[str] = []       # 勾选的有效用例；空 = 全部（仅 source=cases 时生效）
    source: str = "cases"          # cases=会话用例归纳 / manual=手写清单规范化
    text: str = ""                 # source=manual 时的手写原文（markdown）


class DistillCommit(BaseModel):
    rel_dir: str
    scenario_md: str
    checklist_md: str


def _session_test_cases(tid: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """从 checkpoint 读会话用例与 requirement（distill 数据源）。"""
    graph = build_graph_with_providers()
    vals = graph.get_state(_config(thread_id=tid)).values or {}
    report = vals.get("test_report") or {}
    cases = [c for c in (report.get("test_cases") or []) if isinstance(c, dict)]
    return cases, vals.get("requirement") or {}


async def _distill_preview(
    tid: str,
    business: dict[str, Any] | None,
    *,
    cases: list[dict[str, Any]] | None = None,
    doc_text: str | None = None,
    source_label: str = "",
) -> dict[str, Any]:
    """归纳预览（不落盘）：cases 走用例归纳，doc_text 走文档/手写规范化。

    目标目录已有 checklist.md 时走 merge（旧清单原文一并交给 LLM 去重合并），
    且旧 frontmatter 的 sources 与本次来源累积合并，不丢溯源。
    """
    from devflow.checklist.distill import (
        distill_from_cases,
        distill_from_doc,
        merge_sources,
        render_checklist_md,
        render_scenario_md,
    )
    from devflow.checklist.library import resolve_root

    biz = business or {}
    rel_dir = validate_rel_dir(str(biz.get("rel_dir") or ""))
    if not rel_dir:
        raise HTTPException(400, "业务类型目录名非法（限英文/数字/连字符，可含 / 子业务）")

    req = build_graph_with_providers().get_state(_config(thread_id=tid)).values.get("requirement") or {}
    root = resolve_root(str(req.get("project_root") or ""))
    existing_scenario = ""
    existing_checklist = ""
    target = root / rel_dir
    if (target / "checklist.md").is_file():
        existing_checklist = (target / "checklist.md").read_text(encoding="utf-8")
    if (target / "scenario.md").is_file():
        existing_scenario = (target / "scenario.md").read_text(encoding="utf-8")
    mode = "merge" if existing_checklist else "create"

    biz_input = {
        "rel_dir": rel_dir,
        "name": str(biz.get("name") or ""),
        "description": str(biz.get("description") or ""),
    }
    try:
        if doc_text is not None:
            out = await distill_from_doc(doc_text, biz_input, existing_scenario, existing_checklist)
        else:
            out = await distill_from_cases(cases or [], biz_input, existing_scenario, existing_checklist)
    except Exception as e:
        raise HTTPException(502, f"清单归纳失败：{e}")

    checklist_md = render_checklist_md(
        out, business=rel_dir, sources=merge_sources(existing_checklist, source_label)
    )
    return {
        "mode": mode,
        "rel_dir": rel_dir,
        "scenario_md": render_scenario_md(out),
        "checklist_md": checklist_md,
        "merge_notes": out.merge_notes,
        "case_count": len(cases or []),
    }


@app.post("/api/sessions/{tid}/checklist/distill")
async def distill_checklist(tid: str, body: DistillRequest) -> dict[str, Any]:
    """有效用例（或手写原文）→ LLM 归纳为 scenario.md/checklist.md 预览（不落盘）。"""
    if not _thread_exists(tid):
        raise HTTPException(404, f"会话不存在: {tid}")
    if body.source == "manual":
        if not body.text.strip():
            raise HTTPException(400, "手写清单内容为空")
        return await _distill_preview(
            tid, body.business, doc_text=body.text, source_label=tid
        )
    cases, _ = _session_test_cases(tid)
    if not cases:
        raise HTTPException(400, "该会话没有可沉淀的测试用例")
    if body.case_ids:
        wanted = set(body.case_ids)
        cases = [c for c in cases if c.get("case_id") in wanted]
        if not cases:
            raise HTTPException(400, "勾选的用例 ID 均不存在")
    return await _distill_preview(tid, body.business, cases=cases, source_label=tid)


@app.post("/api/sessions/{tid}/checklist/import")
async def import_checklist(
    tid: str,
    file: UploadFile = File(...),
    rel_dir: str = Form(""),
    name: str = Form(""),
    description: str = Form(""),
) -> dict[str, Any]:
    """上传清单文档（wiki 页面 / 验收清单 / 用例文档）→ AI 按库规范归纳入库预览。

    落盘复用 /checklist/commit；溯源记 "import:<文件名>"，与沉淀来源区分。
    """
    if not _thread_exists(tid):
        raise HTTPException(404, f"会话不存在: {tid}")
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
    if not text.strip():
        raise HTTPException(400, "文档解析结果为空，无法归纳")
    return await _distill_preview(
        tid,
        {"rel_dir": rel_dir, "name": name, "description": description},
        doc_text=text,
        source_label=f"import:{Path(file.filename or 'doc').name}",
    )


@app.post("/api/sessions/{tid}/checklist/commit")
def commit_checklist(tid: str, body: DistillCommit) -> dict[str, Any]:
    """确认后的预览写入库（覆盖写；merge 已在 distill 预览环节完成）。"""
    rel_dir = validate_rel_dir(body.rel_dir)
    if not rel_dir:
        raise HTTPException(400, f"非法的业务目录名: {body.rel_dir!r}")
    if not body.scenario_md.strip() or not body.checklist_md.strip():
        raise HTTPException(400, "scenario.md / checklist.md 内容为空")
    graph = build_graph_with_providers()
    vals = graph.get_state(_config(thread_id=tid)).values or {}
    req = vals.get("requirement") or {}
    from devflow.checklist.library import resolve_root, write_checklist

    root = resolve_root(str(req.get("project_root") or ""))
    target = write_checklist(root, rel_dir, body.scenario_md, body.checklist_md)
    return {"written": [str(target / "scenario.md"), str(target / "checklist.md")], "root": str(root)}


# ═══════════════════════════════════════════════════════════════════
# SSE 流（事件订阅）+ 推进入口（消息 / 门禁决策 / 回退）
# ═══════════════════════════════════════════════════════════════════


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"


@app.get("/api/sessions/{tid}/events")
async def events_stream(tid: str, after: int = 0) -> StreamingResponse:
    """订阅会话进度（SSE）：回放缓冲 seq>after → 实时 tail 到 stream_end。

    断线重连带上次游标即可补齐丢失事件；浏览器关闭/换会话后回来同样适用。
    """
    if not _thread_exists(tid):
        raise HTTPException(404, f"会话不存在: {tid}")
    return StreamingResponse(
        _events_gen(_bus(tid), max(0, after)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _gate_resume(
    decision: str, comment: str = "", selected: str = "", fields: dict[str, Any] | None = None
) -> Command:
    """门禁决策 → Command(resume=...)。

    无附加信息时 resume 传裸字符串（兼容旧门禁）；带 comment/selected（清单
    路由勾选）/fields（需求确认就地修改）时传结构化 dict，由各门禁节点自行解析。
    """
    sel_list = [s.strip() for s in selected.split(",") if s.strip()] if selected else None
    if comment or sel_list or fields:
        payload: dict[str, Any] = {"decision": decision}
        if comment:
            payload["comment"] = comment
        if sel_list:
            payload["selected"] = sel_list
        if fields:
            payload["fields"] = fields
        return Command(resume=payload)
    return Command(resume=decision)


@app.post("/api/sessions/{tid}/messages")
async def send_message(tid: str, body: SendMessage) -> dict[str, Any]:
    """提交用户消息：立即返回，进度走 GET /events 订阅。"""
    if not _thread_exists(tid):
        raise HTTPException(404, f"会话不存在: {tid}")
    if _gate_pending(build_graph_with_providers(), tid):
        raise HTTPException(409, "当前有等待确认的门禁，请先在门禁卡片上完成选择，再继续对话")
    result = await start_run(tid, {"messages": [HumanMessage(content=body.text)]})
    if not result.get("accepted"):
        raise HTTPException(409, result.get("reason", "该会话正在推进中"))
    return result


@app.post("/api/sessions/{tid}/gates")
async def decide_gate(tid: str, body: GateDecision) -> dict[str, Any]:
    """提交门禁决策：立即返回，进度走 GET /events 订阅。"""
    if not _thread_exists(tid):
        raise HTTPException(404, f"会话不存在: {tid}")
    input_msg: Any = _gate_resume(
        body.decision, body.comment or "", ",".join(body.selected or []), body.fields
    )
    result = await start_run(tid, input_msg)
    if not result.get("accepted"):
        raise HTTPException(409, result.get("reason", "该会话正在推进中"))
    return result


class AdoptBody(BaseModel):
    case_ids: list[str] = []       # 测试卡上勾选采纳的用例（采纳 = 评审通过）


@app.post("/api/sessions/{tid}/review/adopt")
async def adopt_review(tid: str, body: AdoptBody) -> dict[str, Any]:
    """提交用例采纳 = 终审通过：仅当会话正停在 human_review 门禁时接受。

    组装 resume {"decision": "approve", "adopted": [...]}，review 节点把采纳
    集落 state.adopted_cases（沉淀建议卡预勾选与导出标记的数据源）。
    """
    if not _thread_exists(tid):
        raise HTTPException(404, f"会话不存在: {tid}")
    case_ids = [str(c).strip() for c in body.case_ids if str(c).strip()]
    if not case_ids:
        raise HTTPException(400, "至少勾选一条要采纳的用例")
    graph = build_graph_with_providers()
    snap = graph.get_state(_config(thread_id=tid))
    has_interrupt = any(getattr(t, "interrupts", None) for t in (snap.tasks or ()))
    if "review" not in list(snap.next or []) or not has_interrupt:
        raise HTTPException(409, "终审门禁尚未就绪：请等测试执行完成（门禁弹出）后再提交采纳")
    result = await start_run(tid, Command(resume={"decision": "approve", "adopted": case_ids}))
    if not result.get("accepted"):
        raise HTTPException(409, result.get("reason", "该会话正在推进中"))
    return {"accepted": True, "adopted": case_ids}


@app.post("/api/sessions/{tid}/distill/dismiss")
def dismiss_distill_prompt(tid: str) -> dict[str, Any]:
    """沉淀建议卡「暂不」：落 checkpoint，会话恢复后不再提示。"""
    if not _thread_exists(tid):
        raise HTTPException(404, f"会话不存在: {tid}")
    if _session_running(tid):
        raise HTTPException(409, "该会话正在推进中，请稍后再操作")
    graph = build_graph_with_providers()
    graph.update_state(_config(thread_id=tid), {"distill_dismissed": True})
    return {"dismissed": True}


# ═══════════════════════════════════════════════════════════════════
# 服务重启：扫描进行中标记，对停在半途的会话自动续跑
# ═══════════════════════════════════════════════════════════════════


@app.on_event("startup")
async def resume_interrupted_runs() -> None:
    """重启续跑：上次进程内跑到一半的会话，从最后 checkpoint 自动推进到下一暂停点。

    只续跑停在「非门禁节点」的会话——门禁挂起说明在等用户决策，保持等待。
    """
    d = _runs_dir()
    if not d.is_dir():
        return
    marks = list(d.glob("*.json"))
    if not marks:
        return
    print(f"[resume] 发现 {len(marks)} 个进行中标记，尝试自动续跑", flush=True)
    graph = build_graph_with_providers()
    for f in marks:
        tid = f.stem
        f.unlink(missing_ok=True)  # 标记先清掉；续跑真正启动时 start_run 会重打
        try:
            if not _thread_exists(tid):
                continue
            next_nodes = [str(n) for n in (graph.get_state(_config(thread_id=tid)).next or [])]
            if not next_nodes or set(next_nodes) & _GATE_NODES:
                print(f"[resume] {tid} 停在门禁/轮末（{next_nodes or 'END'}），保持等待用户", flush=True)
                continue
            result = await start_run(tid, None)
            print(f"[resume] {tid} 已自动续跑: accepted={result.get('accepted')}", flush=True)
        except Exception as e:  # noqa: BLE001 — 单会话续跑失败不影响其余
            print(f"[resume] {tid} 续跑失败: {e}", flush=True)


# ═══════════════════════════════════════════════════════════════════
# 静态资源
# ═══════════════════════════════════════════════════════════════════


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


# ═══════════════════════════════════════════════════════════════════
# 首次启动预检（非阻塞）：缺 .env 时打印一次引导，不拦启动
# ═══════════════════════════════════════════════════════════════════

_note = first_run_notice()
if _note:
    print(_note, flush=True)


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
