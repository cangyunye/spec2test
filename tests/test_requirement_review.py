"""制图前需求确认门禁（requirement_review）测试。

覆盖：
  - gate 节点真实 interrupt/resume（迷你图 + MemorySaver）：确认 / 就地改字段 / 驳回
  - 放行条件：已确认且无 AI 推断字段时不重复打断
  - 载荷/字段规格：来源标记、模式、点路径取值
  - 管线接线（clarify_validate → requirement_review → graph_type_select）+ server resume 组装
"""
from __future__ import annotations

import uuid

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from devflow.nodes.requirement_review import (
    SOURCE_INFERRED,
    SOURCE_USER,
    _apply_field_patch,
    _needs_review,
    inferred_fields,
    requirement_fields,
    requirement_review_node,
    review_payload,
    route_after_requirement_review,
)
from devflow.orchestrator import build_graph, build_graph_with_providers, initial_state
from devflow.state import GlobalState


def _mini_graph():
    g = StateGraph(GlobalState)
    g.add_node("gate", requirement_review_node)
    g.add_edge(START, "gate")
    g.add_edge("gate", END)
    return g.compile(checkpointer=MemorySaver())


def _state(**over) -> dict:
    base = {
        "requirement": initial_state()["requirement"]
        | {
            "req_type": "new_feature",
            "project_context": "商城下单支付流程",
            "io_constraints": {"input": "提交订单", "output": "订单号与状态"},
            "edge_cases": ["库存不足"],
            "acceptance_criteria": ["超时取消后释放库存"],
        },
        "requirement_confirmed": False,
        "requirement_sources": {},
        "missing_fields": [],
    }
    return base | over


def _drive(state: dict) -> tuple:
    """推进到 interrupt 挂起点，返回 (graph, config, payload)。"""
    graph = _mini_graph()
    config = {"configurable": {"thread_id": f"rr-{uuid.uuid4().hex[:8]}"}}
    list(graph.stream(state, config, stream_mode="updates"))
    snap = graph.get_state(config)
    payload = snap.tasks[0].interrupts[0].value if snap.tasks else None
    return graph, config, payload


class TestReviewPayload:
    def test_fields_cover_requirement_with_labels(self):
        payload = review_payload(_state())
        keys = [f["key"] for f in payload["fields"]]
        assert keys[0] == "req_type"
        assert "io_constraints.input" in keys and "acceptance_criteria" in keys
        by_key = {f["key"]: f for f in payload["fields"]}
        assert by_key["project_context"]["label"] == "项目背景"
        assert by_key["io_constraints.input"]["value"] == "提交订单"  # 点路径取值
        assert by_key["target_modules"]["value"] == []               # 空值也列出，用户可直接补

    def test_inferred_flag_from_sources(self):
        st = _state(requirement_sources={
            "project_context": SOURCE_USER,
            "acceptance_criteria": SOURCE_INFERRED,
        })
        payload = review_payload(st)
        by_key = {f["key"]: f for f in payload["fields"]}
        assert by_key["acceptance_criteria"]["inferred"] is True
        assert by_key["project_context"]["inferred"] is False
        assert payload["inferred_fields"] == ["acceptance_criteria"]

    def test_mode_reflects_project_code(self):
        assert review_payload(_state())["mode"] == "no_code"
        with_code = _state()
        with_code["requirement"] = with_code["requirement"] | {
            "existing_code_accessible": True,
            "project_root": "/workspace",
            "target_modules": ["order/"],
        }
        assert review_payload(with_code)["mode"] == "with_code"

    def test_type_is_requirement_review(self):
        assert review_payload(_state())["type"] == "requirement_review"


class TestNeedsReview:
    """B：来源明确且无推断字段 → 放行；未知来源或存在推断 → 必须确认。"""

    def test_unknown_sources_requires_review(self):
        assert _needs_review(_state(requirement_sources={})) is True

    def test_inferred_present_requires_review(self):
        assert _needs_review(_state(requirement_sources={
            "project_context": SOURCE_INFERRED,
        })) is True

    def test_all_user_sources_passthrough(self):
        assert _needs_review(_state(requirement_sources={
            "project_context": SOURCE_USER,
            "edge_cases": SOURCE_USER,
        })) is False

    def test_inferred_fields_helper(self):
        assert inferred_fields({"a": SOURCE_USER, "b": SOURCE_INFERRED}) == ["b"]
        assert inferred_fields(None) == []


class TestGateNode:
    def test_interrupt_then_confirm(self):
        graph, config, payload = _drive(_state())
        assert payload["type"] == "requirement_review"
        assert payload["fields"], "载荷必须带字段清单"
        assert graph.get_state(config).next == ("gate",)

        list(graph.stream(Command(resume="confirm"), config, stream_mode="updates"))
        vals = graph.get_state(config).values
        assert vals["requirement_confirmed"] is True
        assert vals["current_stage"] == "graph"
        assert graph.get_state(config).next == ()

    def test_confirm_with_field_patch_applies_dotted_keys(self):
        graph, config, _ = _drive(_state())
        list(graph.stream(
            Command(resume={
                "decision": "confirm",
                "fields": {
                    "io_constraints.input": "提交订单 + 支付请求",
                    "edge_cases": ["库存不足", "超时未支付"],
                    "project_root": "/workspace/order",
                },
            }),
            config,
            stream_mode="updates",
        ))
        req = graph.get_state(config).values["requirement"]
        assert req["io_constraints"]["input"] == "提交订单 + 支付请求"
        assert req["io_constraints"]["output"] == "订单号与状态"  # 同层其它子字段不被覆盖
        assert req["edge_cases"] == ["库存不足", "超时未支付"]
        assert req["project_root"] == "/workspace/order"
        # 用户改过的字段来源标记为 user（B：不再被当成 AI 推断）
        sources = graph.get_state(config).values["requirement_sources"]
        assert sources["io_constraints.input"] == SOURCE_USER
        assert sources["project_root"] == SOURCE_USER

    def test_reject_returns_to_clarify_without_advancing(self):
        graph, config, _ = _drive(_state())
        list(graph.stream(
            Command(resume={"decision": "reject", "comment": "超时时长没写清楚"}),
            config,
            stream_mode="updates",
        ))
        vals = graph.get_state(config).values
        assert vals["requirement_confirmed"] is False
        assert vals["current_stage"] == "clarify"
        msgs = [str(getattr(m, "content", "")) for m in vals.get("messages", [])]
        assert any("超时时长没写清楚" in m for m in msgs), "驳回意见应回显给用户"

    def test_passthrough_when_confirmed_and_no_inferred(self):
        """已确认 + 全部字段来源为 user → 不再打断（零交互放行）。"""
        graph, config, payload = _drive(_state(
            requirement_confirmed=True,
            requirement_sources={"project_context": SOURCE_USER},
        ))
        assert payload is None
        assert graph.get_state(config).next == ()
        assert graph.get_state(config).values["current_stage"] == "graph"

    def test_passthrough_without_confirmation_when_all_user(self):
        """B：从未确认过、但字段全部来自用户原话 → 无需评审，直接放行不打断。"""
        graph, config, payload = _drive(_state(
            requirement_confirmed=False,
            requirement_sources={"project_context": SOURCE_USER, "edge_cases": SOURCE_USER},
        ))
        assert payload is None
        assert graph.get_state(config).next == ()
        assert graph.get_state(config).values["requirement_confirmed"] is True

    def test_interrupts_when_inferred_and_unconfirmed(self):
        """存在 AI 推断字段且尚未确认 → 必须打断等用户确认。"""
        graph, config, payload = _drive(_state(
            requirement_confirmed=False,
            requirement_sources={"project_context": SOURCE_INFERRED},
        ))
        assert payload is not None and payload["inferred_fields"] == ["project_context"]
        assert graph.get_state(config).next == ("gate",)

    def test_confirmed_passthrough_even_with_inferred_left(self):
        """已确认过（需求未变）→ 用户接受过的推断字段不再反复追问。"""
        graph, config, payload = _drive(_state(
            requirement_confirmed=True,
            requirement_sources={"project_context": SOURCE_INFERRED},
        ))
        assert payload is None
        assert graph.get_state(config).next == ()


class TestApplyFieldPatch:
    def test_deep_copy_does_not_mutate_original(self):
        req = {"io_constraints": {"input": "a", "output": "b"}}
        out = _apply_field_patch(req, {"io_constraints.input": "c"})
        assert out["io_constraints"]["input"] == "c"
        assert req["io_constraints"]["input"] == "a"

    def test_missing_nested_parent_created(self):
        out = _apply_field_patch({}, {"io_constraints.output": "x"})
        assert out == {"io_constraints": {"output": "x"}}

    def test_ignores_bad_keys(self):
        out = _apply_field_patch({"a": 1}, {"": 2, "b": 3})
        assert out == {"a": 1, "b": 3}


class TestExtractionDrivesGate:
    """B 端到端：抽取未标注推断 → 门禁直接放行；标注了推断 → 停下等确认。"""

    _EXTRACT = {
        "req_type": "new_feature",
        "project_context": "商城下单支付流程",
        "existing_code_accessible": False,
        "io_constraints": {"input": "提交订单", "output": "订单号与状态"},
        "edge_cases": ["库存不足"],
        "acceptance_criteria": ["超时取消释放库存"],
    }

    def _run(self, monkeypatch, payload: dict) -> tuple:
        from langchain_core.messages import HumanMessage

        async def fake(**kwargs):
            return dict(payload)

        async def fake_title(**kwargs):
            return "下单支付"

        monkeypatch.setattr("devflow.nodes.clarify.invoke_json", fake)
        monkeypatch.setattr("devflow.nodes.clarify.invoke_text", fake_title)
        graph = build_graph()
        config = {"configurable": {"thread_id": f"rr-e2e-{uuid.uuid4().hex[:8]}"}}
        state = initial_state() | {
            "messages": [HumanMessage(content="给商城下单支付加库存校验与超时取消")]
        }
        list(graph.stream(state, config, stream_mode="updates"))
        return graph, config

    def test_no_inferred_fields_skips_gate(self, monkeypatch):
        """全部字段有用户原话依据 → 不打断，直接到图种类门。"""
        graph, config = self._run(monkeypatch, self._EXTRACT)
        next_nodes = graph.get_state(config).next or ()
        assert "requirement_review" not in next_nodes
        assert "graph_type_select" in next_nodes
        assert graph.get_state(config).values["requirement_confirmed"] is True

    def test_inferred_fields_stop_at_gate(self, monkeypatch):
        """有 AI 推断字段 → 停在需求确认门等用户确认。"""
        graph, config = self._run(
            monkeypatch, self._EXTRACT | {"inferred_fields": ["acceptance_criteria"]}
        )
        next_nodes = graph.get_state(config).next or ()
        assert "requirement_review" in next_nodes
        payload = graph.get_state(config).tasks[0].interrupts[0].value
        assert payload["inferred_fields"] == ["acceptance_criteria"]


class TestRouteAndWiring:
    def test_route_confirmed_vs_rejected(self):
        assert route_after_requirement_review({"requirement_confirmed": True}) == "confirmed"
        assert route_after_requirement_review({"requirement_confirmed": False}) == "rejected"
        assert route_after_requirement_review({}) == "rejected"

    def test_wired_in_both_graphs(self):
        for graph in (build_graph(), build_graph_with_providers()):
            nodes = set(graph.get_graph().nodes)
            assert "requirement_review" in nodes
            edges = {(e.source, e.target) for e in graph.get_graph().edges}
            assert ("clarify_validate", "requirement_review") in edges
            assert ("requirement_review", "graph_type_select") in edges
            assert ("requirement_review", "__end__") in edges

    def test_gate_resume_carries_fields(self):
        from web.server import _gate_resume

        # 需求确认就地改字段 → 结构化 dict
        cmd = _gate_resume("confirm", "", "", {"project_context": "商城下单"})
        assert cmd.resume == {
            "decision": "confirm",
            "fields": {"project_context": "商城下单"},
        }
        # 无附加信息仍是裸字符串（旧门禁兼容）
        assert _gate_resume("confirm").resume == "confirm"

    # 注：query 版 fields 解析（_parse_fields）随 GET /stream 端点一并移除，
    # POST /gates 的 fields 走 JSON body（dict），无需字符串解析。

