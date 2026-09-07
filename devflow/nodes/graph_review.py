"""制图门禁（graph_review）：制图后、进代码检索前的人工确认。

确认"逻辑图 ↔ 需求"对齐：
  - approve → 进入 code_search（current_stage="search"）
  - reject  → 回 graph_generate 重制图（current_stage="graph"）

resume 值兼容两种形态：
  - "approve" / "reject"（CLI 旧路径）
  - {"decision": "approve"|"reject", "comment": "修改意见"}（Web 带意见回传）
reject 的意见写入 state.review_feedback，graph_generate 下轮制图时针对性修正。

与终审 review 门复用同一 interrupt 模式；挂起的是不同的 interrupt，
LangGraph 天然区分，无需额外标记。
"""
from __future__ import annotations

from typing import Any

from langgraph.types import interrupt

from ..state import GlobalState


def _parse_decision(resume: Any) -> tuple[str, str | None]:
    """resume 值 → (decision, comment)。兼容 str 与 dict 两种形态。"""
    if isinstance(resume, dict):
        return str(resume.get("decision") or ""), (resume.get("comment") or "").strip() or None
    return str(resume or ""), None


def graph_review_node(state: GlobalState) -> dict[str, Any]:
    """制图评审中断：展示逻辑图摘要，等待 confirm（approve）/ revise（reject）。"""
    graph = state.get("logic_graph") or {}
    nodes = graph.get("nodes", [])
    edges = graph.get("edges", [])
    modified = [n for n in nodes if n.get("is_modified")]
    req = state.get("requirement") or {}

    payload = {
        "type": "graph_review",
        "graph_id": graph.get("graph_id"),
        "node_count": len(nodes),
        "modified_count": len(modified),
        "edge_count": len(edges),
        # 结构化视图（Web 渲染用；CLI 继续用 summary 文本）
        "mermaid_source": graph.get("mermaid_source", ""),
        "nodes": [
            {
                "node_id": n.get("node_id"),
                "label": n.get("label"),
                "node_type": n.get("node_type"),
                "is_modified": bool(n.get("is_modified")),
                "code_ref": n.get("code_ref"),
            }
            for n in nodes
        ],
        "requirement": {
            "project_context": req.get("project_context", ""),
            "target_modules": req.get("target_modules") or [],
            "edge_cases": req.get("edge_cases") or [],
            "acceptance_criteria": req.get("acceptance_criteria") or [],
        },
        "summary": (
            f"逻辑图 graph_id={graph.get('graph_id', '?')}：{len(nodes)} 个节点"
            f"（修改 {len(modified)} 个）、{len(edges)} 条边。"
            "请确认此图与需求对齐：approve 进入代码检索 / reject 重制图。"
        ),
    }

    decision, comment = _parse_decision(interrupt(payload))

    if decision == "approve":
        return {"current_stage": "search", "review_feedback": None}
    feedback = comment or "未通过评审（未填写具体意见），请重新检查与需求的对齐"
    return {
        "current_stage": "graph",
        "review_feedback": feedback,
        "last_error": "[graph_review] 制图未获确认，回退重新制图",
    }


def route_after_graph_review(state: GlobalState) -> str:
    """制图评审后路由：approved → code_search；rejected → graph_generate。"""
    if state.get("current_stage") == "search":
        return "approved"
    return "rejected"
