"""用例设计前的 Checklist 路由门禁（checklist_route_match → checklist_route_gate）。

拆成两个节点是刻意的：LangGraph interrupt 挂起时节点返回不落 checkpoint，
恢复后节点从头重跑——若匹配（LLM 调用）与确认（interrupt）在同节点，每次
恢复都要重算一遍路由。拆开后 match 节点先完成并落 checkpoint，gate 节点
恢复重放时直接读 state.checklist_route 的候选树，零重复 LLM 调用。

流转：
  - 已路由过（checklist_routed）→ 两节点均直接放行（用例回炉重生成不重复弹）
  - 空库 / 需求无匹配 → match 节点记 checklist_routed=True，静默放行
  - 有候选 → gate 节点 interrupt：用户勾选确认后加载 checklist.md 写
    checklist_context，skip 则不注入（与现状等价）
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from langgraph.types import interrupt

from ..checklist.library import build_candidates, load_checklists, resolve_root
from ..checklist.routing import match_businesses, summarize_requirement
from ..state import GlobalState


async def _match_async(state: GlobalState) -> dict[str, Any]:
    req = state.get("requirement") or {}
    root = resolve_root(str(req.get("project_root") or ""))
    text = summarize_requirement(req)
    match = await match_businesses(text, root)
    if not match.businesses:
        # 空库 / 无匹配：标记已路由，静默放行（不打断用户）
        return {"checklist_routed": True, "checklist_route": None}
    candidates = build_candidates(root, match)
    if not candidates:
        return {"checklist_routed": True, "checklist_route": None}
    return {
        "checklist_route": {
            "root": str(root),
            "candidates": [c.model_dump() for c in candidates],
        }
    }


def checklist_route_match(state: GlobalState) -> dict[str, Any]:
    """路由匹配（LLM）：结果写 state.checklist_route 供门禁节点与恢复端点使用。"""
    if state.get("checklist_routed"):
        return {}
    return asyncio.run(_match_async(state))


def _parse_resume(resume: Any) -> tuple[str, list[str]]:
    """resume 值 → (decision, selected)。兼容 str（CLI 旧路径）与 dict。"""
    if isinstance(resume, dict):
        decision = str(resume.get("decision") or "").strip().lower()
        selected = resume.get("selected") or []
        if isinstance(selected, list):
            selected = [str(s) for s in selected if str(s).strip()]
        else:
            selected = []
        return decision, selected
    return str(resume or "").strip().lower(), []


def checklist_route_gate(state: GlobalState) -> dict[str, Any]:
    """路由确认门禁：候选树（AI 预选）勾选确认后才加载 checklist.md。

    恢复重放：本节点从头执行，state.checklist_route 已由 match 节点落盘，
    interrupt() 直接返回 resume 值，不重算路由。
    """
    if state.get("checklist_routed"):
        return {}
    route = state.get("checklist_route") or {}
    candidates = route.get("candidates") or []
    if not candidates:
        return {"checklist_routed": True}

    resume = interrupt({
        "type": "checklist_route",
        "candidates": candidates,
        "root": route.get("root", ""),
        "summary": "已按需求匹配到业务清单，请确认子业务是否正确（取消勾选即不加载）",
    })
    decision, selected = _parse_resume(resume)
    if decision != "confirm" or not selected:
        return {
            "checklist_routed": True,
            "checklist_route": {**route, "decision": "skip"},
        }
    loaded = load_checklists(Path(str(route.get("root") or "")), selected)
    return {
        "checklist_routed": True,
        "checklist_route": {**route, "decision": "confirm", "selected": selected},
        "checklist_context": (
            {"root": route.get("root", ""), "checklists": loaded} if loaded else None
        ),
    }


__all__ = ["checklist_route_gate", "checklist_route_match"]
