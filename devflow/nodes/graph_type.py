"""制图前图种类选择门禁（graph_type_select）。

澄清完成后、制图前，把候选种类（含按需求推断的推荐标记）以 interrupt 抛给
用户选择；用户选定后写入 state.graph_type，后续制图/重制图/驳回重做都沿用该
种类，不再重复询问（含 code_search 空结果回退澄清再进制图的路径）。

resume 值兼容两种形态：
  - "sequence" 等种类 id 字符串（CLI 旧路径 / Web op=gate&decision=sequence）
  - {"graph_type": "sequence"}（结构化回传）
非法/缺省 → 回退默认种类 flowchart（提示词层面保证任何需求都能制图）。

与 graph_review / review 门复用同一 interrupt 模式；LangGraph 天然区分挂起点。
"""
from __future__ import annotations

from typing import Any

from langgraph.types import interrupt

from ..graph_types import DEFAULT_GRAPH_TYPE, normalize_graph_type, suggest_graph_types
from ..state import GlobalState


def _parse_choice(resume: Any) -> str:
    """resume 值 → 种类 id。兼容 str 与 dict 两种形态；未知值回退默认。"""
    if isinstance(resume, dict):
        raw = resume.get("graph_type") or resume.get("decision") or resume.get("type")
    else:
        raw = resume
    return normalize_graph_type(raw) if str(raw or "").strip() else DEFAULT_GRAPH_TYPE


def graph_type_select(state: GlobalState) -> dict[str, Any]:
    """图种类选择中断：展示候选与推荐，等待用户选定后写入 state.graph_type。

    已有 graph_type（回退/重入路径）→ 直接放行，不重复打断。
    注意：interrupt 挂起时本节点的返回不会落 checkpoint，恢复后节点从头重跑、
    interrupt() 直接返回 resume 值——候选推断是纯规则，重算幂等。
    """
    if str(state.get("graph_type") or "").strip():
        return {"current_stage": "graph"}

    req = state.get("requirement") or {}
    candidates = suggest_graph_types(req)
    chosen = _parse_choice(
        interrupt({
            "type": "graph_type_select",
            "candidates": candidates,
            "default": DEFAULT_GRAPH_TYPE,
            "requirement_context": str(req.get("project_context") or ""),
            "summary": "请选择本次逻辑图的种类（影响制图视角与结构化产物的形态）",
        })
    )
    return {"graph_type": chosen, "current_stage": "graph"}
