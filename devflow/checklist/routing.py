"""路由匹配：需求文本 → 业务/子业务（LLM 一次调用，目录摘要在前）。

路由只喂给 LLM 各 scenario.md 的 frontmatter 摘要（name/description/keywords），
checklist.md 正文在用户确认后才加载——与 skill 的渐进披露同一思路，token 成本
与库规模解耦。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from ..llm_client import invoke_json
from .library import catalog_for_routing
from .models import RouteMatch

logger = logging.getLogger(__name__)

ROUTE_SYSTEM_PROMPT = """你是测试用例生成管线的「业务清单路由器」。

给定一段软件需求描述和业务清单目录（每个业务类型带 description/keywords，
其下可挂子业务），请选出与该需求真正相关的业务类型与子业务。

判定规则：
- 只选确实会影响到清单所覆盖业务的需求；按关键词、业务动作、数据对象综合判断；
- 宁缺毋滥：不确定相关的业务不要选，选了会往测试设计里注入噪音；
- 子业务同理：需求明确触及子业务范围（如「退款」「争议」）才选；
- 需求与任何业务都不相关时，businesses 返回空数组——这是正常结果，不要硬凑。
- reason 用一句话中文说明命中依据。"""

ROUTE_USER_TEMPLATE = """【需求描述】
{requirement}

【业务清单目录】
{catalog}

请输出匹配结果（JSON）。"""


def summarize_requirement(req: dict[str, Any]) -> str:
    """requirement schema → 路由用的扁平文本（复用已有字段，不再调 LLM）。"""
    parts: list[str] = []
    if req.get("project_context"):
        parts.append(str(req["project_context"]))
    io = req.get("io_constraints") or {}
    if isinstance(io, dict):
        if io.get("input"):
            parts.append(f"输入: {io['input']}")
        if io.get("output"):
            parts.append(f"输出: {io['output']}")
    if req.get("target_modules"):
        parts.append("目标模块: " + ", ".join(str(m) for m in req["target_modules"] if m))
    if req.get("edge_cases"):
        parts.append("边界关注: " + ", ".join(str(e) for e in req["edge_cases"] if e))
    if req.get("acceptance_criteria"):
        parts.append("验收标准: " + "; ".join(str(a) for a in req["acceptance_criteria"] if a))
    return "\n".join(p for p in parts if p)


async def match_businesses(requirement_text: str, root) -> RouteMatch:
    """LLM 路由匹配。库为空直接返回空匹配；LLM 失败/输出非法也按无匹配兜底
    （路由是增强路径，失败不能阻断用例生成）。"""
    catalog = catalog_for_routing(root)
    if not catalog or not requirement_text.strip():
        return RouteMatch()
    user_prompt = ROUTE_USER_TEMPLATE.format(
        requirement=requirement_text,
        catalog=json.dumps(catalog, ensure_ascii=False, indent=2),
    )
    try:
        raw = await invoke_json(
            ROUTE_SYSTEM_PROMPT,
            user_prompt,
            response_model=RouteMatch,
            response_type="checklist_route",
        )
        return RouteMatch.model_validate(raw)
    except Exception as e:  # noqa: BLE001 — 路由失败静默降级，不阻断用例生成
        logger.warning("checklist 路由匹配失败，按无匹配处理: %s", e)
        return RouteMatch()


__all__ = ["match_businesses", "summarize_requirement"]
