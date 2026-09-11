"""制图前需求确认门禁（requirement_review）。

澄清完备后、选图种类前，把 requirement 摊开给用户过目确认：模型从模糊描述里
提炼出来的字段，未经用户确认不得进入下一阶段（制图）。

门禁结果：
  - confirm（可携带字段修改）→ 写回 requirement，置 requirement_confirmed=True，进 graph_type_select
  - reject                   → 不进下一阶段，本轮 END；用户补充需求后重新走澄清

resume 值兼容：
  - "confirm" / "approve"（CLI 旧路径）
  - {"decision": "confirm", "fields": {"io_constraints.input": "..."}}（就地修改，点路径）
  - {"decision": "reject", "comment": "..."}

注意：interrupt 挂起时本节点的返回不落 checkpoint，恢复后节点从头重跑、interrupt()
直接返回 resume 值——所以载荷必须能由 state 重算（review_payload 同时给 Web 回放端点用）。
"""
from __future__ import annotations

import copy
from typing import Any

from langchain_core.messages import AIMessage
from langgraph.types import interrupt

from ..schemas import empty_requirement, has_project_code
from ..state import SOURCE_INFERRED, SOURCE_MOCK, SOURCE_USER, GlobalState

# 需求字段展示规格：(点路径, 中文标签, 值类型)
# 值类型给前端选控件：text 多行 / str 单行 / list 每行一项 / bool 勾选
REQUIREMENT_FIELD_SPEC: list[tuple[str, str, str]] = [
    ("req_type", "需求类型", "str"),
    ("project_context", "项目背景", "text"),
    ("io_constraints.input", "输入约束", "str"),
    ("io_constraints.output", "输出约束", "str"),
    ("edge_cases", "边界场景", "list"),
    ("acceptance_criteria", "验收标准", "list"),
    ("target_modules", "目标模块", "list"),
    ("existing_code_accessible", "已有代码可访问", "bool"),
    ("project_root", "项目根目录", "str"),
    ("reference_files", "参考文件", "list"),
]

# 抽取来源标记（B）：SOURCE_USER / SOURCE_INFERRED 定义在 state（状态契约的一部分）

_REJECT_WORDS = ("reject", "revise", "no", "驳回", "拒绝")


def _get(req: dict[str, Any], dotted: str) -> Any:
    cur: Any = req
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def requirement_fields(
    req: dict[str, Any], sources: dict[str, str] | None = None
) -> list[dict[str, Any]]:
    """展示用字段列表：空值字段也列出（用户可直接补），带来源标记。"""
    src = sources or {}
    return [
        {
            "key": key,
            "label": label,
            "kind": kind,
            "value": _get(req, key),
            "source": src.get(key) or "",
            # mock 兜底编造的字段视同推断：重点标注、强制确认
            "inferred": src.get(key) in (SOURCE_INFERRED, SOURCE_MOCK),
        }
        for key, label, kind in REQUIREMENT_FIELD_SPEC
    ]


def inferred_fields(sources: dict[str, str] | None) -> list[str]:
    """需要人工确认的字段：AI 推断的 + mock 兜底编造的（都不是用户原话依据）。"""
    return [
        k for k, v in (sources or {}).items()
        if v in (SOURCE_INFERRED, SOURCE_MOCK)
    ]


def _needs_review(state: GlobalState) -> bool:
    """是否需要人工确认。

    来源信息缺失（老会话 / 抽取未标来源）一律要求确认——宁可多问一次，也不放
    未确认内容进制图；来源明确时只有存在 AI 推断字段才要求确认（B：精准拦截）。
    """
    sources = state.get("requirement_sources") or {}
    if not sources:
        return True
    return bool(inferred_fields(sources))


def _apply_field_patch(req: dict[str, Any], fields: dict[str, Any] | None) -> dict[str, Any]:
    """把回传的点路径字段修改写进 requirement（深拷贝，不改原 dict）。"""
    out = copy.deepcopy(req)
    for key, val in (fields or {}).items():
        if not isinstance(key, str) or not key.strip():
            continue
        parts = key.split(".")
        cur = out
        for p in parts[:-1]:
            nxt = cur.get(p)
            if not isinstance(nxt, dict):
                nxt = {}
                cur[p] = nxt
            cur = nxt
        cur[parts[-1]] = val
    return out


def _parse_resume(resume: Any) -> tuple[str, dict[str, Any] | None, str | None]:
    """resume 值 → (decision 小写, fields 点路径修改, comment)。"""
    if isinstance(resume, dict):
        fields = resume.get("fields")
        return (
            str(resume.get("decision") or "").strip().lower(),
            fields if isinstance(fields, dict) else None,
            (resume.get("comment") or "").strip() or None,
        )
    return str(resume or "").strip().lower(), None, None


def review_payload(state: GlobalState) -> dict[str, Any]:
    """门禁载荷（节点与 Web 回放端点共用；不落 checkpoint，须可由 state 重算）。"""
    req = state.get("requirement") or empty_requirement()
    sources = state.get("requirement_sources") or {}
    return {
        "type": "requirement_review",
        "requirement": req,
        "fields": requirement_fields(req, sources),
        "inferred_fields": inferred_fields(sources),
        "missing_fields": state.get("missing_fields") or [],
        "mode": "with_code" if has_project_code(req) else "no_code",
        "summary": (
            "请逐项核对需求字段（AI 推断的字段重点看）：确认后进入制图；"
            "驳回则本轮结束，补充需求后重新澄清"
        ),
    }


def requirement_review_node(state: GlobalState) -> dict[str, Any]:
    """需求确认中断：展示全字段，等用户确认（可带修改）或驳回。"""
    # 放行条件（B）：① 已确认过（需求有变化时抽取节点会重置该标记）；
    # ② 字段来源明确且没有被 AI 推断的字段——全部字段都有用户原话依据，无需评审。
    if state.get("requirement_confirmed") or not _needs_review(state):
        return {"current_stage": "graph", "requirement_confirmed": True}

    decision, fields, comment = _parse_resume(interrupt(review_payload(state)))
    req = state.get("requirement") or empty_requirement()

    if decision in _REJECT_WORDS:
        hint = f"（你的意见：{comment}）" if comment else ""
        return {
            "current_stage": "clarify",
            "requirement_confirmed": False,
            "messages": [AIMessage(content=(
                "需求尚未确认，本轮流程到此结束。" + hint
                + "请继续补充或修正需求描述（例如输入输出约束、边界场景、验收标准），"
                "我会重新抽取并在制图前再请你确认。"
            ))],
        }

    merged = _apply_field_patch(req, fields)
    out: dict[str, Any] = {
        "requirement": merged,
        "requirement_confirmed": True,
        "current_stage": "graph",
    }
    if fields:
        # 用户亲手改过的字段 = 已由用户确认，来源改写为 user（B 用）
        sources = dict(state.get("requirement_sources") or {})
        for key in fields:
            sources[key] = SOURCE_USER
        out["requirement_sources"] = sources
    return out


def route_after_requirement_review(state: GlobalState) -> str:
    """确认 → 选图种类；驳回 → 本轮结束（等用户补充需求）。"""
    return "confirmed" if state.get("requirement_confirmed") else "rejected"
