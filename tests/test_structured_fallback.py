"""D1 结构化输出兼容层测试。

覆盖：
  - make_structured：function_calling 优先，失败自动退化 json_mode
  - D1c 第三档 prompt_json：tools/response_format 皆不支持的裸网关（自部署 vLLM/Ollama）
  - mock LLM 兼容 method 参数
  - invoke_json 结构化路径在 function_calling 抛错时能退化成功
运行: pytest -v tests/test_structured_fallback.py
"""
from __future__ import annotations

from typing import Any, ClassVar

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, Field

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


# ── D1c 第三档退化：prompt_json（裸网关，tools/response_format 皆不支持）──
class _NoStructuredSupportLLM:
    """模拟裸网关（vLLM 未开 tool-choice / 旧 Ollama 兼容层）：
    with_structured_output 无论 method 一律 400；裸 ainvoke 可用（能补全）。"""

    def __init__(self, contents: list[str]) -> None:
        self._contents = contents
        self.ws_calls: list[str] = []
        self.ainvoke_calls = 0

    def with_structured_output(self, schema: Any, *, method: str = "function_calling"):
        self.ws_calls.append(method)
        raise ValueError("Error code: 400 - {'error': {'message': 'tools is not supported'}}")

    async def ainvoke(self, messages: list[BaseMessage], **_: Any) -> BaseMessage:
        content = self._contents[min(self.ainvoke_calls, len(self._contents) - 1)]
        self.ainvoke_calls += 1
        return AIMessage(content=content)


class _Req(BaseModel):
    req_type: str | None = None
    project_context: str | None = None


def _drive_structured(llm: Any, model: type, *, max_retries: int = 0):
    from devflow.llm_client import _invoke_json_once
    from devflow.resilience import CircuitBreaker, TokenBudget

    return _invoke_json_once(
        llm=llm,
        messages=[SystemMessage(content="s"), HumanMessage(content="抽取需求")],
        response_model=model,
        json_schema=None,
        max_retries=max_retries,
        breaker=CircuitBreaker("t-pj"),
        budget=TokenBudget(),
        response_type="requirement_extract",
        model_spec="t-pj",
    )


class TestPromptJsonFallbackTier:
    @pytest.mark.asyncio
    async def test_bare_gateway_degrades_to_prompt_json(self):
        """tools/response_format 均 400 → 退化 prompt_json：schema 进提示词 + 本地校验。"""
        llm = _NoStructuredSupportLLM([
            '<think>让我想想</think>\n{"req_type": "new_feature", "project_context": "桌面计算器"}'
        ])
        result = await _drive_structured(llm, _Req)
        assert result == {"req_type": "new_feature", "project_context": "桌面计算器"}
        assert llm.ws_calls == ["function_calling", "json_mode"]  # 两档都试过才退化
        assert llm.ainvoke_calls == 1

    @pytest.mark.asyncio
    async def test_retry_reuses_prompt_json_without_rehitting_400(self):
        """纠错重试记住上次成功的档位：不再反复撞 function_calling/json_mode 的 400。"""
        llm = _NoStructuredSupportLLM([
            "1. 先聊聊需求再说",           # 首轮：宽容解析成 int → 被守卫拦下
            '{"req_type": "bug_fix"}',    # 重试：合法
        ])
        result = await _drive_structured(llm, _Req, max_retries=1)
        assert result["req_type"] == "bug_fix"
        assert llm.ainvoke_calls == 2
        # 两次 400 只发生在首轮探测；重试直接走 prompt_json
        assert llm.ws_calls == ["function_calling", "json_mode"]

    @pytest.mark.asyncio
    async def test_bare_int_output_raises_output_format(self):
        """prompt_json 档下模型输出裸数字 → LlmOutputFormatError（可重试），不漏 TypeError。"""
        from devflow.errors import LlmOutputFormatError

        llm = _NoStructuredSupportLLM(["123"])
        with pytest.raises(LlmOutputFormatError) as ei:
            await _drive_structured(llm, _Req)
        assert "expected object, got int" in " ".join(ei.value.extra.get("validation_errors", []))

    @pytest.mark.asyncio
    async def test_schema_violation_raises_output_format_not_validationerror(self):
        """输出不符合 pydantic schema → 归一 LlmOutputFormatError（可重试），
        而不是裸 ValidationError 被 wrap 成不可重试的 NODE.CONTEXT。"""
        from devflow.errors import LlmOutputFormatError

        llm = _NoStructuredSupportLLM(['{"req_type": 123}'])  # req_type 必须是 str
        with pytest.raises(LlmOutputFormatError) as ei:
            await _drive_structured(llm, _Req)
        assert ei.value.extra.get("validation_errors")


class TestPromptJsonParsing:
    @pytest.mark.asyncio
    async def test_prose_wrapped_json_parsed_via_brace_scan(self):
        """回归：散文前后包裹的 JSON（弱模型常见习惯）→ 花括号扫描兜底提取成功。"""
        llm = _NoStructuredSupportLLM([
            '好的，以下是抽取结果：\n{"req_type": "bug_fix", "project_context": "计算器"}\n以上。'
        ])
        result = await _drive_structured(llm, _Req)
        assert result == {"req_type": "bug_fix", "project_context": "计算器"}
        assert llm.ainvoke_calls == 1  # 首轮即成功，无需纠错重试

    @pytest.mark.asyncio
    async def test_corrective_retry_carries_error_hint(self):
        """首轮输出不可解析 → 带上错误与原始输出尾的纠错提示重试 → 成功。"""
        llm = _NoStructuredSupportLLM(["我觉得没法回答这个问题", '{"req_type": "new_feature"}'])
        result = await _drive_structured(llm, _Req)
        assert result["req_type"] == "new_feature"
        assert llm.ainvoke_calls == 2

    @pytest.mark.asyncio
    async def test_json_array_rejected_as_object(self):
        """合法 JSON 但顶层是数组 → 期望 object 报错（可重试），不透传非 dict。"""
        from devflow.errors import LlmOutputFormatError

        llm = _NoStructuredSupportLLM(['["a", "b"]'])
        with pytest.raises(LlmOutputFormatError) as ei:
            await _drive_structured(llm, _Req)
        assert "expected object, got list" in " ".join(ei.value.extra.get("validation_errors", []))


# ── 回归：模型未吐 tool call 时 with_structured_output 返回 None ─────
class _FcNoneReturnLLM:
    """模拟 DeepSeek 偶发纯文本回答：结构化档 ainvoke 返回 None（langchain 对
    「消息里没有 tool_calls」不抛错而是返回 None），裸 ainvoke 返回 raw_content。"""

    def __init__(self, *, json_mode_none_too: bool = False,
                 raw_content: str = '{"req_type": "bug_fix", "project_context": "计算器"}') -> None:
        self._json_mode_none_too = json_mode_none_too
        self._raw_content = raw_content
        self.used_methods: list[str] = []

    def with_structured_output(self, schema: Any, *, method: str = "function_calling"):
        self.used_methods.append(method)
        none_too = self._json_mode_none_too

        class _Structured:
            async def ainvoke(self, msgs: list[BaseMessage], **_: Any) -> Any:
                if method == "function_calling" or none_too:
                    return None

                class _Out:
                    def model_dump(self, mode: str = "python") -> dict[str, Any]:
                        return {"req_type": "bug_fix", "project_context": "计算器"}

                return _Out()

        return _Structured()

    async def ainvoke(self, messages: list[BaseMessage], **_: Any) -> BaseMessage:
        return AIMessage(content=self._raw_content)


class TestNoToolCallNoneGuard:
    @pytest.mark.asyncio
    async def test_fc_none_degrades_to_json_mode(self):
        """回归（历史症状 'NoneType' object is not iterable）：模型未触发 tool call 时
        with_structured_output 返回 None，必须退化到下一档，而不是 dict(None) 炸 TypeError。"""
        llm = _FcNoneReturnLLM()
        result = await _drive_structured(llm, _Req)
        assert result == {"req_type": "bug_fix", "project_context": "计算器"}
        assert llm.used_methods == ["function_calling", "json_mode"]

    @pytest.mark.asyncio
    async def test_fc_none_all_methods_raises_output_format(self):
        """结构化档全 None 且裸补全不可解析 → 归一 LlmOutputFormatError（可重试），
        不漏 TypeError('NoneType' object is not iterable)。"""
        from devflow.errors import LlmOutputFormatError

        llm = _FcNoneReturnLLM(json_mode_none_too=True, raw_content="我无法回答这个问题")
        # 三档全试过后抛最后一档（prompt_json）的可重试格式错误，而非 TypeError
        with pytest.raises(LlmOutputFormatError):
            await _drive_structured(llm, _Req)


# ── 回归：网关回空参数 tool_call → langchain 造出「合法但全空」对象 ──────
class _HollowThenFilledLLM:
    """模拟 Qwen3.8 网关：function_calling 下只回 tool_call(arguments={})，
    langchain 用默认值造出全空对象（不报错）；json_mode 能正确抽取。"""

    def __init__(self, *, hollow_methods: tuple[str, ...] = ("function_calling",)) -> None:
        self._hollow_methods = hollow_methods
        self.used_methods: list[str] = []

    def with_structured_output(self, schema: Any, *, method: str = "function_calling"):
        self.used_methods.append(method)
        hollow = method in self._hollow_methods

        class _Structured:
            async def ainvoke(self, msgs: list[BaseMessage], **_: Any) -> Any:
                if hollow:
                    return schema()  # 全字段默认值 = 空壳（实测网关行为）

                class _Out:
                    def model_dump(self, mode: str = "python") -> dict[str, Any]:
                        return {"req_type": "new_feature", "project_context": "桌面计算器"}

                return _Out()

        return _Structured()

    async def ainvoke(self, messages: list[BaseMessage], **_: Any) -> BaseMessage:
        return AIMessage(content='{"req_type": "bug_fix", "project_context": "来自 prompt_json"}')


class _RouteMatchLike(BaseModel):
    """全可选字段 + 空结果是合法语义（对齐 RouteMatch 的 allow_hollow_result）。"""

    allow_hollow_result: ClassVar[bool] = True

    businesses: list[str] = Field(default_factory=list)


class TestHollowOutputGuard:
    @pytest.mark.asyncio
    async def test_hollow_fc_degrades_to_json_mode(self):
        """回归（历史症状：需求抽取永远为空）：function_calling 回空壳 → 判失败降级
        json_mode，而不是把空对象当成功直接返回。"""
        llm = _HollowThenFilledLLM()
        result = await _drive_structured(llm, _Req)
        assert result == {"req_type": "new_feature", "project_context": "桌面计算器"}
        assert llm.used_methods == ["function_calling", "json_mode"]

    @pytest.mark.asyncio
    async def test_hollow_all_structured_tiers_falls_through_to_prompt_json(self):
        """结构化两档都空壳 → 继续降级 prompt_json（裸补全）拿到真实内容。"""
        llm = _HollowThenFilledLLM(hollow_methods=("function_calling", "json_mode"))
        result = await _drive_structured(llm, _Req)
        assert result == {"req_type": "bug_fix", "project_context": "来自 prompt_json"}
        assert llm.used_methods == ["function_calling", "json_mode"]

    @pytest.mark.asyncio
    async def test_hollow_accepted_when_schema_declares_it(self):
        """allow_hollow_result 的 schema（空 = 无匹配）不降级：一次调用直接返回空结果。"""
        llm = _HollowThenFilledLLM()
        result = await _drive_structured(llm, _RouteMatchLike)
        assert result == {"businesses": []}
        assert llm.used_methods == ["function_calling"]


class TestIsHollow:
    """_is_hollow 语义：空/空白/空容器（含嵌套）算空；0 / False 等标量算有值。"""

    def test_deeply_empty_is_hollow(self):
        from devflow.llm_client import _is_hollow

        assert _is_hollow({})
        assert _is_hollow({"a": None, "b": "", "c": "  ", "d": [], "e": {}})
        assert _is_hollow({"io_constraints": {"input": "", "output": " "}})  # 嵌套全空
        assert _is_hollow({"items": [{"text": ""}]})

    def test_any_non_empty_value_breaks_hollow(self):
        from devflow.llm_client import _is_hollow

        assert not _is_hollow({"project_context": "计算器"})
        assert not _is_hollow({"confidence": 0.0})   # 数字 0 是有值
        assert not _is_hollow({"matched": False})    # False 是有值
        assert not _is_hollow({"edge_cases": ["除零"]})
        assert not _is_hollow(["a"])                 # 非 dict 一律不算空壳

    def test_pydantic_instance_supported(self):
        from devflow.llm_client import _is_hollow

        assert _is_hollow(_Req())
        assert not _is_hollow(_Req(req_type="bug_fix"))
