"""Checklist 库数据模型：scenario.md 解析结果、路由候选树、LLM 路由/沉淀输出。"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


# ═══════════════════════════════════════════════════════════════════
# 库文件解析模型
# ═══════════════════════════════════════════════════════════════════


class ScenarioRef(BaseModel):
    """scenario.md frontmatter 里的一条 reference（指向子业务目录的说明）。"""

    path: str = Field(description="子业务相对路径，如 refund 或 refund/dispute")
    desc: str = Field(default="", description="子业务一句话说明")


class ScenarioDoc(BaseModel):
    """scenario.md 解析结果：frontmatter 路由标签 + 正文使用场景。

    路由阶段只消费 name/description/keywords（渐进披露，正文不进上下文）。
    """

    rel_dir: str = Field(description="相对库根的目录路径，如 payment 或 payment/refund")
    name: str = Field(default="", description="业务中文名；frontmatter 缺省时回退目录名")
    description: str = Field(default="", description="一句话路由描述：什么需求该路由到这里")
    keywords: list[str] = Field(default_factory=list)
    references: list[ScenarioRef] = Field(default_factory=list)
    usage: str = Field(default="", description="frontmatter 之后的正文（使用场景）")


# ═══════════════════════════════════════════════════════════════════
# 路由：LLM 输出 + 候选树（门禁 UI 数据）
# ═══════════════════════════════════════════════════════════════════


class RouteMatchBusiness(BaseModel):
    """LLM 匹配到的一个业务类型。"""

    name: str = Field(description="业务类型英文目录名")
    reason: str = Field(default="", description="命中原因（一句话，展示给用户）")


class RouteMatchSub(BaseModel):
    """LLM 匹配到一个业务下的子业务。"""

    business: str = Field(description="所属业务类型英文目录名")
    name: str = Field(description="子业务相对路径（相对业务目录），如 refund")
    reason: str = Field(default="")


class RouteMatch(BaseModel):
    """路由 LLM 的结构化输出；businesses 为空 = 无匹配（静默跳过）。"""

    businesses: list[RouteMatchBusiness] = Field(default_factory=list)
    sub_businesses: list[RouteMatchSub] = Field(default_factory=list)


class RouteCandidate(BaseModel):
    """路由候选树节点：门禁确认卡的渲染数据。

    suggested=True 表示 AI 预选（用户可取消勾选）；children 为该业务下
    匹配到的子业务（rel_dir 已含业务前缀，如 payment/refund）。
    """

    rel_dir: str
    name: str = ""
    description: str = ""
    keywords: list[str] = Field(default_factory=list)
    has_checklist: bool = False
    suggested: bool = False
    reason: str = ""
    children: list["RouteCandidate"] = Field(default_factory=list)


# ═══════════════════════════════════════════════════════════════════
# 沉淀：LLM 输出（用例 → checklist.md / scenario.md）
# ═══════════════════════════════════════════════════════════════════


class DistillItem(BaseModel):
    """清单条目：一句话可验证的检查点 + 建议优先级。"""

    priority: str = Field(default="P1", description="P0/P1/P2")
    text: str = Field(description="检查点描述（可验证的一句话）")


class DistillSection(BaseModel):
    """按用例设计维度分节：正向/反向/边界值/等价类/状态流转/场景法/安全/性能。"""

    category: str
    items: list[DistillItem] = Field(default_factory=list)


class DistillScenario(BaseModel):
    """沉淀产出的 scenario.md frontmatter + 使用场景。"""

    name: str = Field(description="业务中文名")
    description: str = Field(description="一句话路由描述：什么需求应路由到此清单")
    keywords: list[str] = Field(default_factory=list)
    usage: str = Field(default="", description="使用场景段落（何时用、典型需求样例）")


class DistillOutput(BaseModel):
    """沉淀 LLM 的结构化输出，由 render_* 渲染成 markdown 落盘。"""

    scenario: DistillScenario
    sections: list[DistillSection] = Field(default_factory=list)
    merge_notes: str = Field(
        default="", description="merge 模式下的合并说明（保留/新增/改写了什么）"
    )


# checklist.md 合法分节（顺序即渲染顺序；超出该集合的 category 归入「场景法」）
CHECKLIST_CATEGORIES: list[str] = [
    "正向",
    "反向",
    "边界值",
    "等价类",
    "状态流转",
    "场景法",
    "安全",
    "性能",
]

# 用例 tier → 清单分节 的映射兜底（test_report.scenarios[].tier）
TIER_TO_CATEGORY: dict[str, str] = {
    "functional": "正向",
    "performance": "性能",
    "security": "安全",
}


def normalize_category(raw: str) -> str:
    """LLM 输出的 category 归一化到合法分节集合。"""
    raw = str(raw or "").strip()
    for cat in CHECKLIST_CATEGORIES:
        if cat in raw:
            return cat
    return TIER_TO_CATEGORY.get(raw.lower(), "场景法") or "场景法"


__all__ = [
    "CHECKLIST_CATEGORIES",
    "ChecklistSection",
    "DistillItem",
    "DistillOutput",
    "DistillScenario",
    "DistillSection",
    "RouteCandidate",
    "RouteMatch",
    "RouteMatchBusiness",
    "RouteMatchSub",
    "ScenarioDoc",
    "ScenarioRef",
    "TIER_TO_CATEGORY",
    "normalize_category",
]
