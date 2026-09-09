"""D1 结构化输出兼容层测试。

覆盖：
  - make_structured：function_calling 优先，失败自动退化 json_mode
  - mock LLM 兼容 method 参数
  - invoke_json 结构化路径在 function_calling 抛错时能退化成功
运行: pytest -v tests/test_structured_fallback.py
"""
from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from devflow.llm_client import _get_model, invoke_json


# ── 构造一个模拟「不支持 function_calling」的 LLM ───────────────
class _FcUnsupportedLLM:
    """with_structured_output(method='function_calling') 必抛；json_mode 可用。"""

    def __init__(self) -> None:
        self.used_methods: list[str] = []

    def with_structured_output(self, schema: Any, *, method: str = "function_calling"):
        self.used_methods.append(method)
        if method == "function_calling":
            raise ValueError("this vendor does not support tools/function calling")

        class _Structured:
            async def ainvoke(self, msgs: list[BaseMessage], **_: Any) -> Any:
                class _Out:
                    def model_dump(self, mode: str = "python") -> dict[str, Any]:
                        return {
                            "nodes": [
                                {"node_id": "n-1", "label": "In", "node_type": "io",
                                 "is_modified": False},
                                {"node_id": "n-2", "label": "Proc", "node_type": "module",
                                 "is_modified": True},
                            ],
                            "edges": [
                                {"edge_id": "e-1", "from_node": "n-1", "to_node": "n-2",
                                 "edge_type": "call", "condition": None, "is_modified": True},
                            ],
                            "mermaid_source": "graph TD\n n-1 --> n-2",
                        }
                return _Out()

        return _Structured()

    async def ainvoke(self, messages: list[BaseMessage], **_: Any) -> BaseMessage:
        return AIMessage(content="{}")


class TestMakeStructuredFallback:
    @pytest.mark.asyncio
    async def test_function_calling_then_json_mode_fallback(self, monkeypatch):
        """function_calling 抛错（供应商不支持 tools）→ 自动退化 json_mode 成功。"""
        llm = _FcUnsupportedLLM()

        # 让 _get_model 返回我们的伪 LLM：monkeypatch provider spec 已加载路径太复杂，
        # 直接测试底层 make_structured 的退化选择逻辑。
        from devflow.llm_client import _make_structured
        from pydantic import BaseModel, Field

        class _Graph(BaseModel):
            nodes: list[dict[str, Any]] = Field(default_factory=list)
            edges: list[dict[str, Any]] = Field(default_factory=list)
            mermaid_source: str = "graph TD\n n-1 --> n-2"

        structured = _make_structured(llm, _Graph, method=None)  # None = auto
        obj = await structured.ainvoke([HumanMessage(content="x")])
        assert obj.model_dump()["nodes"][0]["node_id"] == "n-1"
        assert llm.used_methods == ["function_calling", "json_mode"]

    @pytest.mark.asyncio
    async def test_function_calling_supported_uses_single_method(self, monkeypatch):
        """供应商支持 function_calling → 只尝试一次，不退化。"""
        from devflow.llm_client import _make_structured
        from pydantic import BaseModel

        class _Ok:
            def __init__(self) -> None:
                self.used_methods: list[str] = []

            def with_structured_output(self, schema: Any, *, method: str = "function_calling"):
                self.used_methods.append(method)

                class _S:
                    async def ainvoke(self, msgs: list[BaseMessage], **_: Any) -> Any:
                        class _Out:
                            def model_dump(self, mode: str = "python") -> dict[str, Any]:
                                return {"ok": True}
                        return _Out()
                return _S()

        llm = _Ok()
        structured = _make_structured(llm, _Ok, method=None)
        await structured.ainvoke([HumanMessage(content="x")])
        assert llm.used_methods == ["function_calling"]

    @pytest.mark.asyncio
    async def test_mock_llm_accepts_method_param(self):
        """mock LLM 的 with_structured_output 也要能吃 method 参数（不炸）。"""
        mock = _get_model("mock")
        from pydantic import BaseModel

        class _Graph(BaseModel):
            nodes: list[dict[str, Any]] = []
            edges: list[dict[str, Any]] = []
            mermaid_source: str = "graph TD\n n-1 --> n-2"

        structured = mock.with_structured_output(_Graph, method="json_mode")
        out = await structured.ainvoke([HumanMessage(content="请生成逻辑图")])
        d = out.model_dump(mode="json")
        assert "nodes" in d and "mermaid_source" in d


class TestInvokeJsonStructuredFallback:
    @pytest.mark.asyncio
    async def test_invoke_json_falls_back_on_fc_unsupported(self, monkeypatch):
        """invoke_json 结构化路径：function_calling 不可用 → json_mode 自动补上。"""
        from pydantic import BaseModel, Field

        llm = _FcUnsupportedLLM()
        monkeypatch.setattr(
            "devflow.llm_client._get_model", lambda spec: llm if isinstance(spec, dict) else _get_model(spec)
        )

        class _Graph(BaseModel):
            nodes: list[dict[str, Any]] = Field(default_factory=list)
            edges: list[dict[str, Any]] = Field(default_factory=list)
            mermaid_source: str = Field(min_length=5)

        # 直接驱动 _invoke_json_once（绕过 provider 循环）
        from devflow.llm_client import _invoke_json_once
        from devflow.resilience import CircuitBreaker, TokenBudget

        breaker = CircuitBreaker("t-fc")
        budget = TokenBudget()
        result = await _invoke_json_once(
            llm=llm,
            messages=[SystemMessage(content="s"), HumanMessage(content="生成图")],
            response_model=_Graph,
            json_schema=None,
            max_retries=0,
            breaker=breaker,
            budget=budget,
            response_type="logic_graph",
            model_spec="t-fc",
        )
        assert result["nodes"][0]["node_id"] == "n-1"
        assert llm.used_methods == ["function_calling", "json_mode"]


# ── 分支 B（json_schema / 裸 parser）的 object 类型守卫 ──────────
class _ScriptedLLM:
    """按脚本顺序返回 content 的伪 LLM：模拟弱模型输出数字开头纯文本。"""

    def __init__(self, contents: list[str]) -> None:
        self._contents = contents
        self.calls = 0

    async def ainvoke(self, messages: list[BaseMessage], **_: Any) -> BaseMessage:
        content = self._contents[min(self.calls, len(self._contents) - 1)]
        self.calls += 1
        return AIMessage(content=content)


_QUESTION_SCHEMA = {
    "type": "object",
    "required": ["questions"],
    "properties": {"questions": {"type": "string", "minLength": 2}},
}


class TestInvokeJsonObjectTypeGuard:
    @pytest.mark.asyncio
    async def test_bare_int_output_raises_output_format_error(self):
        """回归：模型输出裸数字 → 必须归一为 LlmOutputFormatError，而不是把 int
        透传给调用方去下标取值（历史症状：'int' object is not subscriptable）。"""
        from devflow.errors import LlmOutputFormatError
        from devflow.llm_client import _invoke_json_once
        from devflow.resilience import CircuitBreaker, TokenBudget

        llm = _ScriptedLLM(["123"])
        with pytest.raises(LlmOutputFormatError) as ei:
            await _invoke_json_once(
                llm=llm,
                messages=[SystemMessage(content="s"), HumanMessage(content="请输出追问")],
                response_model=None,
                json_schema=_QUESTION_SCHEMA,
                max_retries=0,
                breaker=CircuitBreaker("t-int"),
                budget=TokenBudget(),
                response_type="clarify_question",
                model_spec="t-int",
            )
        assert "expected object, got int" in " ".join(ei.value.extra.get("validation_errors", []))

    @pytest.mark.asyncio
    async def test_digit_led_output_retried_into_object(self):
        """模型首轮输出「1. …」数字开头纯文本 → 守卫拦下 → 重试拿到合法 object。"""
        from devflow.llm_client import _invoke_json_once
        from devflow.resilience import CircuitBreaker, TokenBudget

        llm = _ScriptedLLM([
            "1. 请补充边界场景（如除零）？",
            '{"questions": "请补充边界场景（如除零）？"}',
        ])
        result = await _invoke_json_once(
            llm=llm,
            messages=[SystemMessage(content="s"), HumanMessage(content="请输出追问")],
            response_model=None,
            json_schema=_QUESTION_SCHEMA,
            max_retries=1,
            breaker=CircuitBreaker("t-retry"),
            budget=TokenBudget(),
            response_type="clarify_question",
            model_spec="t-retry",
        )
        assert llm.calls == 2
        assert result["questions"].startswith("请补充边界场景")

    @pytest.mark.asyncio
    async def test_object_schema_without_type_declaration_untouched(self):
        """schema 未声明 type=object 时不加守卫（保持旧行为：数组等合法 JSON 直接透传）。"""
        from devflow.llm_client import _invoke_json_once
        from devflow.resilience import CircuitBreaker, TokenBudget

        llm = _ScriptedLLM(['["a", "b"]'])
        result = await _invoke_json_once(
            llm=llm,
            messages=[SystemMessage(content="s"), HumanMessage(content="列出")],
            response_model=None,
            json_schema={"type": "array", "items": {"type": "string"}},
            max_retries=0,
            breaker=CircuitBreaker("t-arr"),
            budget=TokenBudget(),
            response_type="general",
            model_spec="t-arr",
        )
        assert result == ["a", "b"]
