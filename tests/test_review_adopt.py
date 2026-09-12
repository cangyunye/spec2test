"""用例采纳评审测试：提交采纳 = 终审通过（adopted 落 state）+ 沉淀建议「暂不」持久化。"""
from __future__ import annotations

import asyncio
import uuid

from langgraph.types import Command

from devflow.orchestrator import build_graph_with_providers, initial_state

_CASES = [
    {"case_id": "TC-001", "tier": "functional", "priority": "P0", "title": "全额退款成功",
     "target": "refund", "precondition": "已支付订单", "steps": "发起退款", "expected": "已退款"},
    {"case_id": "TC-002", "tier": "functional", "priority": "P1", "title": "超额退款被拒",
     "target": "refund", "precondition": "已支付订单", "steps": "超额退款", "expected": "拒绝"},
]


def _mk_session() -> str:
    """造一个带 test_report 的会话（checkpoint 直写，不跑节点）。"""
    graph = build_graph_with_providers()
    tid = f"w-{uuid.uuid4().hex[:8]}"
    state = initial_state()
    state["requirement"]["project_context"] = "订单退款系统"
    state["test_report"] = {"session_id": "s-1", "test_cases": _CASES, "run": {}}
    graph.update_state({"configurable": {"thread_id": tid}}, state)
    return tid


def _park_at_review(tid: str) -> None:
    """把会话推进到终审门禁挂起：以 test_run 名义落盘后 stream(None) 跑 review。"""
    graph = build_graph_with_providers()
    config = {"configurable": {"thread_id": tid}}
    graph.update_state(config, {"current_stage": "test"}, as_node="test_run")
    list(graph.stream(None, config, stream_mode="updates"))  # review 节点 interrupt 挂起


async def _adopt_and_settle(tid: str, case_ids: list[str]) -> dict:
    """提交采纳并等后台 run 跑完（task done = checkpoint 已落定）。"""
    from fastapi import HTTPException

    from web.server import AdoptBody, _bus, adopt_review

    try:
        resp = await adopt_review(tid, AdoptBody(case_ids=case_ids))
    except HTTPException as e:
        return {"http_error": e.status_code, "detail": e.detail}
    bus = _bus(tid)
    for _ in range(300):
        if bus.task and bus.task.done():
            break
        await asyncio.sleep(0.05)
    return resp


def test_adopt_approves_review_and_records(monkeypatch):
    """停在终审：提交采纳 → 自动 approve → state.adopted_cases 记录 → 到 END。"""
    tid = _mk_session()
    _park_at_review(tid)
    graph = build_graph_with_providers()
    config = {"configurable": {"thread_id": tid}}
    snap = graph.get_state(config)
    assert snap.next == ("review",)  # 前置确认：确实停在终审

    resp = asyncio.run(_adopt_and_settle(tid, ["TC-001"]))
    assert resp.get("accepted") is True
    vals = build_graph_with_providers().get_state(config).values
    assert vals["current_stage"] == "done"
    assert vals["adopted_cases"] == ["TC-001"]


def test_adopt_rejects_when_gate_not_pending():
    """未停在终审（无 interrupt）→ 409，不会误触发其他推进。"""
    tid = _mk_session()
    resp = asyncio.run(_adopt_and_settle(tid, ["TC-001"]))
    assert resp["http_error"] == 409


def test_review_without_adopt_keeps_none():
    """兜底路径：门禁手动 approve（无 adopted 字段）→ adopted_cases 保持 None。"""
    from devflow.nodes.review import review_node

    tid = _mk_session()
    _park_at_review(tid)
    graph = build_graph_with_providers()
    config = {"configurable": {"thread_id": tid}}
    list(graph.stream(Command(resume="approve"), config, stream_mode="updates"))
    vals = graph.get_state(config).values
    assert vals["current_stage"] == "done"
    assert vals.get("adopted_cases") is None


def test_distill_dismiss_persists():
    """沉淀建议「暂不」：落 checkpoint，会话恢复不再提示的数据源。"""
    from web.server import dismiss_distill_prompt

    tid = _mk_session()
    dismiss_distill_prompt(tid)
    graph = build_graph_with_providers()
    vals = graph.get_state({"configurable": {"thread_id": tid}}).values
    assert vals["distill_dismissed"] is True
