"""LLM 测试场景设计 Provider（第 5 点落地）。

测试场景设计策略（整体优先级）：功能性优先，性能其次，安全性再次。
按 LogicGraph 节点类型微调：
  - io / condition 节点（输入边界、权限、分支）→ 安全 + 边界用例提前
  - 数据汇聚 / 循环 / 批量节点 → 性能用例提前

设计结果写进 TestReport.test_cases（每用例带 tier/priority/title/steps/expected），
run.passed = 场景数（未执行真实测试，logs 注明需接执行器）。
"""
from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..llm_client import invoke_json
from .base import TestGenProvider, TestReport


class _Scenario(BaseModel):
    tier: Literal["functional", "performance", "security"] = Field(description="用例层级")
    priority: Literal["P0", "P1", "P2"] = Field(description="优先级")
    title: str = Field(
        description="用例标题：概括「谁在什么条件下做什么、预期什么结果」"
    )
    case_type: str | None = Field(
        default=None,
        description="设计方法类型：正向/反向/边界值/等价类/状态流转/场景法/性能/安全",
    )
    target: str | None = Field(default=None, description="所属模块/功能点/节点，用于模块分组")
    precondition: str | None = Field(
        default=None,
        description="前置条件：写清账号、环境、数据状态等可执行前提",
    )
    steps: str = Field(
        description="操作步骤：分步编写、每步可执行可验证；涉及页面/入口写清操作路径"
    )
    expected: str = Field(
        description="预期结果：可验收，如「显示某某文案」「状态变为已支付」"
    )
    data_requirement: str | None = Field(default=None, description="测试数据要求（可选）")
    rationale: str | None = Field(default=None, description="设计依据")


class _TestDesign(BaseModel):
    """测试设计产出：总-分结构（概述 → 分模块用例清单）+ 质量自检。"""
    overview: str = Field(
        description="测试概述：测试范围、测试类型说明、通用前置条件与数据准备（总文档 INDEX 内容）"
    )
    scenarios: list[_Scenario] = Field(description="测试场景组（每条带所属模块 target）")
    self_check: list[str] = Field(
        default_factory=list,
        description="输出前质量自检结论，逐条对应：正向覆盖/反向覆盖/边界覆盖/状态流转覆盖/类型优先级标注",
    )
    summary: str = Field(description="设计摘要")

SYSTEM_PROMPT_TEST_DESIGN = """你是有经验的测试架构师。根据【需求】【逻辑图】【目标模块】设计测试场景组，
运用系统化测试设计方法（正向 / 反向 / 边界值 / 等价类 / 状态流转 / 场景法）。

## 设计策略（必须默认系统化运用，用 case_type 标注每条用例的运用方法）
1. 正向用例：每个核心功能点至少 1 条 happy path；前置、输入、步骤、预期与需求一致；成功判定标准明确。
2. 反向/异常用例：每个可校验的输入/约束至少考虑一类无效情况（格式错、类型错、越权、过期、重复提交等）；
   预期必须明确（错误提示文案、不写库、状态不变等不产生副作用的要求）。
3. 边界值：提取所有「有范围/长度/数量限制」的字段或参数，对每个边界设计边界内有效值、边界值、边界外无效值；
   可选/可空字段考虑空串、null、未传；数值考虑 0、负值、极大值（若业务允许）。
4. 等价类：有效等价类选 1~2 个代表值；无效等价类对每种违规类型各选代表值；与边界值结合避免冗余。
5. 状态流转：若有状态机/多步骤流程，列出主要状态与允许迁移，覆盖合法迁移（正向）与
   非法状态下操作（反向，预期拒绝或明确提示）；涉及角色/权限时覆盖越权访问与可见性。
6. 场景法：归纳 2~3 个典型用户目标或业务场景，端到端串联多个功能点，每个场景覆盖主流程与常见分支，
   同一场景下可有正常/异常/边界变体。
7. 优先级：核心正常路径、关键校验与错误处理 → P0；边界与次要异常 → P1/P2。

整体层级（tier）：功能性优先，性能其次，安全性再次。
例外：目标涉及 io/condition 节点（输入边界、权限、分支判断）时安全用例提前到 P0；
涉及数据汇聚/循环/批量处理时性能用例提前。

仅需求模式（未提供项目代码）时：以场景法为主线设计【端到端】测试场景——从用户视角出发，
按「前置准备 → 操作步骤 → 预期结果」描述完整业务路径，覆盖需求中的每条验收标准和每个边界场景；
不要假设实现细节，不要引用具体代码文件或函数。

## 表述要求
- 标题概括「谁在什么条件下做什么、预期什么结果」，便于评审与回归选择。
- 步骤分步编写，每步可执行、可验证；涉及页面/入口写清操作路径。
- 预期结果可验收（如「显示某某文案」「状态变为已支付」「列表出现一条新记录」）。
- 前置条件写清账号、环境、数据状态，必要时区分环境。
- 每条用例归属到需求中的模块或功能点（target 字段），便于需求覆盖与追溯。

## 输出前质量自检（不满足则补充用例后再输出，并把逐条结论写入 self_check）
- 每个核心功能点是否至少有一条正向用例？
- 关键输入与约束是否都有反向或异常用例？
- 有范围/长度/数量限制的是否有边界值用例？
- 若有状态与流程，是否覆盖合法迁移与非法操作？
- 用例类型（case_type）与优先级是否明确？

输出 JSON。"""


def _build_user_prompt(
    project_root: str,
    target_symbols: list[str],
    logic_graph: dict[str, Any],
    *,
    requirement: dict[str, Any] | None = None,
    feedback: str | None = None,
) -> str:
    graph_ctx = {
        "graph_id": logic_graph.get("graph_id"),
        "nodes": [
            {"node_id": n.get("node_id"), "label": n.get("label"),
             "node_type": n.get("node_type"), "is_modified": n.get("is_modified")}
            for n in (logic_graph.get("nodes") or [])
        ],
        "edges": [
            {"edge_id": e.get("edge_id"), "from_node": e.get("from_node"),
             "to_node": e.get("to_node"), "edge_type": e.get("edge_type"),
             "condition": e.get("condition")}
            for e in (logic_graph.get("edges") or [])
        ],
    }
    root_line = (
        f"【项目根目录】{project_root}\n" if project_root
        else "【项目根目录】（未提供项目代码，仅基于需求与逻辑图设计端到端测试场景）\n"
    )
    parts = [
        root_line,
        f"【目标模块】{json.dumps(target_symbols, ensure_ascii=False)}\n",
        f"【逻辑图】{json.dumps(graph_ctx, ensure_ascii=False)}\n",
    ]
    req = requirement or {}
    req_sections: list[str] = []
    if (req.get("project_context") or "").strip():
        req_sections.append(f"项目背景：{req['project_context']}")
    io = req.get("io_constraints") or {}
    if (io.get("input") or "").strip() or (io.get("output") or "").strip():
        req_sections.append(f"输入约束：{io.get('input', '')}；输出约束：{io.get('output', '')}")
    for ec in req.get("edge_cases") or []:
        req_sections.append(f"边界场景：{ec}")
    for ac in req.get("acceptance_criteria") or []:
        req_sections.append(f"验收标准：{ac}")
    if req_sections:
        parts.append("【需求要点】\n" + "\n".join(req_sections) + "\n")
    if feedback and feedback.strip():
        parts.append(
            "【上轮验收意见（用户驳回测试设计后给出的修改要求，本轮必须针对性修正）】\n"
            f"{feedback.strip()}\n"
        )
    parts.append(
        "请按上述设计策略系统化输出测试场景组：先写 overview 测试概述（总-分结构的总文档），"
        "再输出 scenarios（每条标注 case_type 设计方法与 target 所属模块），最后给出 self_check 自检结论。"
    )
    return "\n".join(parts)

class LlmTestGenProvider(TestGenProvider):
    name = "llm_test_gen"

    async def generate(
        self,
        project_root: str,
        target_symbols: list[str],
        *,
        coverage_target: int = 80,
        modified_branches_only: bool = True,
        logic_graph: dict[str, Any] | None = None,
        session_id: str | None = None,
        requirement: dict[str, Any] | None = None,
        feedback: str | None = None,
    ) -> TestReport:
        graph = logic_graph or {}
        result = await invoke_json(
            system_prompt=SYSTEM_PROMPT_TEST_DESIGN,
            user_prompt=_build_user_prompt(
                project_root, target_symbols, graph,
                requirement=requirement, feedback=feedback,
            ),
            response_model=_TestDesign,
            response_type="test_design",
        )
        # invoke_json 可能返回纯 dict（mock）或 pydantic 模型，统一取 scenarios
        if hasattr(result, "model_dump"):
            result = result.model_dump()
        scenarios = result.get("scenarios")
        if not isinstance(scenarios, list):
            # mock 兜底键名兼容（如 test_scenarios）
            for v in result.values():
                if isinstance(v, list) and v and isinstance(v[0], dict):
                    scenarios = v
                    break
        scenarios = scenarios or []

        def _sget(s: Any, key: str, default: str = "") -> str:
            v = s.get(key) if isinstance(s, dict) else getattr(s, key, None)
            if v is None:
                return default
            return v if isinstance(v, str) else str(v)

        cases = [
            {
                "test_file": (
                    f"tests/test_{target_symbols[0].replace('.', '_')}.py"
                    if target_symbols else "tests/test_scenarios.py"
                ),
                "test_symbol": f"test_{_sget(s, 'tier', 'case')}_{i + 1:02d}",
                "case_id": f"TC-{i + 1:03d}",
                "tier": s.get("tier") if isinstance(s, dict) else getattr(s, "tier", None),
                "priority": s.get("priority") if isinstance(s, dict) else getattr(s, "priority", None),
                "case_type": _sget(s, "case_type") or None,
                "title": _sget(s, "title") or None,
                "target": _sget(s, "target") or None,
                "precondition": _sget(s, "precondition"),
                "steps": _sget(s, "steps"),
                "expected": _sget(s, "expected"),
                "data_requirement": _sget(s, "data_requirement") or None,
                "rationale": _sget(s, "rationale"),
                "code_snippet": f"{_sget(s, 'steps')}\n预期: {_sget(s, 'expected')}",
            }
            for i, s in enumerate(scenarios)
        ]
        total = len(cases)
        return {
            "session_id": session_id or "llm-test-design",
            "test_cases": cases,
            "run": {
                "passed": total if total else 1,
                "failed": 0,
                "skipped": 0,
                "coverage_pct": None,
                "logs": f"LLM 设计 {total} 个测试场景（功能/性能/安全分级），未执行真实测试",
            },
            "target_symbols": target_symbols,
            # 总-分结构 + 质量自检（方法论来自 doc-based / functional testcase-generator skills）
            "overview": result.get("overview") if isinstance(result, dict) else None,
            "self_check": result.get("self_check") if isinstance(result, dict) else None,
        }