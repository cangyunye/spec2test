"""D1b 结构化输出解析失败的异常分类修正测试。

背景：DeepSeek function calling 输出偶发不严格符合 json_schema
（如 code_ref 输出成字符串而非对象）→ langchain 抛 OutputParserException。
此前被 wrap 成 NODE.CONTEXT（不可重试）→ 直接 mock 兜底；
修正后应归类 LLM.OUTPUT_FORMAT（可重试），让重试机制生效。

覆盖：
  - _make_structured 内 OutputParserException → LlmOutputFormatError（retryable=True）
  - 解析失败且 json_mode 也失败 → 抛出的仍是 LlmOutputFormatError（可重试）
运行: pytest -v tests/test_structured_parse_error.py
"""
from __future__ import annotations

from typing import Any

import pytest
from langchain_core.exceptions import OutputParserException
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from pydantic import BaseModel, Field

from devflow.errors import LLM_OUTPUT_FORMAT, LlmOutputFormatError
from devflow.llm_client import _make_structured


class _NeverParses:
    """with_structured_output 每次调用都抛 OutputParserException（模拟模型输出不符合 schema）；
    裸 ainvoke 也只回不可解析的散文（prompt_json 档同样解析失败）。"""

    def __init__(self) -> None:
        self.methods_used: list[str] = []

    def with_structured_output(self, schema: Any, *, method: str = "function_calling"):
        self.methods_used.append(method)

        class _S:
            async def ainvoke(self, msgs: list[BaseMessage], **_: Any) -> Any:
                raise OutputParserException("Failed to parse X from completion {...}")
        return _S()

    async def ainvoke(self, messages: list[BaseMessage], **_: Any) -> BaseMessage:
        return AIMessage(content="抱歉，我需要更多信息才能回答。")


class _Graph(BaseModel):
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    edges: list[dict[str, Any]] = Field(default_factory=list)
    mermaid_source: str = Field(min_length=5)


class TestParseErrorClassification:
    @pytest.mark.asyncio
    async def test_parse_error_becomes_output_format_error(self):
        """OutputParserException 必须转成 LlmOutputFormatError（retryable=True）。"""
        llm = _NeverParses()
        structured = _make_structured(llm, _Graph, method=None)
        with pytest.raises(LlmOutputFormatError) as ei:
            await structured.ainvoke([HumanMessage(content="x")])
        assert ei.value.code == LLM_OUTPUT_FORMAT
        assert ei.value.retryable is True
        # 两个 method 都试过了（function_calling → json_mode）
        assert llm.methods_used == ["function_calling", "json_mode"]

    @pytest.mark.asyncio
    async def test_parse_error_keeps_retryable_after_full_fallback(self):
        """即便两个 method 都解析失败，最终错误仍是 LlmOutputFormatError（可重试），
        而非被 wrap 成 NODE.CONTEXT（不可重试）。"""
        llm = _NeverParses()
        structured = _make_structured(llm, _Graph, method=None)
        try:
            await structured.ainvoke([HumanMessage(content="x")])
        except LlmOutputFormatError as e:
            assert e.retryable is True
            assert "解析失败" in e.message


class TestLogicNodeCodeRefLenient:
    """模型把 code_ref 输出成字符串而非对象时，不应导致整图解析失败。"""

    def test_logic_node_accepts_str_code_ref(self):
        from devflow.nodes.graph_gen import _LogicNode

        n = _LogicNode(
            node_id="n-1", label="x", node_type="module",
            is_modified=False, code_ref="app/auth/login.py",
        )
        assert n.code_ref == "app/auth/login.py"

    def test_normalize_str_code_ref_to_object(self):
        from devflow.nodes.graph_gen import _normalize_code_ref

        assert _normalize_code_ref(None) is None
        assert _normalize_code_ref("app/auth/login.py") == {
            "file_path": "app/auth/login.py",
            "symbol": None,
        }
        d = {"file_path": "a.py", "symbol": "f", "line_start": 1, "line_end": 5}
        assert _normalize_code_ref(d) == d

    @pytest.mark.asyncio
    async def test_graph_generate_normalizes_str_code_ref(self, monkeypatch):
        """invoke_json 返回含字符串 code_ref 的图 → 输出 logic_graph 中 code_ref 已是对象。"""
        async def fake_invoke_json(**kwargs):
            return {
                "nodes": [
                    {"node_id": "n-1", "label": "In", "node_type": "io",
                     "is_modified": False, "code_ref": None},
                    {"node_id": "n-2", "label": "Proc", "node_type": "module",
                     "is_modified": True, "code_ref": "app/auth/login.py"},  # 字符串！
                ],
                "edges": [
                    {"edge_id": "e-1", "from_node": "n-1", "to_node": "n-2",
                     "edge_type": "call", "condition": None, "is_modified": True},
                ],
                "mermaid_source": "graph TD\n n-1 --> n-2",
            }

        monkeypatch.setattr("devflow.nodes.graph_gen.invoke_json", fake_invoke_json)
        from devflow.nodes.graph_gen import graph_generate_async

        out = await graph_generate_async(
            {"requirement": {"project_root": "/x"}, "code_context": [], "retry_count": {}}
        )
        assert out.get("last_error") is None
        nodes = out["logic_graph"]["nodes"]
        proc = next(n for n in nodes if n["node_id"] == "n-2")
        assert proc["code_ref"] == {
            "file_path": "app/auth/login.py",
            "symbol": None,
        }
        # 整图仍能通过拓扑校验
        from devflow.schemas import validate_logic_graph
        assert validate_logic_graph(out["logic_graph"]) == []
