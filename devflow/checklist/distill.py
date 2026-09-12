"""沉淀：评审有效的用例 → checklist.md / scenario.md（登记入库）。

create 模式：新业务目录，生成 scenario frontmatter + 全新清单；
merge  模式：目录已存在，把旧清单原文与新用例一起交给 LLM 去重合并，
写盘前先整段预览（Web 端确认后调 write_checklist）。
"""
from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any, Optional

from ..llm_client import invoke_json
from .library import parse_frontmatter
from .models import (
    CHECKLIST_CATEGORIES,
    DistillOutput,
    normalize_category,
)

logger = logging.getLogger(__name__)

DISTILL_SYSTEM_PROMPT = f"""你是测试资产沉淀专家。给定一组「评审确认有效」的测试用例，
把它们归纳为一份可复用的业务检查清单（checklist），供日后同类需求的测试设计做参照。

清单设计规则：
- 条目是「可验证的一句话检查点」，不是用例原文复制：去掉具体测试数据，保留业务规则与验证点；
- 同类检查点去重合并；按分节归类：{'、'.join(CHECKLIST_CATEGORIES)}；
- 每条带建议优先级 P0（资损/主流程/安全）/ P1（重要分支与边界）/ P2（体验与次要）；
- scenario 的 description 是路由标签：写清「什么需求应该路由到这份清单」，
  供日后路由器做语义匹配，要含业务关键词；
- merge 模式会同时给出已有清单，保留其中仍成立的条目、吸收新条目、剔除过时条目，
  并在 merge_notes 里简述合并动作。"""

DISTILL_USER_TEMPLATE = """【业务归属】
{business}

【已有清单】
{existing}

【待沉淀的有效用例】
{cases}

请输出归纳结果（JSON）。"""


# 文档导入 / 手写清单规范化：来源是既有文档而非本会话用例
DOC_DISTILL_SYSTEM_PROMPT = DISTILL_SYSTEM_PROMPT + """
- 来源文档可能是检查清单、测试用例文档、验收标准或 wiki 页面的混合体：
  只提取「可作为测试检查点」的业务规则与验证点，丢弃操作指引与无关细节；
  来源已是规范条目的优先保留原文措辞，不做无谓改写。"""

DOC_DISTILL_USER_TEMPLATE = """【业务归属】
{business}

【已有清单】
{existing}

【来源文档】
{doc}

请输出归纳结果（JSON）。"""


def _existing_text(existing_scenario_md: str, existing_checklist_md: str) -> str:
    parts: list[str] = []
    if existing_scenario_md.strip():
        parts.append("— scenario.md —\n" + existing_scenario_md)
    if existing_checklist_md.strip():
        parts.append("— checklist.md —\n" + existing_checklist_md)
    return "\n\n".join(parts) if parts else "（无，首次创建）"


def _business_desc(business: dict[str, Any]) -> str:
    return json.dumps(business, ensure_ascii=False) if business else "（未指定，请从输入推断）"


def _normalize(out: DistillOutput) -> DistillOutput:
    for section in out.sections:
        section.category = normalize_category(section.category)
    return out


def _format_case(case: dict[str, Any]) -> str:
    steps = case.get("steps")
    if isinstance(steps, list):
        steps = " → ".join(str(s) for s in steps)
    fields = [
        f"用例 {case.get('case_id', '?')} [{case.get('priority', 'P1')}] "
        f"{case.get('title', '')}（{case.get('case_type', '')}）",
        f"  前置: {case.get('precondition', '—')}",
        f"  步骤: {steps or '—'}",
        f"  预期: {case.get('expected', '—')}",
    ]
    if case.get("rationale"):
        fields.append(f"  依据: {case['rationale']}")
    return "\n".join(fields)


async def distill_from_cases(
    cases: list[dict[str, Any]],
    business: dict[str, Any],
    existing_scenario_md: str = "",
    existing_checklist_md: str = "",
) -> DistillOutput:
    """LLM 归纳。business: {rel_dir, name?, description?}（用户标记的业务类型）。

    失败向上抛（Web 端转 4xx/5xx 提示），不静默——沉淀是显式动作，失败要可见。
    """
    user_prompt = DISTILL_USER_TEMPLATE.format(
        business=_business_desc(business),
        existing=_existing_text(existing_scenario_md, existing_checklist_md),
        cases="\n\n".join(_format_case(c) for c in cases),
    )
    raw = await invoke_json(
        DISTILL_SYSTEM_PROMPT,
        user_prompt,
        response_model=DistillOutput,
        response_type="checklist_distill",
    )
    return _normalize(DistillOutput.model_validate(raw))


async def distill_from_doc(
    doc_text: str,
    business: dict[str, Any],
    existing_scenario_md: str = "",
    existing_checklist_md: str = "",
) -> DistillOutput:
    """从外部文档归纳清单（上传的 wiki/清单/用例文档，或用户手写内容的规范化）。

    与 distill_from_cases 同构：同样产出 scenario.md + checklist.md 预览，
    已有清单时 merge 去重。失败向上抛不静默。
    """
    user_prompt = DOC_DISTILL_USER_TEMPLATE.format(
        business=_business_desc(business),
        existing=_existing_text(existing_scenario_md, existing_checklist_md),
        doc=doc_text,
    )
    raw = await invoke_json(
        DOC_DISTILL_SYSTEM_PROMPT,
        user_prompt,
        response_model=DistillOutput,
        response_type="checklist_distill",
    )
    return _normalize(DistillOutput.model_validate(raw))


# ═══════════════════════════════════════════════════════════════════
# 渲染（DistillOutput → markdown，与库文件规范一一对应）
# ═══════════════════════════════════════════════════════════════════


def render_scenario_md(out: DistillOutput, references: Optional[list[dict[str, str]]] = None) -> str:
    meta: dict[str, Any] = {
        "name": out.scenario.name,
        "description": out.scenario.description,
        "keywords": out.scenario.keywords,
    }
    if references:
        meta["references"] = references
    lines = ["---", yaml_dump(meta), "---", "", "## 使用场景", ""]
    usage = out.scenario.usage.strip() or "（待补充：何时使用这份清单、典型需求样例）"
    lines.extend([usage, ""])
    return "\n".join(lines)


def render_checklist_md(
    out: DistillOutput,
    *,
    business: str,
    sources: list[str],
) -> str:
    meta = {
        "name": out.scenario.name,
        "business": business,
        "updated": date.today().isoformat(),
        "sources": sources,
    }
    lines = ["---", yaml_dump(meta), "---", ""]
    if out.merge_notes.strip():
        lines.extend([f"> 合并说明：{out.merge_notes.strip()}", ""])
    for section in out.sections:
        if not section.items:
            continue
        # 渲染层强制归一化分节：无论 LLM 输出如何，落盘文件永远符合库规范
        lines.extend([f"## {normalize_category(section.category)}", ""])
        for item in section.items:
            priority = item.priority if item.priority in ("P0", "P1", "P2") else "P1"
            lines.append(f"- [{priority}] {item.text.strip()}")
        lines.append("")
    return "\n".join(lines)


def yaml_dump(meta: dict[str, Any]) -> str:
    """轻量 YAML 序列化（keys 固定、value 简单），避免引钥风格漂移。"""
    import yaml

    return yaml.safe_dump(
        meta, allow_unicode=True, default_flow_style=False, sort_keys=True
    ).strip()


def parse_checklist_meta(checklist_md: str) -> dict[str, Any]:
    """读取 checklist.md frontmatter（merge 前展示既有 sources 用）。"""
    meta, _ = parse_frontmatter(checklist_md)
    return meta


def merge_sources(existing_checklist_md: str, new_source: str) -> list[str]:
    """合并溯源列表：既有 sources 保序 + 追加本次来源（去重）。

    既有条目可以是会话 id，也可以是 "import:<文件名>"（文档导入）。
    修复 merge 模式丢失旧 sources 的问题——溯源要跨次累积。
    """
    meta = parse_checklist_meta(existing_checklist_md) if existing_checklist_md.strip() else {}
    raw = meta.get("sources")
    out: list[str] = []
    for s in raw if isinstance(raw, list) else []:
        s = str(s).strip()
        if s and s not in out:
            out.append(s)
    new_source = str(new_source or "").strip()
    if new_source and new_source not in out:
        out.append(new_source)
    return out


__all__ = [
    "distill_from_cases",
    "distill_from_doc",
    "merge_sources",
    "parse_checklist_meta",
    "render_checklist_md",
    "render_scenario_md",
    "yaml_dump",
]
