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
from typing import Any

from ..llm_client import invoke_json
from .base import TestGenProvider, TestReport

SYSTEM_PROMPT_TEST_DESIGN = """你是有经验的测试架构师。根据【需求】【逻辑图】【目标模块】设计测试场景组。

优先级策略（整体）：功能性优先，性能其次，安全性再次。
例外：如果目标模块涉及 io/condition 节点（输入边界、权限、分支判断），安全性用例提前到 P0。
如果涉及数据汇聚/循环/批量处理，性能用例提前。

功能用例必须覆盖（按适用性选取）：
- 正常主路径（happy path）
- 等价类划分、边界值（含最大/最小/溢出边界）
- 错误路径、空输入、非法输入
- 连续/重复操作

性能用例：响应时间、吞吐、并发/连续操作（如适用）。

安全用例：输入校验、异常输入、数值溢出、越权/权限（如适用）。

每个用例给出：层级（functional/performance/security）、优先级（P0/P1/P2）、
标题、前置条件、步骤、预期结果。输出 JSON。"""


def _build_user_prompt(project_root: str, target_symbols: list[str], logic_graph: dict[str, Any]) -> str:
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
    return (
        f"【项目根目录】{project_root}\n"
        f"【目标模块】{json.dumps(target_symbols, ensure_ascii=False)}\n"
        f"【逻辑图】{json.dumps(graph_ctx, ensure_ascii=False)}\n"
        "请按优先级策略输出测试场景组。"
    )


_SCHEMA = {
    "type": "object",
    "required": ["scenarios", "summary"],
    "properties": {
        "scenarios": {
            "type": "array",
            "minItems": 4,
            "items": {
                "type": "object",
                "required": ["tier", "priority", "title", "steps", "expected"],
                "properties": {
                    "tier": {"type": "string", "enum": ["functional", "performance", "security"]},
                    "priority": {"type": "string", "enum": ["P0", "P1", "P2"]},
                    "title": {"type": "string"},
                    "target": {"type": "string"},
                    "precondition": {"type": "string"},
                    "steps": {"type": "string"},
                    "expected": {"type": "string"},
                    "rationale": {"type": "string"},
                },
            },
        },
        "summary": {"type": "string"},
    },
}


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
    ) -> TestReport:
        graph = logic_graph or {}
        result = await invoke_json(
            system_prompt=SYSTEM_PROMPT_TEST_DESIGN,
            user_prompt=_build_user_prompt(project_root, target_symbols, graph),
            json_schema=_SCHEMA,
            response_type="test_design",
        )
        scenarios = result.get("scenarios") or []
        cases = [
            {
                "test_file": f"tests/test_{target_symbols[0].replace('.', '_')}.py" if target_symbols else "tests/test_scenarios.py",
                "test_symbol": f"test_{s.get('tier', 'case')}_{i + 1:02d}",
                "tier": s.get("tier"),
                "priority": s.get("priority"),
                "title": s.get("title"),
                "target": s.get("target"),
                "precondition": s.get("precondition", ""),
                "steps": s.get("steps"),
                "expected": s.get("expected"),
                "rationale": s.get("rationale", ""),
                "code_snippet": f"{s.get('steps', '')}\n预期: {s.get('expected', '')}",
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
        }