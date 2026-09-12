"""逻辑图种类注册表 + 需求关键字推断。

制图前由 graph_type_select 门禁把候选种类（含推荐标记）抛给用户选择；
本模块是种类定义的唯一来源（id / 展示名 / mermaid 声明头 / 推断关键词）。

五种候选全部受支持：生成、语法修复、结构化校验、前端渲染全链路可用。
推断是纯规则匹配（不调 LLM）：对需求文本字段做关键词命中计数，
flowchart 恒为默认推荐；其余种类命中 ≥2 个不同关键词才标记推荐。
"""
from __future__ import annotations

from typing import Any

# ── 种类注册表（id 顺序即候选卡展示顺序） ─────────────────────
GRAPH_TYPES: list[dict[str, str]] = [
    {
        "id": "flowchart",
        "label": "流程图",
        "header": "flowchart TD",
        "desc": "处理流程与分支（默认）：输入 → 核心处理 → 输出，标注本次改动点",
        "keywords": [],
    },
    {
        "id": "sequence",
        "label": "时序图",
        "header": "sequenceDiagram",
        "desc": "参与者之间的调用顺序：适合接口交互、服务调用链、回调与异步消息",
        "keywords": [
            "接口", "调用", "请求", "响应", "api", "API", "sdk", "SDK",
            "时序", "链路", "回调", "消息", "推送", "第三方", "上游", "下游",
            "客户端", "服务端", "前后端",
        ],
    },
    {
        "id": "state",
        "label": "状态图",
        "header": "stateDiagram-v2",
        "desc": "对象状态流转：适合订单/审批/任务等有生命周期与异常迁移的需求",
        "keywords": [
            "状态", "流转", "生命周期", "状态机", "审批", "工单", "订单",
            "待支付", "已支付", "待审核", "已取消", "已完结", "失效", "激活",
            "冻结", "停用", "启用",
        ],
    },
    {
        "id": "er",
        "label": "ER 图",
        "header": "erDiagram",
        "desc": "数据实体与关系：适合建表、加字段、数据模型调整类需求",
        "keywords": [
            "数据库", "表结构", "建表", "字段", "存储", "实体", "数据模型",
            "持久化", "ORM", "orm", "迁移", "主键", "外键", "索引", "schema",
        ],
    },
    {
        "id": "journey",
        "label": "用户旅程图",
        "header": "journey",
        "desc": "用户视角的阶段旅程与满意度：适合多角色多步骤操作流程、体验与痛点分析",
        "keywords": [
            "用户旅程", "旅程", "体验", "用户体验", "触点", "满意度", "痛点",
            "角色", "用户流程", "操作步骤", "使用流程", "旅程图", "journey",
        ],
    },
]

GRAPH_TYPE_IDS: set[str] = {t["id"] for t in GRAPH_TYPES}
DEFAULT_GRAPH_TYPE = "flowchart"

# 推荐阈值：命中的不同关键词数 ≥ 2 才推荐（1 个太容易误报）
_RECOMMEND_THRESHOLD = 2


def graph_type_meta(graph_type: str) -> dict[str, str]:
    """按 id 取种类定义；未知 id 回退默认种类。"""
    for t in GRAPH_TYPES:
        if t["id"] == graph_type:
            return t
    return next(t for t in GRAPH_TYPES if t["id"] == DEFAULT_GRAPH_TYPE)


def suggest_graph_types(requirement: dict[str, Any] | None) -> list[dict[str, Any]]:
    """从需求文本推断候选种类与推荐标记（纯规则，不调 LLM）。

    计分口径：project_context / io_constraints / target_modules / edge_cases /
    acceptance_criteria 拼成一个文本块，统计每个种类的「不同关键词命中数」。
    返回注册表顺序的候选列表（flowchart 恒推荐）；命中的把推荐理由一并带回。
    """
    req = requirement or {}
    parts: list[str] = [
        str(req.get("project_context") or ""),
        str(req.get("req_type") or ""),
    ]
    io = req.get("io_constraints") or {}
    if isinstance(io, dict):
        parts += [str(io.get("input") or ""), str(io.get("output") or "")]
    for key in ("target_modules", "edge_cases", "acceptance_criteria", "reference_files"):
        val = req.get(key) or []
        if isinstance(val, list):
            parts += [str(v) for v in val]
    blob = "\n".join(parts)

    out: list[dict[str, Any]] = []
    for t in GRAPH_TYPES:
        hits = sorted({kw for kw in t["keywords"] if kw and kw in blob})
        recommended = t["id"] == DEFAULT_GRAPH_TYPE or len(hits) >= _RECOMMEND_THRESHOLD
        reason = "默认 · 通用处理流程" if t["id"] == DEFAULT_GRAPH_TYPE else ""
        if t["id"] != DEFAULT_GRAPH_TYPE and recommended:
            reason = "需求文本命中：" + "、".join(hits[:4]) + (" 等" if len(hits) > 4 else "")
        out.append(
            {
                "id": t["id"],
                "label": t["label"],
                "desc": t["desc"],
                "recommended": recommended,
                "reason": reason,
                "hit_count": len(hits),
            }
        )
    # 命中数高的非默认种类排前面（flowchart 恒在第一位作为默认）
    out[1:] = sorted(out[1:], key=lambda c: -c["hit_count"])
    return out


def normalize_graph_type(value: Any) -> str:
    """任意来源（用户输入 / resume 值 / 旧数据）→ 合法种类 id；未知回退默认。"""
    v = str(value or "").strip().lower()
    if v in GRAPH_TYPE_IDS:
        return v
    # 容错：中文名 / 常见别名
    aliases = {
        "流程图": "flowchart", "flow": "flowchart", "flow-chart": "flowchart",
        "时序图": "sequence", "时序": "sequence", "sequence": "sequence", "seq": "sequence",
        "状态图": "state", "状态": "state", "state": "state",
        "er": "er", "er图": "er", "er 图": "er",
        "旅程图": "journey", "用户旅程图": "journey", "用户旅程": "journey",
        "journey": "journey", "user journey": "journey",
    }
    return aliases.get(v, DEFAULT_GRAPH_TYPE)
