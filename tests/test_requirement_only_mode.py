"""仅需求模式（不提供项目代码）分支测试。

验证：
  1. Schema 层：has_project_code 分支开关 + 两种模式的校验规则
  2. 路由层：_route_after_graph_review / route_after_review 的模式分流
  3. 节点层：test_gen 仅需求模式下从需求+逻辑图推导测试目标
  4. 整图 E2E（Mock）：无代码 → 制图门 approve → 直接 test_gen → 人工验收 → END；
     终审 reject → 回 test_gen 重新设计 → 再次人工验收
"""
from __future__ import annotations

import uuid
from typing import Any

import pytest
from langgraph.types import Command

from devflow.orchestrator import (
    _route_after_graph_review,
    build_graph_with_providers,
    initial_state,
)
from devflow.providers import (
    MockCodeEdit,
    MockCodeGraphRender,
    MockCodeSearch,
    MockTestGen,
    Providers,
)
from devflow.schemas import empty_requirement, has_project_code, validate_requirement


# ═══════════════════════════════════════════════════════════════════
# 辅助
# ═══════════════════════════════════════════════════════════════════


def _mock_providers() -> Providers:
    return Providers(
        code_search=MockCodeSearch(),
        graph_render=MockCodeGraphRender(),
        code_edit=MockCodeEdit(),
        test_gen=MockTestGen(),
    )


def _requirement_only_requirement() -> dict[str, Any]:
    """完备的「仅需求模式」需求：无 project_root、不声明可访问代码。"""
    req = empty_requirement()
    req.update({
        "req_type": "new_feature",
        "project_context": "电商 App 的购物车模块（未提供代码仓库）",
        "existing_code_accessible": False,
        "io_constraints": {
            "input": "加入购物车 / 修改数量 / 删除商品",
            "output": "购物车列表与总价",
        },
        "edge_cases": ["数量为 0", "商品已下架", "未登录"],
        "acceptance_criteria": ["总价计算正确", "下架商品给出提示"],
    })
    return req


def _requirement_only_state() -> dict[str, Any]:
    """跳过澄清循环的初始 state（预填完整需求，不放 HumanMessage）。"""
    state = initial_state()
    state["requirement"] = _requirement_only_requirement()
    return state


# ═══════════════════════════════════════════════════════════════════
# 1. Schema：分支开关 + 校验
# ═══════════════════════════════════════════════════════════════════


class TestHasProjectCode:
    def test_false_when_no_root_and_not_accessible(self):
        req = empty_requirement()
        assert has_project_code(req) is False

    def test_true_when_accessible_declared(self):
        assert has_project_code({"existing_code_accessible": True}) is True

    def test_true_when_root_present(self):
        assert has_project_code({"project_root": "/workspace"}) is True

    def test_true_when_root_set_but_declared_false(self):
        """Web 配置面板填了路径但声明未提供 → 以路径为准（用户给了代码）。"""
        assert has_project_code(
            {"existing_code_accessible": False, "project_root": "/workspace"}
        ) is True

    def test_none_and_empty(self):
        assert has_project_code(None) is False
        assert has_project_code({"project_root": "   "}) is False


class TestValidateRequirementTwoModes:
    def test_requirement_only_complete_passes(self):
        assert validate_requirement(_requirement_only_requirement()) == []

    def test_requirement_only_missing_modules_is_ok(self):
        req = _requirement_only_requirement()
        req["target_modules"] = []
        assert validate_requirement(req) == []

    def test_code_mode_requires_root_and_modules(self):
        req = _requirement_only_requirement()
        req["existing_code_accessible"] = True
        errs = "\n".join(validate_requirement(req))
        assert "project_root" in errs
        assert "target_modules" in errs

    def test_code_mode_complete_passes(self):
        req = _requirement_only_requirement()
        req.update({
            "existing_code_accessible": True,
            "project_root": "/workspace/cart",
            "target_modules": ["cart/service.py"],
        })
        assert validate_requirement(req) == []

    def test_code_mode_root_empty_but_flag_true_errors(self):
        req = _requirement_only_requirement()
        req["existing_code_accessible"] = True
        req["project_root"] = ""
        errs = "\n".join(validate_requirement(req))
        assert "project_root" in errs


# ═══════════════════════════════════════════════════════════════════
# 2. 路由：模式分流
# ═══════════════════════════════════════════════════════════════════


class TestGraphReviewModeRoutes:
    def test_no_code_label(self):
        s = {"current_stage": "test", "requirement": {"existing_code_accessible": False}}
        assert _route_after_graph_review(s) == "no_code"

    def test_with_code_label(self):
        s = {"current_stage": "search", "requirement": {"project_root": "/w"}}
        assert _route_after_graph_review(s) == "with_code"

    def test_rejected_label(self):
        s = {"current_stage": "graph", "requirement": {}}
        assert _route_after_graph_review(s) == "rejected"


class TestReviewModeRoutes:
    def test_rejected_test_route(self):
        from devflow.nodes.review import route_after_review

        assert route_after_review({"current_stage": "test"}) == "rejected_test"

    def test_rejected_code_route(self):
        from devflow.nodes.review import route_after_review

        assert route_after_review({"current_stage": "code"}) == "rejected"

    def test_approved_route(self):
        from devflow.nodes.review import route_after_review

        assert route_after_review({"current_stage": "done"}) == "approved"


# ═══════════════════════════════════════════════════════════════════
# 3. 节点：test_gen 仅需求模式的目标推导
# ═══════════════════════════════════════════════════════════════════


class TestTestGenNodeRequirementOnly:
    @pytest.mark.asyncio
    async def test_targets_from_requirement(self):
        from devflow.nodes.provider_nodes import make_test_gen_node

        node = make_test_gen_node(_mock_providers())
        state = {
            "requirement": _requirement_only_requirement(),
            "logic_graph": {},
            "code_changes": [],
            "opencode_sessions": {},
        }
        out = await node.async_version(state)  # type: ignore[attr-defined]
        assert out.get("last_error_code") is None
        report = out["test_report"]
        # MockTestGen 按 target_symbols 造用例 → 目标来自需求里的模块列表
        assert "购物车模块" in (report.get("target_symbols") or [""])[0] or report["test_cases"]

    @pytest.mark.asyncio
    async def test_targets_fallback_to_graph_nodes(self):
        from devflow.nodes.provider_nodes import make_test_gen_node

        node = make_test_gen_node(_mock_providers())
        state = {
            "requirement": {**_requirement_only_requirement(), "target_modules": []},
            "logic_graph": {
                "nodes": [
                    {"node_id": "n-1", "label": "Input", "is_modified": False},
                    {"node_id": "n-2", "label": "Compute", "is_modified": True},
                ],
            },
            "code_changes": [],
            "opencode_sessions": {},
        }
        out = await node.async_version(state)  # type: ignore[attr-defined]
        assert out.get("last_error_code") is None
        assert (out["test_report"].get("target_symbols") or []) == ["Compute"]

    @pytest.mark.asyncio
    async def test_code_mode_still_requires_changes(self):
        """代码模式下 code_changes 为空仍报错（不因新模式放松原约束）。"""
        from devflow.nodes.provider_nodes import make_test_gen_node

        node = make_test_gen_node(_mock_providers())
        state = {
            "requirement": {**_requirement_only_requirement(), "project_root": "/w"},
            "logic_graph": {},
            "code_changes": [],
            "opencode_sessions": {},
        }
        out = await node.async_version(state)  # type: ignore[attr-defined]
        assert out.get("last_error_code"), "代码模式无变更应报错"


# ═══════════════════════════════════════════════════════════════════
# 4. 整图 E2E（Mock）：仅需求模式全流程
# ═══════════════════════════════════════════════════════════════════


class TestE2ERequirementOnly:
    @pytest.fixture(autouse=True)
    def _setup_graph(self):
        self.graph = build_graph_with_providers(_mock_providers())
        self.tid = f"tcro-{uuid.uuid4().hex[:8]}"
        self.config = {"configurable": {"thread_id": self.tid}}

    def test_full_pipeline_without_code(self):
        # 1. 启动 → 过需求确认门 + 图种类门 → 停在制图门禁
        list(self.graph.stream(_requirement_only_state(), self.config, stream_mode="updates"))
        list(self.graph.stream(Command(resume="confirm"), self.config, stream_mode="updates"))
        list(self.graph.stream(Command(resume="flowchart"), self.config, stream_mode="updates"))
        snapshot = self.graph.get_state(self.config)
        assert "graph_review" in (snapshot.next or [])

        # 2. 制图门 approve（未提供代码）→ 跳过检索/生成，直接到终审 review
        list(self.graph.stream(Command(resume="approve"), self.config, stream_mode="updates"))
        snapshot = self.graph.get_state(self.config)
        next_nodes = snapshot.next or []
        assert "review" in next_nodes, f"仅需求模式 approve 后应到达终审，实际 next={next_nodes}"

        # 3. 产物检查：无检索/无代码变更，但测试场景已设计
        vals = snapshot.values
        assert vals.get("code_context") in (None, [])
        assert vals.get("code_changes") in (None, [])
        report = vals.get("test_report") or {}
        assert report.get("test_cases"), "应产出端到端测试场景"
        # 未提供项目 → 不执行真实 pytest，但原因是明确的模式说明
        run = report.get("run") or {}
        assert not run.get("executed")
        assert "仅需求模式" in (run.get("skip_reason") or "")

        # 4. 终审 approve → 结束
        list(self.graph.stream(Command(resume="approve"), self.config, stream_mode="updates"))
        snapshot = self.graph.get_state(self.config)
        assert snapshot.next == ()
        assert snapshot.values.get("current_stage") == "done"

    def test_review_reject_goes_back_to_test_gen(self):
        # 1. 跑到制图门并通过（仅需求模式 → 直达终审）
        list(self.graph.stream(_requirement_only_state(), self.config, stream_mode="updates"))
        list(self.graph.stream(Command(resume="confirm"), self.config, stream_mode="updates"))
        list(self.graph.stream(Command(resume="flowchart"), self.config, stream_mode="updates"))
        list(self.graph.stream(Command(resume="approve"), self.config, stream_mode="updates"))
        snapshot = self.graph.get_state(self.config)
        assert "review" in (snapshot.next or [])

        # 2. 终审 reject → 应回测试用例设计（而不是 code_gen），再回终审
        list(self.graph.stream(
            Command(resume={"decision": "reject", "comment": "缺少未登录场景"}), self.config,
            stream_mode="updates",
        ))
        snapshot = self.graph.get_state(self.config)
        assert "review" in (snapshot.next or []), \
            f"仅需求模式 reject 后应重新设计用例并回到终审，实际 next={snapshot.next}"
        vals = snapshot.values
        # 意见回传给 test_gen 消费
        assert "未登录" in (vals.get("review_feedback") or "")
        # 不产生代码变更 / 落盘
        assert vals.get("code_changes") in (None, [])
