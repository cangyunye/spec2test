"""测试设计 feature 拆分与合并（feature 模式的纯逻辑层）。

职责（节点层在 nodes/feature_split.py，provider 交互在 provider_nodes.py）：

  拆分（map 前）
    - cluster_nodes_by_target   代码模式：逻辑图节点按 code_ref.file_path / 目标模块确定性聚类
    - llm_name_features         代码模式：LLM 仅负责给聚类结果命名（失败退标签）
    - llm_split_features        仅需求模式：LLM + JSON Schema 拆解，归入 criteria/edge_cases
    - assign_uncovered          覆盖自检：未归属准则归入 F0「综合与集成」，不凭空丢失
    - clamp_features            超过 TEST_DESIGN_MAX_FEATURES 时尾部并入 F0

  评审与合并（reduce）
    - validate_feature_report   单 feature 确定性校验（准则归属 / ≥1 正向 / 条数异常）
    - merge_feature_reports     合并：title 归一去重 + case_id 全局稳定重编 + 自检 union

  技能派发
    - resolve_skill_paths       本仓 .agents/skills/<name>/SKILL.md 绝对路径解析
    - skill_dispatch_prefix     「先完整读取并严格遵守 <abs path>」prompt 前缀
    - SKILL_ENVELOPE            统一输出契约（技能与解析器共用的同一份 schema 说明）

  交付
    - render_test_design_plan   面向测试工程师的 test-design-plan 文档（Markdown）
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .llm_client import invoke_json

logger = logging.getLogger(__name__)

# F0 保留给「综合与集成」功能点：拆分阶段未归属的准则、跨功能端到端场景、
# 没有任何 feature 认领的清单规则都归这里，保证覆盖自检"必归属"。
F0_ID = "F0"
F0_NAME = "综合与集成场景"


# ═══════════════════════════════════════════════════════════════════
# Feature 构造与规整
# ═══════════════════════════════════════════════════════════════════

def make_feature(
    feature_id: str,
    name: str,
    description: str = "",
    *,
    target_modules: list[str] | None = None,
    acceptance_criteria: list[str] | None = None,
    edge_cases: list[str] | None = None,
    node_ids: list[str] | None = None,
    is_modified: bool = False,
) -> dict[str, Any]:
    return {
        "feature_id": feature_id,
        "name": name or feature_id,
        "description": description or "",
        "target_modules": [m for m in (target_modules or []) if m],
        "acceptance_criteria": [c for c in (acceptance_criteria or []) if c],
        "edge_cases": [c for c in (edge_cases or []) if c],
        "node_ids": [n for n in (node_ids or []) if n],
        "is_modified": bool(is_modified),
    }


def make_f0(description: str = "", **kw: Any) -> dict[str, Any]:
    return make_feature(
        F0_ID, F0_NAME,
        description or "跨功能点端到端场景与未归属准则的兜底功能点",
        **kw,
    )


def renumber_features(features: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按出现顺序重编 feature_id：F1..Fn；F0（若存在）保持末位且编号不变。"""
    f0 = next((f for f in features if f.get("feature_id") == F0_ID), None)
    rest = [f for f in features if f.get("feature_id") != F0_ID]
    out = []
    for i, f in enumerate(rest, start=1):
        out.append({**f, "feature_id": f"F{i}"})
    if f0 is not None:
        out.append({**f0, "feature_id": F0_ID})
    return out


def clamp_features(
    features: list[dict[str, Any]], max_n: int
) -> list[dict[str, Any]]:
    """超过上限时把尾部功能点并入 F0（或自身合并），准则不丢。

    预算：保留 keep 个常规功能点 + 恰好 1 个 F0（已有的或新建的）≤ max_n。
    """
    if max_n <= 0 or len(features) <= max_n:
        return list(features)
    ordered = renumber_features(features)
    f0 = next((f for f in ordered if f.get("feature_id") == F0_ID), None)
    rest = [f for f in ordered if f.get("feature_id") != F0_ID]
    keep_n = max(1, max_n - 1)
    keep, overflow = rest[:keep_n], rest[keep_n:]
    merged_desc = "；".join(
        f"{f.get('feature_id')} {f.get('name', '')}" for f in overflow
    )
    if f0 is None:
        f0 = make_f0(f"超出 TEST_DESIGN_MAX_FEATURES 上限并入：{merged_desc}")
    else:
        f0 = {
            **f0,
            "description": (f0.get("description", "") + f"；并入：{merged_desc}").strip("；"),
        }
    for f in overflow:
        f0["acceptance_criteria"] += f.get("acceptance_criteria") or []
        f0["edge_cases"] += f.get("edge_cases") or []
        f0["target_modules"] += f.get("target_modules") or []
        f0["node_ids"] += f.get("node_ids") or []
    return renumber_features(keep + [f0])


# ═══════════════════════════════════════════════════════════════════
# 拆分：代码模式确定性聚类 / 仅需求模式 LLM 拆解
# ═══════════════════════════════════════════════════════════════════

def cluster_nodes_by_target(
    logic_graph: dict[str, Any] | None,
    requirement: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """按 code_ref.file_path（无则按需求目标模块关键词，再退 node_type）聚类逻辑图节点。

    确定性：同输入同输出，不依赖模型。返回按序的 feature dict（feature_id 暂空，
    由 renumber_features 补齐）。
    """
    nodes = [n for n in ((logic_graph or {}).get("nodes") or []) if isinstance(n, dict)]
    if not nodes:
        return []
    target_modules = [
        str(m) for m in ((requirement or {}).get("target_modules") or []) if m
    ]
    groups: dict[str, dict[str, Any]] = {}

    def _group_key(node: dict[str, Any]) -> str:
        ref = node.get("code_ref") or {}
        fp = str(ref.get("file_path") or "").strip()
        if fp:
            return fp
        label = str(node.get("label") or "")
        for m in target_modules:
            if m and (m in label or label in m):
                return f"module:{m}"
        return f"type:{node.get('node_type') or 'module'}"

    for n in nodes:
        key = _group_key(n)
        g = groups.setdefault(key, {"label": str(n.get("label") or key), "node_ids": []})
        if n.get("node_id"):
            g["node_ids"].append(str(n["node_id"]))
        # 组名取第一个节点标签（LLM 命名失败时的兜底）
    out = []
    for key, g in groups.items():
        name_source = key.split(":", 1)[-1] if key.startswith(("module:", "type:")) else key
        out.append(make_feature(
            "",
            str(name_source).replace("\\", "/").rsplit("/", 1)[-1] or g["label"],
            f"覆盖逻辑图节点：{', '.join(g['node_ids'])}",
            target_modules=[key[7:]] if key.startswith("module:") else [],
            node_ids=g["node_ids"],
        ))
    return renumber_features(out)


class _FeatureNames(BaseModel):
    names: list[str] = Field(description="与输入顺序一一对应的功能点命名")


async def llm_name_features(features: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """代码模式：LLM 仅给聚类命名（节点归属已是确定性结果）。失败静默保留原名。"""
    if len(features) <= 1:
        return features
    meta: dict[str, Any] = {}
    listing = "\n".join(
        f"{i + 1}. {f.get('name', '')}（节点：{', '.join(f.get('node_ids') or []) or '无'}）"
        f" 描述：{f.get('description', '')}"
        for i, f in enumerate(features)
    )
    try:
        raw = await invoke_json(
            "你是测试设计拆分助手。给下列按代码结构聚类的功能点起简洁的业务化名称"
            "（每个不超过 12 个字，体现该组节点共同实现的业务能力）。",
            f"【功能点列表】\n{listing}\n\n请输出命名（JSON，顺序与输入一一对应）。",
            response_model=_FeatureNames,
            response_type="feature_name",
            meta=meta,
        )
        if meta.get("mock"):
            return features  # mock 兜底的编造命名不如确定性标签诚实
        names = raw.get("names") if isinstance(raw, dict) else None
        if not isinstance(names, list) or len(names) != len(features):
            return features
        return [
            {**f, "name": str(names[i]) or f.get("name", "")}
            for i, f in enumerate(features)
        ]
    except Exception as e:  # noqa: BLE001 — 命名是增强路径，失败不阻断拆分
        logger.warning("feature LLM 命名失败，退回节点标签: %s", e)
        return features


class _FeatureOut(BaseModel):
    name: str = Field(description="功能点名称（简洁业务化）")
    description: str = Field(default="", description="功能点职责一句话说明")
    target_modules: list[str] = Field(default_factory=list, description="涉及的模块/页面/子系统")
    acceptance_criteria: list[str] = Field(default_factory=list, description="归属该功能点的验收标准（原文）")
    edge_cases: list[str] = Field(default_factory=list, description="归属该功能点的边界场景（原文）")
    node_ids: list[str] = Field(default_factory=list, description="相关逻辑图节点 id（没有可空）")


class _QuestionOut(BaseModel):
    question: str = Field(description="拆分阶段无法自决、需要用户确认的问题")
    options: list[str] = Field(default_factory=list, description="候选选项（2~4 个）")
    recommended: str = Field(default="", description="推荐选项（用户不回答时按此继续）")
    feature_ids: list[str] = Field(default_factory=list, description="问题影响的功能点（如 [\"F1\",\"F3\"]）")


class _FeatureSplit(BaseModel):
    features: list[_FeatureOut] = Field(description="功能点拆分结果")
    open_questions: list[_QuestionOut] = Field(default_factory=list, description="待用户确认的问题（没有则空）")
    assumptions: list[str] = Field(default_factory=list, description="拆分时做的假设（用户未确认前按此执行）")


SPLIT_SYSTEM_PROMPT = """你是测试需求拆分师。把一段软件需求拆成 2~6 个可独立设计测试用例的功能点（feature）。

## 拆分规则
1. 每个功能点右尺寸：一个功能点约对应 5~15 条测试用例的测试面，不再细拆到单条校验。
2. 必归属：需求里的每条验收标准、每个边界场景必须归属到恰好一个功能点
   （原文填入 acceptance_criteria / edge_cases，不要改写）；与功能点无关的
   不要硬塞，留给系统兜底归入综合功能点。
3. 无占位：功能点必须有真实的业务含义与明确的测试面，禁止「其他」「待定」类占位。
4. 不许停下提问：拆分依据不足时不要向用户追问，把不确定项写进 open_questions
   （含 options 与 recommended）与 assumptions，按假设继续拆分。
5. 跨功能点的端到端场景不要单独成组——由系统在合并阶段补充综合功能点。"""


async def llm_split_features(
    requirement: dict[str, Any] | None,
    logic_graph: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]] | None:
    """仅需求模式：LLM + JSON Schema 拆解。

    返回 (features, questions, assumptions)；LLM 不可用（含 mock 兜底）返回 None，
    由调用方走单 feature 降级——mock 的编造拆分会让演示流程失真。
    """
    req = requirement or {}
    parts: list[str] = []
    if (req.get("project_context") or "").strip():
        parts.append(f"项目背景：{req['project_context']}")
    io = req.get("io_constraints") or {}
    if (io.get("input") or "").strip() or (io.get("output") or "").strip():
        parts.append(f"输入约束：{io.get('input', '')}；输出约束：{io.get('output', '')}")
    if req.get("target_modules"):
        parts.append("目标模块：" + "、".join(str(m) for m in req["target_modules"] if m))
    for ac in req.get("acceptance_criteria") or []:
        parts.append(f"验收标准：{ac}")
    for ec in req.get("edge_cases") or []:
        parts.append(f"边界场景：{ec}")
    nodes = [n for n in ((logic_graph or {}).get("nodes") or []) if isinstance(n, dict)]
    if nodes:
        parts.append("逻辑图节点：" + "；".join(
            f"{n.get('node_id')}({n.get('label')})" for n in nodes if n.get("node_id")
        ))
    if not parts:
        return None
    meta: dict[str, Any] = {}
    try:
        raw = await invoke_json(
            SPLIT_SYSTEM_PROMPT,
            "【需求要点】\n" + "\n".join(parts) + "\n\n请输出功能点拆分结果（JSON）。",
            response_model=_FeatureSplit,
            response_type="feature_split",
            meta=meta,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("feature LLM 拆分失败: %s", e)
        return None
    if meta.get("mock") or not isinstance(raw, dict):
        return None
    features = [
        make_feature(
            "",
            str(f.get("name") or ""),
            str(f.get("description") or ""),
            target_modules=[str(m) for m in (f.get("target_modules") or [])],
            acceptance_criteria=[str(c) for c in (f.get("acceptance_criteria") or [])],
            edge_cases=[str(c) for c in (f.get("edge_cases") or [])],
            node_ids=[str(n) for n in (f.get("node_ids") or [])],
        )
        for f in raw.get("features") or []
        if isinstance(f, dict) and str(f.get("name") or "").strip()
    ]
    if not features:
        return None
    questions = [q for q in (raw.get("open_questions") or []) if isinstance(q, dict)]
    assumptions = [str(a) for a in (raw.get("assumptions") or []) if a]
    return features, questions, assumptions


# ═══════════════════════════════════════════════════════════════════
# 覆盖自检：准则必归属（未归属 → F0）
# ═══════════════════════════════════════════════════════════════════

def assign_uncovered(
    features: list[dict[str, Any]],
    requirement: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """把需求准则（验收标准/边界场景）对齐到 feature；对不上的归入 F0。

    返回 (规整后的 features, gaps)。gaps 记录「原文 → 归宿」便于门禁卡展示。
    已被某 feature 声明的准则不重复分配。
    """
    assigned: set[str] = set()
    for f in features:
        for c in (f.get("acceptance_criteria") or []) + (f.get("edge_cases") or []):
            assigned.add(_norm_text(c))
    req = requirement or {}
    pool: list[tuple[str, str]] = []  # (准则原文, 类型)
    pool += [(str(c), "验收标准") for c in (req.get("acceptance_criteria") or []) if str(c).strip()]
    pool += [(str(c), "边界场景") for c in (req.get("edge_cases") or []) if str(c).strip()]

    gaps: list[str] = []
    unassigned: list[tuple[str, str]] = []
    for text, kind in pool:
        if _norm_text(text) in assigned:
            continue
        # 关键词兜底：准则文本命中某 feature 名称/模块 → 归入
        target = _match_feature_by_text(features, text)
        if target is not None:
            key = "acceptance_criteria" if kind == "验收标准" else "edge_cases"
            target[key] = list(target.get(key) or []) + [text]
            gaps.append(f"{kind}「{text}」关键词归入 {target.get('feature_id')} {target.get('name', '')}")
        else:
            unassigned.append((text, kind))
    if unassigned:
        f0 = next((f for f in features if f.get("feature_id") == F0_ID), None)
        if f0 is None:
            f0 = make_f0("拆分覆盖自检归集：未归属到具体功能点的验收标准与边界场景")
            features = features + [f0]
        for text, kind in unassigned:
            key = "acceptance_criteria" if kind == "验收标准" else "edge_cases"
            f0[key] = list(f0.get(key) or []) + [text]
            gaps.append(f"{kind}「{text}」未归属，归入 {F0_ID} 综合与集成")
    return features, gaps


def _match_feature_by_text(
    features: list[dict[str, Any]], text: str
) -> dict[str, Any] | None:
    for f in features:
        if f.get("feature_id") == F0_ID:
            continue
        hay = _norm_text(
            " ".join([str(f.get("name") or ""), *(f.get("target_modules") or [])])
        )
        needle = _norm_text(text)
        if len(needle) >= 3 and any(
            w and w in hay for w in (needle[:6], needle[:4], needle[:3])
        ):
            return f
    return None


# ═══════════════════════════════════════════════════════════════════
# reduce：单 feature 确定性评审 + 合并
# ═══════════════════════════════════════════════════════════════════

def _norm_text(s: Any) -> str:
    return re.sub(r"[\s，。；：、,.:;()（）\[\]【】\"'“”‘’!?！？\-—_/\\]", "", str(s or "")).lower()


def _case_text(case: dict[str, Any]) -> str:
    return _norm_text(" ".join(
        str(case.get(k) or "") for k in
        ("title", "case_type", "target", "precondition", "steps", "expected", "rationale")
    ))


def _criterion_covered(criterion: str, case_texts: list[str]) -> bool:
    """准则 → 用例的确定性归属判定：归一化后的前缀片段是否出现在任一用例文本中。

    配套约束（写入各 provider 提示词）：用例 rationale 必须注明
    「验收:<准则摘要>」「边界:<场景摘要>」，使该判定可依赖显式标记。
    """
    needle = _norm_text(criterion)
    if not needle:
        return True
    probes = [needle[:10], needle[:6]] if len(needle) > 6 else [needle]
    for ct in case_texts:
        if any(p and p in ct for p in probes):
            return True
    return False


def checklists_for_feature(
    checklists: list[dict[str, str]], feature: dict[str, Any] | None
) -> list[dict[str, str]]:
    """按关键词（功能点名称 / 目标模块）划清单子集，控制单 feature 上下文。

    匹配 rel_dir + name + content（正文截断 4000 字防全量扫描）；没有功能点
    信息（整单兜底）时返回空，由调用方决定全量注入。
    """
    if not checklists or not feature:
        return []
    needles = [
        _norm_text(x) for x in
        [str(feature.get("name") or ""), *(feature.get("target_modules") or [])] if x
    ]
    needles = [n for n in needles if len(n) >= 2]
    if not needles:
        return []
    out = []
    for cl in checklists:
        hay = _norm_text(" ".join([
            str(cl.get("rel_dir") or ""), str(cl.get("name") or ""),
            str(cl.get("content") or "")[:4000],
        ]))
        if any(n in hay for n in needles):
            out.append(cl)
    return out


def validate_feature_report(
    feature: dict[str, Any] | None, report: dict[str, Any]
) -> list[str]:
    """单 feature 生成结果的确定性校验。返回问题列表（空 = 通过）。"""
    cases = [c for c in (report.get("test_cases") or []) if isinstance(c, dict)]
    issues: list[str] = []
    if not cases:
        return ["未产出任何用例"]
    # 正向覆盖：至少 1 条非反向用例（case_type 未标注时视为正向缺席）
    positives = [
        c for c in cases
        if c.get("case_type") and "反向" not in str(c["case_type"])
    ]
    if not positives:
        issues.append("缺少正向用例（每功能点至少 1 条 happy path）")
    # 条数异常：单 feature 用例数爆炸通常是上下文串味
    if len(cases) > 40:
        issues.append(f"用例数异常（{len(cases)} 条），疑似混入了其他功能点的内容")
    # 准则归属：每条验收标准/边界场景都要有对应用例
    if feature is not None:
        case_texts = [_case_text(c) for c in cases]
        for c in (feature.get("acceptance_criteria") or []):
            if not _criterion_covered(str(c), case_texts):
                issues.append(f"验收标准无对应用例：「{c}」")
        for c in (feature.get("edge_cases") or []):
            if not _criterion_covered(str(c), case_texts):
                issues.append(f"边界场景无对应用例：「{c}」")
    return issues


def merge_feature_reports(
    feature_reports: list[tuple[dict[str, Any] | None, dict[str, Any]]],
    *,
    features: list[dict[str, Any]],
    requirement: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """合并逐 feature 报告：去重（title 归一）→ case_id 全局稳定重编 → 总分结构。

    feature_reports: [(feature dict 或 None(整单兜底), 单 feature 报告)]，按 feature 顺序。
    """
    req = requirement or {}
    merged_cases: list[dict[str, Any]] = []
    seen_titles: set[str] = set()
    self_check: list[str] = []
    checklist_refs: list[str] = []
    session_ids: list[str] = []
    gap_notes: list[str] = []

    def _norm_title(t: Any) -> str:
        return _norm_text(t)

    for feature, report in feature_reports:
        fid = str((feature or {}).get("feature_id") or "ALL")
        fname = str((feature or {}).get("name") or "整单")
        sid = report.get("session_id")
        if sid and sid not in session_ids:
            session_ids.append(str(sid))
        for ref in report.get("checklist_refs") or []:
            if ref and ref not in checklist_refs:
                checklist_refs.append(str(ref))
        for entry in report.get("self_check") or []:
            self_check.append(f"[{fid}] {entry}")
        for issue in report.get("open_issues") or []:
            gap_notes.append(f"[{fid}] {issue}")
        for c in report.get("test_cases") or []:
            if not isinstance(c, dict):
                continue
            tkey = _norm_title(c.get("title") or c.get("test_symbol") or "")
            if tkey and tkey in seen_titles:
                continue
            if tkey:
                seen_titles.add(tkey)
            merged_cases.append({
                **c,
                "feature_id": fid,
                "feature_name": fname,
            })

    # case_id 全局稳定重编（adopted_cases 依赖该规则的稳定性）
    for i, c in enumerate(merged_cases, start=1):
        c["case_id"] = f"TC-{i:03d}"

    fid_names = {f.get("feature_id"): f.get("name", "") for f in features}
    counts = "；".join(
        f"{fid} {name}({sum(1 for c in merged_cases if c.get('feature_id') == fid)}条)"
        for fid, name in fid_names.items()
    ) or "整单"
    overview = (
        f"本次测试设计按功能点拆分逐个设计后合并：{counts}。"
        "用例 case_id 全局连续编制（TC-001 起），每条用例标注归属功能点（feature_id）；"
        "跨功能点重复用例已按标题归一去重。"
    )
    if req.get("project_context"):
        overview = f"测试对象：{req['project_context']}。{overview}"
    if gap_notes:
        overview += f" 遗留缺口 {len(gap_notes)} 项，详见质量自检。"

    self_check.append(
        f"[MERGE] case_id 全局连续重编（TC-001 ~ TC-{len(merged_cases):03d}），"
        f"去重前共 {sum(1 for _, r in feature_reports for _ in (r.get('test_cases') or []))} 条"
    )
    self_check.extend(f"[MERGE] {g}" for g in gap_notes)

    return {
        "session_id": "feature-map:" + ",".join(session_ids[:5]) if session_ids else "feature-map",
        "test_cases": merged_cases,
        "run": {
            "passed": len(merged_cases) if merged_cases else 1,
            "failed": 0,
            "skipped": 0,
            "coverage_pct": None,
            "logs": (
                f"feature map-reduce 设计：{len(features)} 个功能点，"
                f"合并后 {len(merged_cases)} 条用例（未执行真实测试）"
            ),
        },
        "target_symbols": list(dict.fromkeys([
            m for f in features for m in (f.get("target_modules") or [])
        ])) or [f.get("name", "") for f in features],
        "overview": overview,
        "self_check": self_check,
        "checklist_refs": checklist_refs,
        "features": [dict(f) for f in features],
    }


# ═══════════════════════════════════════════════════════════════════
# 技能派发：路径解析 + prompt 前缀 + 输出契约
# ═══════════════════════════════════════════════════════════════════

SKILL_EXECUTE_NAME = "test-design-execute"
SKILL_PLAN_NAME = "test-design-plan"

# 统一输出契约（写进技能与解析器；skill_paths 派发时的 stdout JSON envelope）
SKILL_ENVELOPE = {
    "status": "ok | partial（open_issues 非空时）",
    "feature_id": "F1..Fn / F0",
    "cases": [
        {
            "tier": "functional|performance|security",
            "priority": "P0|P1|P2",
            "title": "谁在什么条件下做什么、预期什么结果",
            "case_type": "正向|反向|边界值|等价类|状态流转|场景法|性能|安全",
            "target": "所属模块/功能点",
            "precondition": "前置条件",
            "steps": "分步操作步骤",
            "expected": "可验收的预期结果",
            "rationale": "设计依据，必须注明「验收:<准则摘要>」「边界:<场景摘要>」「清单:<业务>/<条目>」",
        }
    ],
    "open_questions": [{"question": "…", "options": ["…"], "recommended": "…"}],
    "assumptions": ["…"],
    "coverage_self_check": ["逐条自检结论"],
    "open_issues": ["无法自行补齐的缺口"],
}


def resolve_skill_paths(skill_dir: Path | None = None, *names: str) -> list[str]:
    """解析本仓技能的 SKILL.md 绝对路径（存在才返回）。"""
    root = Path(skill_dir) if skill_dir else _default_skill_dir()
    out: list[str] = []
    for name in names:
        p = root / name / "SKILL.md"
        if p.is_file():
            out.append(str(p.resolve()))
    return out


def _default_skill_dir() -> Path:
    from .config import settings

    d = Path(settings.TEST_DESIGN_SKILL_DIR)
    return d if d.is_absolute() else Path(__file__).resolve().parent.parent / d


def skill_dispatch_prefix(skill_paths: list[str]) -> str:
    """派发 prompt 前缀：显式传路径并要求执行器先完整读取（核心约束）。"""
    lines = ["【任务规范（开始任何工作前必须先完整读取并严格遵守）】"]
    for p in skill_paths:
        lines.append(f"- 使用文件读取工具完整读取：{p}（这是你的任务规范，读完再动手）")
    lines.append("- 规范与本次任务冲突时，以规范为准；规范要求输出 JSON envelope 的，"
                 "最终答复必须只含该 JSON。")
    return "\n".join(lines)


def build_feature_design_prompt(
    feature: dict[str, Any],
    *,
    requirement: dict[str, Any] | None = None,
    feedback: str | None = None,
    checklists: list[dict[str, str]] | None = None,
    skill_paths: list[str] | None = None,
) -> str:
    """单功能点派发 prompt（opencode run / pi -p 技能执行器共用，契约单点维护）。

    派发时显式传 SKILL.md 绝对路径并要求先完整读取；产出走 SKILL_ENVELOPE 契约。
    """
    lines = [
        "你是测试设计执行器。只针对下面给定的单个功能点设计测试场景，"
        "不要设计其他功能点的用例；本功能点至少 1 条正向用例。",
    ]
    if skill_paths:
        lines += ["", skill_dispatch_prefix(skill_paths)]
    lines += ["", f"【当前功能点】{feature.get('feature_id')} {feature.get('name', '')}"]
    if (feature.get("description") or "").strip():
        lines.append(str(feature["description"]))
    if feature.get("target_modules"):
        lines.append(f"模块：{'、'.join(str(m) for m in feature['target_modules'])}")
    criteria = [str(c) for c in (feature.get("acceptance_criteria") or []) if c]
    edges = [str(c) for c in (feature.get("edge_cases") or []) if c]
    if criteria:
        lines.append("验收标准（逐条覆盖）：")
        lines += [f"- {c}" for c in criteria]
    if edges:
        lines.append("边界场景（逐条覆盖）：")
        lines += [f"- {c}" for c in edges]
    if str(feature.get("feature_id") or "") == F0_ID:
        siblings = "、".join(str(s) for s in (feature.get("sibling_features") or []) if s)
        lines.append(
            "本功能点是跨功能端到端综合场景：用场景法串联"
            f"（{siblings or '其他功能点'}）设计端到端用例。"
        )
    for line in feature.get("qa_decisions") or []:
        lines.append(f"拆分确认决策：{line}")
    req = requirement or {}
    if (req.get("project_context") or "").strip():
        lines.append(f"全局背景：{req['project_context']}")
    if feedback and feedback.strip():
        lines.append(f"上轮意见（必须针对性修正）：{feedback.strip()}")
    if checklists:
        lines.append("业务检查清单（逐条核对覆盖）：")
        for cl in checklists:
            label = cl.get("name") or cl.get("rel_dir") or "业务清单"
            lines.append(f"- [{label}] {str(cl.get('content', ''))[:1500]}")
    lines += [
        "",
        "归属标注：每条用例的 rationale 必须注明依据——验收标准写「验收:<准则摘要>」、"
        "边界场景写「边界:<场景摘要>」、清单规则写「清单:<业务>/<条目>」、"
        "常规用例写「功能:<功能点名称>」。",
        "",
        "最终答复只输出一个 JSON envelope（不要代码围栏、不要解释文字），结构：",
        "```json",
        json.dumps(SKILL_ENVELOPE, ensure_ascii=False, indent=2),
        "```",
    ]
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
# 交付：面向测试工程师的 test-design-plan 文档
# ═══════════════════════════════════════════════════════════════════

def render_test_design_plan(
    features: list[dict[str, Any]],
    requirement: dict[str, Any] | None = None,
    *,
    questions: list[dict[str, Any]] | None = None,
) -> str:
    """features → test-design-plan.md（面向测试工程师的执行计划，writing-plans 风格）。"""
    req = requirement or {}
    lines: list[str] = ["# 测试设计计划（test-design-plan）", ""]
    if req.get("project_context"):
        lines += [f"**测试对象**：{req['project_context']}", ""]
    io = req.get("io_constraints") or {}
    if (io.get("input") or "").strip() or (io.get("output") or "").strip():
        lines += [
            f"**系统口径**：输入 = {io.get('input', '')}；输出 = {io.get('output', '')}",
            "",
        ]
    lines += ["## 设计方法与优先级口径", "",
              "- 方法：正向 / 反向 / 边界值 / 等价类 / 状态流转 / 场景法（每条用例标注 case_type）",
              "- 优先级：P0 核心路径与关键校验 · P1 边界与重要异常 · P2 次要异常与体验",
              "- 层级：功能性优先，性能其次，安全性再次（io/condition 密集的安全提前）",
              ""]
    lines += ["## 功能点拆分", ""]
    for f in features:
        fid, name = f.get("feature_id"), f.get("name", "")
        lines.append(f"### {fid} {name}")
        if f.get("description"):
            lines.append(f"{f['description']}")
        if f.get("target_modules"):
            lines.append(f"- 模块：{'、'.join(f['target_modules'])}")
        for c in f.get("acceptance_criteria") or []:
            lines.append(f"- 验收：{c}")
        for c in f.get("edge_cases") or []:
            lines.append(f"- 边界：{c}")
        lines.append("")
    if questions:
        lines += ["## 待确认问题（未回答按推荐项执行）", ""]
        for q in questions:
            rec = f"（推荐：{q.get('recommended')}）" if q.get("recommended") else ""
            lines.append(f"- {q.get('question')} {rec}")
        lines.append("")
    lines += ["## 验收准出（每个功能点共用）", "",
              "- 每条验收标准/边界场景都有对应用例，且用例 rationale 注明归属",
              "- 每个功能点至少 1 条正向用例；反向覆盖所有可校验约束",
              "- 跨功能点端到端场景由综合功能点（F0）补齐",
              ""]
    return "\n".join(ln for ln in lines if ln is not None)
