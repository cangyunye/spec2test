"""测试设计 feature 拆分节点（feature_split → feature_gate → test_gen）。

与 checklist 路由同样的两段式设计：LLM 拆分（match 类，结果落 state）与
确认门禁（interrupt，恢复重放只读 state，零重复 LLM 调用）分开——
feature_gate 恢复重放时不重算拆分。

流转：
  - state.features 已存在（评审打回回炉）→ 两节点均直接放行，不重拆
  - 代码模式：逻辑图节点确定性聚类 + LLM 仅命名；仅需求模式：LLM + Schema 拆解；
    两条路都失败 → 单 feature（整单）降级
  - 覆盖自检：每条验收标准/边界场景必须归属；未归属 → F0 综合与集成
  - feature_gate：有 open_questions 才 interrupt；跳过/不答按 recommended 继续
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from langgraph.types import interrupt

from ..feature_split import (
    F0_ID,
    clamp_features,
    cluster_nodes_by_target,
    llm_name_features,
    llm_split_features,
    make_feature,
    assign_uncovered,
)
from ..schemas import has_project_code
from ..state import GlobalState

logger = logging.getLogger(__name__)


async def _split_async(state: GlobalState) -> dict[str, Any]:
    from ..config import settings

    # TEST_DESIGN_MODE=single（默认）：拆分不启用，整单一次性设计（模型可靠时的路径）
    if settings.TEST_DESIGN_MODE != "feature":
        return {}
    req = state.get("requirement") or {}
    logic_graph = state.get("logic_graph") or {}
    questions: list[dict[str, Any]] = []
    assumptions: list[str] = []

    features = await _build_features(req, logic_graph, questions, assumptions)
    if not features:
        return {}  # 既聚不出也拆不出 → 单次整单路径（test_gen 按无 features 处理）
    features, gaps = assign_uncovered(features, req)
    features = clamp_features(features, settings.TEST_DESIGN_MAX_FEATURES)
    for note in assumptions:
        questions.append({"assumption": note, "resolved": True})
    if gaps:
        questions.append({
            "question": "以下准则在拆分时未能归属到明确功能点，已归入 F0 综合与集成，是否认可？",
            "options": ["认可（保持 F0 兜底）", "不认可（回炉重新拆分）"],
            "recommended": "认可（保持 F0 兜底）",
            "feature_ids": [F0_ID],
            "resolved": False,
        })
    logger.info("feature_split: %d 个功能点（%s）", len(features),
                ", ".join(f.get("feature_id", "?") for f in features))
    return {"features": features, "feature_questions": questions}


async def _build_features(
    req: dict[str, Any],
    logic_graph: dict[str, Any],
    questions: list[dict[str, Any]],
    assumptions: list[str],
) -> list[dict[str, Any]]:
    """代码模式聚类 + LLM 命名；仅需求模式 LLM 拆解；都失败 → None（调用方降级）。"""
    if has_project_code(req):
        features = cluster_nodes_by_target(logic_graph, req)
        if features:
            return await llm_name_features(features)
    split = await llm_split_features(req, logic_graph)
    if split is None:
        return _fallback_single(req)
    features, llm_questions, llm_assumptions = split
    questions.extend(
        {
            "question": str(q.get("question") or "").strip(),
            "options": [str(o) for o in (q.get("options") or []) if str(o).strip()],
            "recommended": str(q.get("recommended") or "").strip(),
            "feature_ids": [str(x) for x in (q.get("feature_ids") or [])],
            "resolved": False,
        }
        for q in llm_questions
        if str(q.get("question") or "").strip()
    )
    assumptions.extend(a for a in llm_assumptions if a)
    return features


def _fallback_single(req: dict[str, Any]) -> list[dict[str, Any]]:
    """LLM 拆分不可用：整单作为单 feature（含全部准则），流程不中断。"""
    targets = [str(m) for m in (req.get("target_modules") or []) if m]
    criteria = [str(c) for c in (req.get("acceptance_criteria") or []) if c]
    edges = [str(c) for c in (req.get("edge_cases") or []) if c]
    if not targets and (criteria or edges):
        targets = ["端到端场景"]
    if not targets and not criteria and not edges:
        return []
    return [make_feature(
        "F1",
        "、".join(targets[:3]) or "整单需求",
        "LLM 拆分不可用，按整单一次性设计（TEST_DESIGN_MODE=feature 下的降级路径）",
        target_modules=targets,
        acceptance_criteria=criteria,
        edge_cases=edges,
    )]


def feature_split_node(state: GlobalState) -> dict[str, Any]:
    """拆分（确定性聚类 / LLM）：结果写 state.features；已拆过直接放行。"""
    if state.get("features"):
        return {}
    return asyncio.run(_split_async(state))


def _parse_answers(resume: Any, pending: list[dict[str, Any]]) -> dict[str, str]:
    """resume 值 → {问题原文: 回答}。兼容按序数组与 {问题: 回答} 两种形态。"""
    if not isinstance(resume, dict):
        return {}
    raw = resume.get("answers")
    out: dict[str, str] = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            if str(v).strip():
                out[str(k)] = str(v).strip()
        return out
    if isinstance(raw, list):
        for q, v in zip(pending, raw):
            if str(v or "").strip():
                out[str(q.get("question") or "")] = str(v).strip()
    return out


def feature_gate_node(state: GlobalState) -> dict[str, Any]:
    """拆分问题确认门禁：有 open_questions 才弹；跳过/不答按 recommended 继续。

    恢复重放：questions 已由拆分节点落 checkpoint，interrupt() 直接返回 resume 值。
    """
    questions = [dict(q) for q in (state.get("feature_questions") or []) if isinstance(q, dict)]
    pending = [q for q in questions if q.get("question") and not q.get("resolved")]
    if not pending:
        return {}
    resume = interrupt({
        "type": "feature_questions",
        "summary": "测试拆分完成，确认以下问题后开始逐功能点设计（不回答按推荐项继续）",
        "features": [
            {k: f.get(k) for k in ("feature_id", "name", "description", "target_modules")}
            for f in (state.get("features") or [])
        ],
        "assumptions": [q.get("assumption") for q in questions if q.get("assumption")],
        "questions": pending,
    })
    answers = _parse_answers(resume, pending)
    skip = isinstance(resume, dict) and str(resume.get("decision") or "").lower() == "skip"
    for q in pending:
        key = str(q.get("question") or "")
        ans = answers.get(key, "")
        if not ans and skip:
            ans = str(q.get("recommended") or "")
        q["answer"] = ans or str(q.get("recommended") or "")
        q["resolved"] = True
    return {"feature_questions": questions}


__all__ = ["feature_split_node", "feature_gate_node"]
