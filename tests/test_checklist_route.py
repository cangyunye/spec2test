"""Checklist 路由门禁测试：match/gate 两节点协议 + 管线接线 + server resume 组装。

门禁流程用两节点迷你图跑真实 interrupt/Command 恢复（MemorySaver），
路由匹配 LLM 一律 monkeypatch（单测不依赖模型输出）。
"""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from devflow.checklist.scaffold import init_library
from devflow.nodes import checklist_route as cr
from devflow.nodes.checklist_route import checklist_route_gate, checklist_route_match
from devflow.orchestrator import build_graph_with_providers, initial_state
from devflow.state import GlobalState


@pytest.fixture()
def lib(tmp_path) -> Path:
    init_library(tmp_path, with_example=True)
    return tmp_path


def _state(lib: Path) -> dict:
    return {
        "requirement": initial_state()["requirement"]
        | {
            "project_context": "订单支付系统，新增部分退款能力",
            "target_modules": ["refund"],
        },
        "checklist_routed": False,
        "checklist_route": None,
        "checklist_context": None,
    }


def _match_patch(monkeypatch, rel_dirs: list[str]):
    """把 LLM 匹配替换为确定性结果：命中 payment + 子业务 rel_dirs。"""
    from devflow.checklist.models import RouteMatch, RouteMatchBusiness, RouteMatchSub

    async def fake_match(text, root):
        return RouteMatch(
            businesses=[RouteMatchBusiness(name="payment", reason="涉及支付退款")],
            sub_businesses=[
                RouteMatchSub(business="payment", name=r.split("/")[-1]) for r in rel_dirs
            ],
        )

    monkeypatch.setattr(cr, "match_businesses", fake_match)


def _mini_graph():
    g = StateGraph(GlobalState)
    g.add_node("match", checklist_route_match)
    g.add_node("gate", checklist_route_gate)
    g.add_edge(START, "match")
    g.add_edge("match", "gate")
    g.add_edge("gate", END)
    return g.compile(checkpointer=MemorySaver())


# ═══════════════════════════════════════════════════════════════════
# match 节点
# ═══════════════════════════════════════════════════════════════════


class TestMatchNode:
    def test_stores_candidates(self, lib, monkeypatch):
        _match_patch(monkeypatch, ["payment/refund"])
        monkeypatch.setattr(cr, "resolve_root", lambda pr="": lib)
        out = checklist_route_match(_state(lib))
        assert not out.get("checklist_routed")  # 有候选：留给 gate 打断
        cands = out["checklist_route"]["candidates"]
        assert cands[0]["rel_dir"] == "payment"
        assert cands[0]["children"][0]["rel_dir"] == "payment/refund"

    def test_no_match_records_empty_status(self, lib, monkeypatch):
        """LLM 无匹配 → 落 status=no_match 且不打 checklist_routed（门禁必弹）。"""
        from devflow.checklist.models import RouteMatch

        async def fake_match(text, root):
            return RouteMatch()

        monkeypatch.setattr(cr, "match_businesses", fake_match)
        monkeypatch.setattr(cr, "resolve_root", lambda pr="": lib)
        out = checklist_route_match(_state(lib))
        assert not out.get("checklist_routed")  # 门禁仍要打断（给上传入口）
        route = out["checklist_route"]
        assert route["candidates"] == []
        assert route["status"] == "no_match"
        assert route["business_count"] >= 1  # init_library 示例库至少 1 个业务

    def test_empty_library_no_llm(self, tmp_path, monkeypatch):
        """空库：status=empty_library、business_count=0，且不调路由 LLM。"""
        monkeypatch.setenv("DEVFLOW_CHECKLIST_ROOT", str(tmp_path / "no-lib"))
        out = checklist_route_match(_state(tmp_path))
        route = out["checklist_route"]
        assert route["status"] == "empty_library"
        assert route["business_count"] == 0
        assert route["candidates"] == []

    def test_already_routed_passthrough(self, lib, monkeypatch):
        """回炉重生成路径：checklist_routed=True 时零开销放行（不调 LLM）。"""

        def _boom(*a, **k):
            raise AssertionError("已路由过不应再调 LLM")

        monkeypatch.setattr(cr, "match_businesses", _boom)
        st = _state(lib) | {"checklist_routed": True}
        assert checklist_route_match(st) == {}


# ═══════════════════════════════════════════════════════════════════
# gate 节点：真实 interrupt / resume（迷你图）
# ═══════════════════════════════════════════════════════════════════


class TestGateNode:
    @pytest.fixture()
    def gated(self, lib, monkeypatch):
        """推进到 gate 挂起点，返回 (graph, config)。stream 是惰性生成器，必须消费。"""
        _match_patch(monkeypatch, ["payment/refund"])
        monkeypatch.setattr(cr, "resolve_root", lambda pr="": lib)
        graph = _mini_graph()
        config = {"configurable": {"thread_id": f"cr-{uuid.uuid4().hex[:8]}"}}
        list(graph.stream(_state(lib), config, stream_mode="updates"))
        return graph, config

    def test_interrupt_payload_shape(self, gated):
        graph, config = gated
        snap = graph.get_state(config)
        assert snap.next == ("gate",)
        intr = snap.tasks[0].interrupts[0].value
        assert intr["type"] == "checklist_route"
        assert intr["candidates"][0]["rel_dir"] == "payment"

    def test_resume_confirm_loads_checklists(self, gated, lib):
        graph, config = gated
        list(graph.stream(
            Command(resume={"decision": "confirm", "selected": ["payment", "payment/refund"]}),
            config,
            stream_mode="updates",
        ))
        vals = graph.get_state(config).values
        assert vals["checklist_routed"] is True
        ctx = vals["checklist_context"]
        loaded = {c["rel_dir"]: c for c in ctx["checklists"]}
        assert set(loaded) == {"payment", "payment/refund"}
        assert "全额退款" in loaded["payment/refund"]["content"]
        assert vals["checklist_route"]["decision"] == "confirm"

    def test_resume_skip_no_context(self, gated):
        graph, config = gated
        list(graph.stream(Command(resume={"decision": "skip"}), config, stream_mode="updates"))
        vals = graph.get_state(config).values
        assert vals["checklist_routed"] is True
        assert vals["checklist_context"] is None
        assert vals["checklist_route"]["decision"] == "skip"

    def test_resume_string_confirm_all(self, gated):
        """裸字符串 resume（CLI 旧路径兼容）= confirm 无 selected → 不注入但不阻断。"""
        graph, config = gated
        list(graph.stream(Command(resume="confirm"), config, stream_mode="updates"))
        vals = graph.get_state(config).values
        assert vals["checklist_context"] is None  # 无 selected → 不注入，但不阻断

    def test_gate_reentry_after_routed(self, lib, monkeypatch):
        """已路由（skip 落库后回炉）→ gate 零开销放行。"""
        st = _state(lib) | {"checklist_routed": True}
        assert checklist_route_gate(st) == {}


class TestGateNodeEmpty:
    """空库 / 无匹配：门禁也必弹（上传入口），skip 或确认均可恢复。"""

    @pytest.fixture()
    def gated_empty(self, lib, monkeypatch):
        """无匹配但库有内容（status=no_match）推进到 gate 挂起。"""
        from devflow.checklist.models import RouteMatch

        async def fake_match(text, root):
            return RouteMatch()

        monkeypatch.setattr(cr, "match_businesses", fake_match)
        monkeypatch.setattr(cr, "resolve_root", lambda pr="": lib)
        graph = _mini_graph()
        config = {"configurable": {"thread_id": f"cr-e-{uuid.uuid4().hex[:8]}"}}
        list(graph.stream(_state(lib), config, stream_mode="updates"))
        return graph, config

    def test_empty_still_interrupts(self, gated_empty):
        graph, config = gated_empty
        snap = graph.get_state(config)
        assert snap.next == ("gate",)
        intr = snap.tasks[0].interrupts[0].value
        assert intr["type"] == "checklist_route"
        assert intr["status"] == "no_match"
        assert intr["business_count"] >= 1
        assert intr["candidates"] == []

    def test_empty_skip_resumes(self, gated_empty):
        graph, config = gated_empty
        list(graph.stream(Command(resume={"decision": "skip"}), config, stream_mode="updates"))
        vals = graph.get_state(config).values
        assert vals["checklist_routed"] is True
        assert vals["checklist_context"] is None

    def test_confirm_with_rel_not_in_candidates(self, gated_empty):
        """导入后的业务不在匹配候选里也能注入：load_checklists 以磁盘为准。"""
        graph, config = gated_empty
        list(graph.stream(
            Command(resume={"decision": "confirm", "selected": ["payment/refund"]}),
            config, stream_mode="updates",
        ))
        ctx = graph.get_state(config).values["checklist_context"]
        assert {c["rel_dir"] for c in ctx["checklists"]} == {"payment/refund"}


# ═══════════════════════════════════════════════════════════════════
# 全图接线 + server resume 组装
# ═══════════════════════════════════════════════════════════════════


class TestWiring:
    def test_nodes_registered_and_edged(self):
        graph = build_graph_with_providers()
        nodes = set(graph.get_graph().nodes)
        assert {"checklist_route_match", "checklist_route_gate"} <= nodes
        edges = {(e.source, e.target) for e in graph.get_graph().edges}
        assert ("checklist_route_match", "checklist_route_gate") in edges
        assert ("checklist_route_gate", "test_gen") in edges

    def test_gate_resume_dict(self):
        from web.server import _gate_resume

        # 裸决策（无附加信息）→ 字符串，旧门禁兼容
        assert _gate_resume("approve").resume == "approve"
        # 带 selected → 结构化 dict
        cmd = _gate_resume("confirm", "", "payment,payment/refund")
        assert cmd.resume == {
            "decision": "confirm",
            "selected": ["payment", "payment/refund"],
        }
        # 带 comment → dict（旧逻辑保留）
        assert _gate_resume("reject", "改一下").resume == {
            "decision": "reject",
            "comment": "改一下",
        }
