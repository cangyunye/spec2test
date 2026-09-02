"""制图门禁（graph_review）：制图后、进代码检索前的人工确认。

确认"逻辑图 ↔ 需求"对齐：
  - approve → 进入 code_search（current_stage="search"）
  - reject  → 回 graph_generate 重制图（current_stage="graph"）

与终审 review 门复用同一 interrupt 模式；resume 值同为 approve/reject，
但挂起的是不同的 interrupt，LangGraph 天然区分，无需额外标记。
"""
from __future__ import annotations

from typing import Any

from langgraph.types import interrupt

from ..state import GlobalState


def graph_review_node(state: GlobalState) -> dict[str, Any]:
    """制图评审中断：展示逻辑图摘要，等待 confirm（approve）/ revise（reject）。"""
    graph = state.get("logic_graph") or {}
    nodes = graph.get("nodes", [])
    edges = graph.get("edges", [])
    modified = [n for n in nodes if n.get("is_modified")]

    payload = {
        "type": "graph_review",
        "graph_id": graph.get("graph_id"),
        "node_count": len(nodes),
        "modified_count": len(modified),
        "edge_count": len(edges),
        "summary": (
            f"逻辑图 graph_id={graph.get('graph_id', '?')}：{len(nodes)} 个节点"
            f"（修改 {len(modified)} 个）、{len(edges)} 条边。"
            "请确认此图与需求对齐：approve 进入代码检索 / reject 重制图。"
        ),
    }

    decision = interrupt(payload)

    if decision == "approve":
        return {"current_stage": "search"}
    return {
        "current_stage": "graph",
        "last_error": "[graph_review] 制图未获确认，回退重新制图",
    }


def route_after_graph_review(state: GlobalState) -> str:
    """制图评审后路由：approved → code_search；rejected → graph_generate。"""
    if state.get("current_stage") == "search":
        return "approved"
    return "rejected"