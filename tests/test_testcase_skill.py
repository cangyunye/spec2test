"""测试设计方法论（来自 doc-based / functional testcase-generator skills）集成测试。

验证：
  1. 提示词包含系统化设计策略与自检要求
  2. LlmTestGenProvider 产出带 case_id / case_type / overview / self_check 的报告
  3. MockTestGen 字段对齐
  4. Web 导出 MD 为总-分结构（概述 → 分模块用例 → 自检）
"""
from __future__ import annotations

from typing import Any

import pytest

from devflow.providers.llm_testgen import (
    LlmTestGenProvider,
    SYSTEM_PROMPT_TEST_DESIGN,
    _build_user_prompt,
)
from devflow.providers.mock import MockTestGen


# ═══════════════════════════════════════════════════════════════════
# 1. 提示词方法论
# ═══════════════════════════════════════════════════════════════════


class TestPromptMethodology:
    def test_system_prompt_contains_design_strategies(self):
        for kw in ["正向", "反向", "边界值", "等价类", "状态流转", "场景法", "P0", "质量自检"]:
            assert kw in SYSTEM_PROMPT_TEST_DESIGN, f"系统提示词缺少设计策略关键词: {kw}"

    def test_user_prompt_requirement_only_and_feedback(self):
        prompt = _build_user_prompt(
            "",
            ["购物车"],
            {"graph_id": "g-1", "nodes": [], "edges": []},
            requirement={
                "project_context": "电商购物车",
                "io_constraints": {"input": "加购", "output": "总价"},
                "edge_cases": ["数量为 0"],
                "acceptance_criteria": ["总价正确"],
            },
            feedback="补充未登录场景",
        )
        assert "未提供项目代码" in prompt
        assert "项目背景：电商购物车" in prompt
        assert "边界场景：数量为 0" in prompt
        assert "验收标准：总价正确" in prompt
        assert "补充未登录场景" in prompt

    def test_user_prompt_with_project_root(self):
        prompt = _build_user_prompt("/workspace", ["a.py"], {"nodes": [], "edges": []})
        assert "【项目根目录】/workspace" in prompt


# ═══════════════════════════════════════════════════════════════════
# 2. LlmTestGenProvider 产出结构
# ═══════════════════════════════════════════════════════════════════


def _fake_design() -> dict[str, Any]:
    return {
        "overview": "覆盖购物车加购、改量、删除与总价计算",
        "scenarios": [
            {
                "tier": "functional", "priority": "P0",
                "title": "已登录用户加购一件商品，列表出现该商品",
                "case_type": "正向", "target": "购物车模块",
                "precondition": "已登录，购物车为空",
                "steps": "1. 进入商品详情\n2. 点击加入购物车",
                "expected": "购物车列表出现该商品，数量为 1",
                "data_requirement": "在架商品 1 件",
                "rationale": "核心 happy path",
            },
            {
                "tier": "functional", "priority": "P1",
                "title": "数量改为 0 时给出提示",
                "case_type": "边界值", "target": "购物车模块",
                "steps": "把数量改为 0",
                "expected": "提示数量不能为 0",
            },
            {
                "tier": "security", "priority": "P0",
                "title": "未登录用户加购被引导登录",
                "case_type": "反向", "target": "登录模块",
                "steps": "未登录状态点加购",
                "expected": "跳转登录页",
            },
        ],
        "self_check": ["每个核心功能至少 1 条正向用例：通过", "边界值覆盖：通过"],
        "summary": "3 条用例",
    }


class TestLlmTestGenOutput:
    @pytest.fixture()
    def report(self, monkeypatch):
        async def _fake_invoke(**kwargs):
            return _fake_design()

        monkeypatch.setattr("devflow.providers.llm_testgen.invoke_json", _fake_invoke)
        import asyncio

        return asyncio.run(LlmTestGenProvider().generate("", ["购物车"]))

    def test_case_id_sequential(self, report):
        ids = [c["case_id"] for c in report["test_cases"]]
        assert ids == ["TC-001", "TC-002", "TC-003"]

    def test_case_type_and_data_requirement_passthrough(self, report):
        c0 = report["test_cases"][0]
        assert c0["case_type"] == "正向"
        assert c0["data_requirement"] == "在架商品 1 件"
        assert report["test_cases"][1]["case_type"] == "边界值"

    def test_overview_and_self_check_in_report(self, report):
        assert report["overview"].startswith("覆盖购物车")
        assert len(report["self_check"]) == 2

    def test_case_fields_kept(self, report):
        c0 = report["test_cases"][0]
        assert c0["tier"] == "functional"
        assert c0["priority"] == "P0"
        assert "预期: 购物车列表出现该商品" in c0["code_snippet"]


# ═══════════════════════════════════════════════════════════════════
# 3. MockTestGen 字段对齐
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_mock_testgen_new_fields():
    rep = await MockTestGen().generate("", ["calc.add"])
    assert rep["test_cases"][0]["case_id"] == "TC-001"
    assert rep["test_cases"][0]["case_type"]
    assert rep["overview"]
    assert rep["self_check"] == []


# ═══════════════════════════════════════════════════════════════════
# 4. Web 导出 MD：总-分结构
# ═══════════════════════════════════════════════════════════════════


class TestExportMdGrouped:
    def _vals(self) -> dict[str, Any]:
        return {
            "current_stage": "done",
            "requirement": {"project_context": "购物车"},
            "logic_graph": None,
            "code_changes": [],
            "test_report": {
                "overview": "覆盖购物车核心流程",
                "self_check": ["正向覆盖：通过"],
                "run": {"passed": 3, "failed": 0},
                "test_cases": [
                    {"case_id": "TC-001", "tier": "functional", "priority": "P0",
                     "case_type": "正向", "title": "加购成功", "target": "购物车模块",
                     "precondition": "已登录", "steps": "点加购", "expected": "列表+1"},
                    {"case_id": "TC-002", "tier": "functional", "priority": "P1",
                     "case_type": "边界值", "title": "数量为 0", "target": "购物车模块",
                     "steps": "改 0", "expected": "提示"},
                    {"case_id": "TC-003", "tier": "security", "priority": "P0",
                     "case_type": "反向", "title": "未登录加购", "target": "登录模块",
                     "steps": "未登录点加购", "expected": "跳登录"},
                ],
            },
        }

    def test_index_and_module_sections(self):
        from web.server import _build_export_md

        md = _build_export_md(self._vals())
        assert "## 测试用例文档" in md
        assert "覆盖购物车核心流程" in md            # 概述
        assert "优先级口径" in md                     # 公共口径
        assert "### 2.1 购物车模块" in md             # 分模块节
        assert "### 2.2 登录模块" in md
        assert "| TC-001 |" in md                     # 用例标识列
        assert "### 质量自检" in md and "正向覆盖：通过" in md

    def test_module_order_follows_first_appearance(self):
        from web.server import _build_export_md

        md = _build_export_md(self._vals())
        assert md.index("2.1 购物车模块") < md.index("2.2 登录模块")
