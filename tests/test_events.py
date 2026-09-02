"""events_from_stream：langgraph stream 解包 → 事件流。

CLI 与 Web（SSE）共用的唯一事件源。覆盖：
  1. 单键 dict {'node': update}（langgraph 1.x）
  2. {'__interrupt__': ...} → gate 事件
  3. update 为 None（节点无更新）→ 静默跳过
  4. 旧版 (node, update) 元组兼容
  5. 各 artifact 字段映射（logic_graph / code_context / code_changes / test_report）
  6. missing_fields / questions → question 事件；last_error → error 事件
"""
from __future__ import annotations

from langgraph.types import Interrupt

from devflow.events import events_from_stream


def _seq(events) -> list[dict]:
    return list(events)


# ═══════════════════════════════════════════════════════════════════
# 单键 dict（langgraph 1.x）
# ═══════════════════════════════════════════════════════════════════


class TestDictItems:
    def test_full_chain_event_sequence(self):
        items = [
            {"compress_messages": None},
            {"clarify_validate": {"missing_fields": [], "current_stage": "graph"}},
            {"graph_generate": {"logic_graph": {"graph_id": "g1"}}},
            {"code_search": {"code_context": [{"file_path": "a.py"}]}},
            {"code_gen": {"code_changes": [{"file_path": "b.py", "action": "create"}]}},
            {"test_gen": {"test_report": {"run": {"passed": 1, "failed": 0}}}},
        ]
        events = _seq(events_from_stream(iter(items)))
        assert [e["type"] for e in events] == [
            "stage", "artifact", "artifact", "artifact", "artifact",
        ]
        kinds = [e.get("kind") for e in events if e["type"] == "artifact"]
        assert kinds == ["logic_graph", "code_context", "code_changes", "test_report"]

    def test_none_update_skipped(self):
        events = _seq(events_from_stream(iter([{"compress_messages": None}])))
        assert events == []

    def test_empty_update_skipped(self):
        events = _seq(events_from_stream(iter([{"clarify_validate": {}}])))
        assert events == []


# ═══════════════════════════════════════════════════════════════════
# __interrupt__ → gate 事件
# ═══════════════════════════════════════════════════════════════════


class TestInterrupt:
    def test_interrupt_becomes_gate_event(self):
        payload = {"type": "human_review", "summary": "验收摘要", "code_changes_count": 2}
        items = [
            {"graph_generate": {"logic_graph": {"graph_id": "g1"}}},
            {"__interrupt__": (Interrupt(value=payload),)},
        ]
        events = _seq(events_from_stream(iter(items)))
        gates = [e for e in events if e["type"] == "gate"]
        assert len(gates) == 1
        assert gates[0]["gate"] == "human_review"
        assert gates[0]["payload"] == payload

    def test_graph_review_gate_tagged(self):
        payload = {"type": "graph_review", "graph_id": "g1", "nodes": 3, "edges": 2}
        items = [{"__interrupt__": (Interrupt(value=payload),)}]
        events = _seq(events_from_stream(iter(items)))
        assert events[0]["gate"] == "graph_review"

    def test_gate_after_artifacts_keeps_order(self):
        payload = {"type": "human_review"}
        items = [
            {"test_gen": {"test_report": {"run": {}}}},
            {"__interrupt__": (Interrupt(value=payload),)},
        ]
        events = _seq(events_from_stream(iter(items)))
        assert [e["type"] for e in events] == ["artifact", "gate"]


# ═══════════════════════════════════════════════════════════════════
# 旧版 (node, update) 元组兼容
# ═══════════════════════════════════════════════════════════════════


class TestLegacyTuples:
    def test_tuple_items_unsupported_removed(self):
        # 兼容层只接受 dict 与 (node, update) 元组
        items = [
            ("graph_generate", {"logic_graph": {"graph_id": "g1"}}),
            ("test_gen", {"test_report": {"run": {}}}),
        ]
        events = _seq(events_from_stream(iter(items)))
        assert [e.get("kind") for e in events] == ["logic_graph", "test_report"]


# ═══════════════════════════════════════════════════════════════════
# question / error 事件
# ═══════════════════════════════════════════════════════════════════


class TestQuestionAndError:
    def test_missing_fields_becomes_question(self):
        items = [{"clarify_validate": {"missing_fields": ["target_modules"], "current_stage": "clarify"}}]
        events = _seq(events_from_stream(iter(items)))
        q = [e for e in events if e["type"] == "question"]
        assert q and q[0]["missing"] == ["target_modules"]

    def test_build_question_questions_field(self):
        items = [{"clarify_build_question": {"questions": ["需要补充哪些模块？"]}}]
        events = _seq(events_from_stream(iter(items)))
        q = [e for e in events if e["type"] == "question"]
        assert q and q[0]["questions"] == ["需要补充哪些模块？"]

    def test_messages_event(self):
        from langchain_core.messages import AIMessage

        items = [{"clarify_build_question": {"messages": [AIMessage(content="需要补充：\n- target_modules")]}}]
        events = _seq(events_from_stream(iter(items)))
        msgs = [e for e in events if e["type"] == "messages"]
        assert len(msgs) == 1
        assert msgs[0]["messages"][0]["type"] == "ai"
        assert "target_modules" in msgs[0]["messages"][0]["content"]

    def test_last_error_becomes_error(self):
        items = [{"graph_generate": {"last_error": "[graph_generate:LLM.XXX] 失败", "last_error_code": "LLM.XXX"}}]
        events = _seq(events_from_stream(iter(items)))
        err = [e for e in events if e["type"] == "error"]
        assert err and "LLM.XXX" in err[0]["error"]