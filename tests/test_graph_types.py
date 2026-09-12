"""图种类选择功能：注册表/推断、mermaid_fix 多类型、多类型校验、
graph_type_select 门禁解析、graph_generate 按种类分叉与 nodes/edges 投影。"""
from __future__ import annotations

import pytest

from devflow.graph_types import (
    DEFAULT_GRAPH_TYPE,
    GRAPH_TYPE_IDS,
    normalize_graph_type,
    suggest_graph_types,
)
from devflow.mermaid_fix import mermaid_problems, sanitize_mermaid
from devflow.schemas import validate_logic_graph


# ═══════════════════════════════════════════════════════════════════
# 1. 注册表与推断
# ═══════════════════════════════════════════════════════════════════


class TestSuggestGraphTypes:
    def test_default_recommends_flowchart_only(self):
        req = {"project_context": "一个简单的加法计算器", "edge_cases": ["空输入"]}
        cands = {c["id"]: c for c in suggest_graph_types(req)}
        assert cands["flowchart"]["recommended"] is True
        assert cands["sequence"]["recommended"] is False
        assert cands["state"]["recommended"] is False
        assert cands["er"]["recommended"] is False

    def test_state_keywords_recommend_state(self):
        req = {
            "project_context": "订单系统，订单状态需要流转",
            "edge_cases": ["待支付订单超时关闭", "审批被驳回"],
            "acceptance_criteria": ["状态机流转正确"],
        }
        cands = {c["id"]: c for c in suggest_graph_types(req)}
        assert cands["state"]["recommended"] is True
        assert cands["state"]["hit_count"] >= 2
        assert cands["state"]["reason"]

    def test_er_keywords_recommend_er(self):
        req = {
            "project_context": "为电商系统建表，新增订单表和字段",
            "acceptance_criteria": ["数据库迁移可回滚"],
        }
        cands = {c["id"]: c for c in suggest_graph_types(req)}
        assert cands["er"]["recommended"] is True

    def test_sequence_keywords_recommend_sequence(self):
        req = {
            "project_context": "对接第三方支付接口，回调通知服务端",
            "io_constraints": {"input": "HTTP 请求", "output": "响应 JSON"},
        }
        cands = {c["id"]: c for c in suggest_graph_types(req)}
        assert cands["sequence"]["recommended"] is True

    def test_journey_keywords_recommend_journey(self):
        req = {
            "project_context": "优化下单用户旅程，减少关键触点的体验痛点",
            "edge_cases": ["新用户首次使用流程卡顿"],
            "acceptance_criteria": ["核心操作步骤满意度提升"],
        }
        cands = {c["id"]: c for c in suggest_graph_types(req)}
        assert cands["journey"]["recommended"] is True
        assert cands["journey"]["hit_count"] >= 2
        assert cands["journey"]["reason"]

    def test_all_candidates_always_present_and_flowchart_first(self):
        cands = suggest_graph_types({})
        assert [c["id"] for c in cands][0] == "flowchart"
        assert {c["id"] for c in cands} == GRAPH_TYPE_IDS

    def test_empty_requirement_safe(self):
        assert suggest_graph_types(None)[0]["id"] == "flowchart"


class TestNormalizeGraphType:
    def test_valid_ids_passthrough(self):
        for tid in GRAPH_TYPE_IDS:
            assert normalize_graph_type(tid) == tid

    def test_none_and_garbage_fallback_to_default(self):
        assert normalize_graph_type(None) == DEFAULT_GRAPH_TYPE
        assert normalize_graph_type("") == DEFAULT_GRAPH_TYPE
        assert normalize_graph_type("mindmap") == DEFAULT_GRAPH_TYPE

    def test_chinese_alias(self):
        assert normalize_graph_type("时序图") == "sequence"
        assert normalize_graph_type("状态图") == "state"
        assert normalize_graph_type("ER图") == "er"
        assert normalize_graph_type("用户旅程图") == "journey"
        assert normalize_graph_type("旅程图") == "journey"


# ═══════════════════════════════════════════════════════════════════
# 2. mermaid_fix 多类型
# ═══════════════════════════════════════════════════════════════════


class TestSanitizeTypedMermaid:
    def test_sequence_keeps_header(self):
        src = "sequenceDiagram\n  A->>B: 你好(含括号)"
        out = sanitize_mermaid(src, "sequence")
        assert out.startswith("sequenceDiagram")
        # 非 flowchart 不做标签加引号（会破坏时序语法）
        assert '你好(含括号)' in out and '"你好(含括号)"' not in out

    def test_sequence_missing_header_gets_prepended(self):
        out = sanitize_mermaid("A->>B: hi", "sequence")
        assert out.splitlines()[0] == "sequenceDiagram"

    def test_state_missing_header_gets_prepended(self):
        out = sanitize_mermaid("[*] --> s1", "state")
        assert out.splitlines()[0] == "stateDiagram-v2"

    def test_er_missing_header_gets_prepended(self):
        out = sanitize_mermaid("A ||--o{ B : has", "er")
        assert out.splitlines()[0] == "erDiagram"

    def test_journey_missing_header_gets_prepended(self):
        out = sanitize_mermaid("打开应用: 5: 用户\n支付订单(含优惠券): 3: 用户", "journey")
        assert out.splitlines()[0] == "journey"
        # 非 flowchart 不做标签加引号（journey 任务行以冒号分隔，加引号会原样显示）
        assert '"支付订单(含优惠券)"' not in out
        assert "支付订单(含优惠券): 3: 用户" in out

    def test_flowchart_still_injects_flowchart_td(self):
        out = sanitize_mermaid("n-1[表单(必填)] --> n-2", "flowchart")
        assert out.splitlines()[0] == "flowchart TD"
        assert '"表单(必填)"' in out

    def test_fence_and_literal_newline_fixed_for_all_types(self):
        src = "```mermaid\nsequenceDiagram\\n  A->>B: hi\\n```"
        out = sanitize_mermaid(src, "sequence")
        assert "```" not in out
        assert "\n" in out


class TestProblemsTypedMermaid:
    def test_sequence_no_false_header_problem(self):
        assert mermaid_problems("sequenceDiagram\n  A->>B: hi", "sequence") == []

    def test_sequence_missing_header_reported(self):
        problems = mermaid_problems("A->>B: hi", "sequence")
        assert any("sequenceDiagram" in p for p in problems)

    def test_journey_valid_no_problems(self):
        src = "journey\n  section 下单\n    打开应用: 5: 用户\n    支付订单: 3: 用户"
        assert mermaid_problems(src, "journey") == []

    def test_journey_missing_header_reported(self):
        problems = mermaid_problems("打开应用: 5: 用户", "journey")
        assert any("journey" in p for p in problems)

    def test_flowchart_undefined_class_still_checked(self):
        problems = mermaid_problems(
            "flowchart TD\n  n-1 --> n-2:::modified", "flowchart"
        )
        assert any("modified" in p for p in problems)


# ═══════════════════════════════════════════════════════════════════
# 3. 多类型结构化校验
# ═══════════════════════════════════════════════════════════════════


def _sequence_graph() -> dict:
    return {
        "graph_id": "g-s1",
        "graph_type": "sequence",
        "participants": [
            {"alias": "FE", "label": "前端", "kind": "actor", "is_modified": True},
            {"alias": "API", "label": "后端", "kind": "service", "is_modified": True},
        ],
        "messages": [
            {"msg_id": "m1", "from_participant": "FE", "to_participant": "API",
             "label": "提交", "kind": "sync", "is_modified": True},
            {"msg_id": "m2", "from_participant": "API", "to_participant": "FE",
             "label": "返回", "kind": "return", "is_modified": False},
        ],
        "mermaid_source": "sequenceDiagram\n  FE->>API: 提交\n  API-->>FE: 返回",
    }


def _state_graph() -> dict:
    return {
        "graph_id": "g-s2",
        "graph_type": "state",
        "states": [
            {"state_id": "s1", "label": "初始", "kind": "initial", "is_modified": False},
            {"state_id": "s2", "label": "完成", "kind": "final", "is_modified": True},
        ],
        "transitions": [
            {"trans_id": "t1", "from_state": "s1", "to_state": "s2",
             "event": "确认", "is_modified": True},
        ],
        "mermaid_source": "stateDiagram-v2\n  [*] --> s1\n  s1 --> s2: 确认\n  s2 --> [*]",
    }


def _er_graph() -> dict:
    return {
        "graph_id": "g-s3",
        "graph_type": "er",
        "entities": [
            {"e_id": "n-user", "table": "USER", "is_modified": False, "attributes": [
                {"name": "user_id", "type": "int", "is_pk": True}]},
            {"e_id": "n-order", "table": "ORDER", "is_modified": True, "attributes": [
                {"name": "order_id", "type": "int", "is_pk": True}]},
        ],
        "relations": [
            {"rel_id": "r1", "from_entity": "n-user", "to_entity": "n-order",
             "cardinality": "one_to_many", "label": "places", "is_modified": True},
        ],
        "mermaid_source": "erDiagram\n  USER ||--o{ ORDER : places",
    }


def _journey_graph() -> dict:
    return {
        "graph_id": "g-s4",
        "graph_type": "journey",
        "sections": [
            {"section_id": "sec1", "label": "下单支付", "is_modified": True},
        ],
        "tasks": [
            {"task_id": "j1", "label": "打开应用", "score": 7, "actors": ["用户"],
             "section_id": None, "is_modified": False},
            {"task_id": "j2", "label": "加入购物车", "score": 5, "actors": ["用户"],
             "section_id": "sec1", "is_modified": True},
            {"task_id": "j3", "label": "完成支付", "score": 8, "actors": ["用户", "商家"],
             "section_id": "sec1", "is_modified": False},
        ],
        "mermaid_source": (
            "journey\n"
            "  打开应用: 7: 用户\n"
            "  section 下单支付\n"
            "    加入购物车: 5: 用户\n"
            "    完成支付: 8: 用户, 商家"
        ),
    }


class TestValidateTypedGraphs:
    @pytest.mark.parametrize(
        "graph", [_sequence_graph(), _state_graph(), _er_graph(), _journey_graph()]
    )
    def test_valid_typed_graphs_pass(self, graph):
        assert validate_logic_graph(graph) == []

    def test_sequence_unknown_participant_reference(self):
        g = _sequence_graph()
        g["messages"][0]["from_participant"] = "GHOST"
        errors = validate_logic_graph(g)
        assert any("GHOST" in e for e in errors)

    def test_state_unknown_state_reference(self):
        g = _state_graph()
        g["transitions"][0]["to_state"] = "s_ghost"
        errors = validate_logic_graph(g)
        assert any("s_ghost" in e for e in errors)

    def test_er_single_entity_rejected(self):
        g = _er_graph()
        g["entities"] = g["entities"][:1]
        assert validate_logic_graph(g)

    def test_journey_unknown_section_reference(self):
        g = _journey_graph()
        g["tasks"][1]["section_id"] = "sec_ghost"
        errors = validate_logic_graph(g)
        assert any("sec_ghost" in e for e in errors)

    def test_journey_ungrouped_task_passes(self):
        g = _journey_graph()
        g["tasks"][1]["section_id"] = None
        assert validate_logic_graph(g) == []

    def test_journey_single_task_rejected(self):
        g = _journey_graph()
        g["tasks"] = g["tasks"][:1]  # 少于 minItems 2
        assert validate_logic_graph(g)

    def test_flowchart_unchanged_path(self):
        g = {
            "graph_id": "g-f1",
            "nodes": [{"node_id": "n-1", "label": "A", "node_type": "io", "is_modified": True}],
            "edges": [],
            "mermaid_source": "flowchart TD\n  n-1[A]",
        }
        assert validate_logic_graph(g) == []

    def test_unknown_graph_type_rejected(self):
        assert validate_logic_graph({"graph_type": "mindmap"}) != []


# ═══════════════════════════════════════════════════════════════════
# 4. graph_type_select 门禁：resume 解析与已选放行
# ═══════════════════════════════════════════════════════════════════


class TestGraphTypeSelectNode:
    def test_parse_choice_str_and_dict(self):
        from devflow.nodes.graph_type import _parse_choice

        assert _parse_choice("sequence") == "sequence"
        assert _parse_choice({"graph_type": "er"}) == "er"
        assert _parse_choice({"decision": "state"}) == "state"
        assert _parse_choice("时序图") == "sequence"
        assert _parse_choice(None) == "flowchart"
        assert _parse_choice("nonsense") == "flowchart"

    def test_existing_graph_type_passes_through_without_interrupt(self):
        from devflow.nodes.graph_type import graph_type_select

        out = graph_type_select({"graph_type": "sequence", "requirement": {}})
        assert out == {"current_stage": "graph"}

    def test_candidates_payload_shape(self):
        from devflow.graph_types import suggest_graph_types

        req = {"project_context": "订单状态流转", "edge_cases": ["订单取消"]}
        cands = suggest_graph_types(req)
        for c in cands:
            assert {"id", "label", "desc", "recommended", "reason", "hit_count"} <= set(c)


# ═══════════════════════════════════════════════════════════════════
# 5. graph_generate 按种类分叉 + nodes/edges 投影
# ═══════════════════════════════════════════════════════════════════


class TestGraphGenerateTyped:
    async def _generate(self, graph_type: str, payload: dict, monkeypatch) -> dict:
        async def fake_invoke_json(**kwargs):
            return payload

        monkeypatch.setattr("devflow.nodes.graph_gen.invoke_json", fake_invoke_json)
        from devflow.nodes.graph_gen import graph_generate_async

        return await graph_generate_async(
            {"requirement": {"project_context": "x"}, "code_context": [],
             "graph_type": graph_type, "retry_count": {}}
        )

    @pytest.mark.asyncio
    async def test_sequence_generation_projects_nodes(self, monkeypatch):
        out = await self._generate("sequence", _sequence_graph(), monkeypatch)
        assert out["last_error"] is None
        g = out["logic_graph"]
        assert g["graph_type"] == "sequence"
        # 投影兜底：participants → nodes（actor→io），messages → edges
        assert [n["node_type"] for n in g["nodes"]] == ["io", "module"]
        assert len(g["edges"]) == 2
        assert g["edges"][0]["edge_type"] == "call"
        assert g["edges"][1]["edge_type"] == "data_flow"
        assert validate_logic_graph(g) == []

    @pytest.mark.asyncio
    async def test_state_generation_projects_nodes(self, monkeypatch):
        out = await self._generate("state", _state_graph(), monkeypatch)
        assert out["last_error"] is None
        g = out["logic_graph"]
        assert [n["node_type"] for n in g["nodes"]] == ["io", "io"]
        assert g["edges"][0]["edge_type"] == "condition"
        assert g["edges"][0]["condition"] == "确认"
        assert validate_logic_graph(g) == []

    @pytest.mark.asyncio
    async def test_er_generation_projects_nodes(self, monkeypatch):
        out = await self._generate("er", _er_graph(), monkeypatch)
        assert out["last_error"] is None
        g = out["logic_graph"]
        assert all(n["node_type"] == "module" for n in g["nodes"])
        assert g["edges"][0]["from_node"] == "n-user"
        assert validate_logic_graph(g) == []

    @pytest.mark.asyncio
    async def test_journey_generation_projects_nodes(self, monkeypatch):
        out = await self._generate("journey", _journey_graph(), monkeypatch)
        assert out["last_error"] is None
        g = out["logic_graph"]
        assert g["graph_type"] == "journey"
        # 投影兜底：tasks → nodes（触点→io），按旅程顺序两两连 data_flow 边
        assert all(n["node_type"] == "io" for n in g["nodes"])
        assert [n["node_id"] for n in g["nodes"]] == ["n-1", "n-2", "n-3"]
        assert len(g["edges"]) == 2
        assert all(e["edge_type"] == "data_flow" for e in g["edges"])
        # 边的 is_modified 取两端任务并集：j1(false)-j2(true) → true；j2(true)-j3(false) → true
        assert [e["is_modified"] for e in g["edges"]] == [True, True]
        assert validate_logic_graph(g) == []

    @pytest.mark.asyncio
    async def test_invalid_typed_payload_reports_errors(self, monkeypatch):
        bad = _sequence_graph()
        bad["messages"] = bad["messages"][:1]  # 少于 minItems 2
        out = await self._generate("sequence", bad, monkeypatch)
        assert out["logic_graph"] is None
        assert out["last_error"]
        assert out["missing_fields"]

    @pytest.mark.asyncio
    async def test_projection_skips_dangling_edges(self, monkeypatch):
        g = _state_graph()
        g["transitions"].append(
            {"trans_id": "t9", "from_state": "s2", "to_state": "s_ghost",
             "event": None, "is_modified": False})
        # 原生数据校验会拦住 dangling 引用；这里验证投影层不崩（校验在前返回错误）
        out = await self._generate("state", g, monkeypatch)
        assert out["logic_graph"] is None  # 被引用完整性校验拦截


# ═══════════════════════════════════════════════════════════════════
# 6. Mock 兜底按种类返回
# ═══════════════════════════════════════════════════════════════════


class TestMockDispatchByType:
    def test_mock_payload_markers(self):
        from devflow.llm_client import _mock_graph_payload

        assert _mock_graph_payload("第一行必须是 sequenceDiagram")["participants"]
        assert _mock_graph_payload("第一行必须是 stateDiagram-v2")["states"]
        assert _mock_graph_payload("第一行必须是 erDiagram")["entities"]
        assert _mock_graph_payload("第一行必须是 `journey`")["tasks"]
        assert _mock_graph_payload("普通需求澄清提示词") is None

    @pytest.mark.asyncio
    async def test_mock_fallback_sequence_end_to_end(self, monkeypatch):
        """无真实 provider 时（Mock 兜底），sequence 制图全链路产出合法结构。"""
        import asyncio

        from devflow.nodes.graph_gen import graph_generate

        # 强制 mock：清空 providers 不可行（settings 全局），直接拦 _candidates_providers
        import devflow.llm_client as lc

        monkeypatch.setattr(lc, "_candidates_providers", lambda: [])
        out = await asyncio.to_thread(
            graph_generate,
            {"requirement": {"project_context": "x"}, "code_context": [],
             "graph_type": "sequence", "retry_count": {}},
        )
        assert out["last_error"] is None
        g = out["logic_graph"]
        assert g["graph_type"] == "sequence"
        assert g["participants"] and g["messages"]
        assert g["nodes"] and g["edges"]  # 投影兜底就位
        assert g["mermaid_source"].startswith("sequenceDiagram")

    @pytest.mark.asyncio
    async def test_mock_fallback_journey_end_to_end(self, monkeypatch):
        """无真实 provider 时（Mock 兜底），journey 制图全链路产出合法结构。"""
        import asyncio

        from devflow.nodes.graph_gen import graph_generate

        import devflow.llm_client as lc

        monkeypatch.setattr(lc, "_candidates_providers", lambda: [])
        out = await asyncio.to_thread(
            graph_generate,
            {"requirement": {"project_context": "x"}, "code_context": [],
             "graph_type": "journey", "retry_count": {}},
        )
        assert out["last_error"] is None
        g = out["logic_graph"]
        assert g["graph_type"] == "journey"
        assert g["sections"] and g["tasks"]
        assert g["nodes"] and g["edges"]  # 投影兜底就位
        assert validate_logic_graph(g) == []
        assert g["mermaid_source"].startswith("journey")
