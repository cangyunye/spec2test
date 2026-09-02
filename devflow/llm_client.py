"""LLM 调用封装：统一走 ChatOpenAI（兼容所有 OpenAI 格式服务）+ SPEC 5 弹性治理。

模型配置支持两种方式：
  1) 推荐：LLM_PROVIDERS_JSON = [{name, base_url, api_key, model, temperature}, ...]
           每个 provider 独立熔断器，接口统一走 langchain_openai.ChatOpenAI
           兼容：DeepSeek / SiliconFlow / vLLM / OneAPI / Ollama OpenAI 兼容层 / ...
  2) 旧版兼容：LLM_BASE_URL + LLM_MODEL + LLM_FALLBACKS（自动转成 providers 列表）

最后可选 mock 兜底（LLM_USE_MOCK_FALLBACK=True）。
"""
from __future__ import annotations

import json
import logging
import textwrap
from typing import Any, Iterator, Type, cast

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.output_parsers import JsonOutputParser
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from .config import LlmProviderSpec, settings
from .errors import (
    LlmContextOverflowError,
    LlmOutputFormatError,
    LlmRefusedError,
    LlmTokenBudgetError,
    DevFlowError,
    RetryPolicy,
    wrap_exception,
)
from .resilience import (
    DEFAULT_TOKEN_BUDGET,
    TokenBudget,
    dead_letter_record,
    default_breaker,
    retry_with_backoff,
)

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
# 1. 模型模型池 + mock
# ═══════════════════════════════════════════════════════════════════

class _MockLLM:
    """SPEC 5.3 最后兜底：不依赖任何外部服务，始终返回模板 JSON / 文本。"""

    async def ainvoke(self, messages: list[BaseMessage], **_: Any) -> BaseMessage:
        # 依据 prompt 猜需求：要求 extract requirement 时给空模板，graph 给模板
        joined = "\n".join(str(getattr(m, "content", "")) for m in messages)
        if "requirement" in joined.lower() and "json" in joined.lower():
            payload = {
                "req_type": "component_iteration",
                "project_root": ".",
                "project_context": "(mock fallback: LLM 不可用)",
                "target_modules": [],
                "existing_code_accessible": True,
                "io_constraints": {"input": "", "output": "", "latency_ms": None, "throughput_qps": None, "env": None},
                "edge_cases": [],
                "acceptance_criteria": ["由人工补充验收标准"],
            }
            return AIMessage(content=json.dumps(payload, ensure_ascii=False))
        if any(kw in joined.lower() for kw in ("logic", "graph", "逻辑图", "制图", "mermaid")):
            return AIMessage(content=json.dumps({
                "graph_id": "mock-graph",
                "nodes": [
                    {"node_id": "n-1", "label": "Input", "node_type": "io",
                     "code_ref": None, "is_modified": False},
                    {"node_id": "n-2", "label": "Process", "node_type": "module",
                     "code_ref": None, "is_modified": True},
                ],
                "edges": [
                    {"edge_id": "e-1", "from_node": "n-1", "to_node": "n-2",
                     "edge_type": "call", "condition": None, "is_modified": False},
                ],
                "mermaid_source": "graph TD\\n  n-1(Input) --> n-2(Process)",
            }, ensure_ascii=False))
        # clarify_build_question / 其它：提示人工介入
        return AIMessage(content="(mock fallback) 当前 LLM 不可用，请人工补充需求。")

    def with_structured_output(self, _schema: Any, **_: Any) -> "_MockStructured":
        return _MockStructured()


class _MockStructured:
    async def ainvoke(self, messages: list[BaseMessage], **_: Any) -> Any:
        joined = "\n".join(str(getattr(m, "content", "")) for m in messages)
        joined_lower = joined.lower()

        class _ReqMocker:
            def model_dump(self, mode: str = "python") -> dict[str, Any]:
                return {
                    "req_type": "component_iteration",
                    "project_root": ".",
                    "project_context": "mock",
                    "target_modules": [],
                    "existing_code_accessible": True,
                    "io_constraints": {"input": "", "output": ""},
                    "edge_cases": [],
                    "acceptance_criteria": [],
                }

        class _GraphMocker:
            def model_dump(self, mode: str = "python") -> dict[str, Any]:
                return {
                    "nodes": [
                        {"node_id": "n-1", "label": "Input", "node_type": "io",
                         "code_ref": None, "is_modified": False},
                        {"node_id": "n-2", "label": "Process", "node_type": "module",
                         "code_ref": None, "is_modified": True},
                    ],
                    "edges": [
                        {"edge_id": "e-1", "from_node": "n-1", "to_node": "n-2",
                         "edge_type": "call", "condition": None, "is_modified": False},
                    ],
                    "mermaid_source": "graph TD\n  n-1(Input) --> n-2(Process)",
                }

        # 中英文关键词都匹配
        if any(kw in joined_lower for kw in ("logic", "graph", "逻辑图", "制图", "mermaid")):
            return _GraphMocker()
        return _ReqMocker()


_model_cache: dict[str, Any] = {}


def _candidates_providers() -> list[LlmProviderSpec]:
    """fallback 链：按 settings.LLM_PROVIDERS 顺序返回结构化 provider specs。"""
    return list(settings.LLM_PROVIDERS)


def _use_mock_fallback() -> bool:
    return bool(settings.LLM_USE_MOCK_FALLBACK)


def _breaker_name_for(spec: LlmProviderSpec) -> str:
    return f"llm_{spec['name']}"


def _get_model(spec: str | LlmProviderSpec):
    """懒加载单例。

    两种调用方式：
      - spec == "mock" / 传字符串 'mock'：内置 Mock
      - 传 LlmProviderSpec dict：统一 ChatOpenAI 构造，支持所有 OpenAI 兼容接口
    """
    # 兼容老调用：spec = 字符串（mock）
    if isinstance(spec, str):
        if spec == "mock":
            _model_cache.setdefault("mock", _MockLLM())
            return _model_cache["mock"]
        raise ValueError(
            f"_get_model(spec) 字符串模式已废弃，仅支持 'mock'；"
            f"多提供商请传 LlmProviderSpec dict（got: {spec!r}）"
        )

    # 结构化 LlmProviderSpec
    key = f"provider::{spec['name']}::{spec['base_url']}::{spec['model']}"
    if key not in _model_cache:
        kwargs: dict[str, Any] = {}
        # ponytail: DeepSeek V4 默认开 thinking 且拒绝 tool_choice（HTTP 400），
        # 导致 function_calling 结构化输出必败。本工具链以结构化 JSON 为主，
        # 直接关掉 thinking；如需推理能力改用非结构化 invoke_text 或换模型。
        if spec["model"].startswith("deepseek-v4"):
            kwargs["model_kwargs"] = {"extra_body": {"thinking": {"type": "disabled"}}}
        _model_cache[key] = ChatOpenAI(
            model=spec["model"],
            base_url=spec["base_url"],
            api_key=spec["api_key"],
            temperature=spec.get("temperature", 0.1),
            # 常见超时
            timeout=60.0,
            max_retries=0,  # 不在 SDK 层重试，我们自己的 retry_with_backoff 负责
            **kwargs,
        )
    return _model_cache[key]


def _make_structured(llm: Any, response_model: Type[BaseModel], method: str | None = None):
    """结构化输出兼容层（D1）。

    不同 OpenAI 兼容供应商对 with_structured_output 的支持不一致：
      - OpenAI / DeepSeek（deepseek-chat）     : 支持 function_calling 与 json_mode
      - 部分自建 vLLM / OneAPI 网关           : 不支持 tools，需退化 json_mode

    method=None（auto）时：先试 function_calling，创建或调用阶段抛错 → 自动退化 json_mode；
    全部失败则抛最后一个错误（不吞异常）。

    D1b：模型输出不严格符合 json_schema 时（如 code_ref 输出成字符串），langchain
    会抛 OutputParserException。此处统一转成 LlmOutputFormatError（retryable=True），
    让上层 retry_with_backoff 有机会重试，而不是被 wrap 成 NODE.CONTEXT（不可重试）直接放弃。
    """
    from langchain_core.exceptions import OutputParserException

    methods = ["function_calling", "json_mode"] if method is None else [method]

    class _StructuredWithFallback:
        async def ainvoke(self, msgs: list[BaseMessage], **_: Any) -> Any:
            last_err: Exception | None = None
            for m in methods:
                try:
                    structured = llm.with_structured_output(response_model, method=m)
                    return await structured.ainvoke(msgs)
                except OutputParserException as e:
                    # 模型输出不合 schema → 归类 LLM.OUTPUT_FORMAT（可重试）
                    last_err = LlmOutputFormatError(
                        f"结构化输出解析失败（method={m}）: {e}", cause=e
                    )
                except Exception as e:  # noqa: BLE001 - 兼容层需兜底全部创建/调用异常
                    last_err = e
                    # 还有下一个 method 可试 → 继续退化；否则抛出最后一个错误
                    if m is methods[-1]:
                        break
            assert last_err is not None
            raise last_err

    return _StructuredWithFallback()


def _token_budget() -> TokenBudget:
    if DEFAULT_TOKEN_BUDGET.daily_tokens != settings.LLM_TOKEN_BUDGET_DAILY:
        DEFAULT_TOKEN_BUDGET.daily_tokens = settings.LLM_TOKEN_BUDGET_DAILY
    return DEFAULT_TOKEN_BUDGET


# ═══════════════════════════════════════════════════════════════════
# 2. 对外：invoke_json / invoke_text（async）
# ═══════════════════════════════════════════════════════════════════

def _estimate_tokens(chars: int) -> int:
    # 粗略估算：GPT 中文/英文混合 1 token ≈ 3.5 chars
    return max(1, int(chars / 3.5))


def _messages_too_big(messages: list[BaseMessage]) -> bool:
    chars = sum(len(str(getattr(m, "content", ""))) for m in messages)
    limit = int(settings.LLM_CONTEXT_WINDOW_TOKENS * 3.5 * 0.9)
    return chars > limit


def _shrink_messages(messages: list[BaseMessage]) -> list[BaseMessage]:
    """SPEC 5.1 / 5.6 降级压缩：系统 prompt 不动，最早的 50% history 砍掉；
    如果仍超限，把每条 HumanMessage 取前 80%。
    """
    if len(messages) <= 2:
        return messages
    system = messages[0]
    mid = messages[1:-1]  # 去掉 system 和最后一条 user
    # 砍最早一半
    keep = mid[max(0, len(mid) // 2):]
    rebuilt: list[BaseMessage] = [system, *keep, messages[-1]]
    if not _messages_too_big(rebuilt):
        return rebuilt
    # 还超限：对每条非 system 消息内容截断
    out2: list[BaseMessage] = [rebuilt[0]]
    for m in rebuilt[1:]:
        c = str(getattr(m, "content", ""))
        truncated = textwrap.shorten(c, width=max(200, int(len(c) * 0.6)), placeholder=" ...<TRUNCATED>")
        if isinstance(m, HumanMessage):
            out2.append(HumanMessage(content=truncated))
        elif isinstance(m, AIMessage):
            out2.append(AIMessage(content=truncated))
        else:
            out2.append(m)
    return out2


async def invoke_json(
    system_prompt: str,
    user_prompt: str,
    response_model: Type[BaseModel] | None = None,
    json_schema: dict[str, Any] | None = None,
    *,
    chat_history: list[BaseMessage] | None = None,
    max_retries: int = 2,
    shrink_attempts: int = 3,
    response_type: str = "general",
) -> dict[str, Any]:
    """异步版 JSON 结构化调用，套 SPEC 5 的完整治理。

    参数:
        response_type: 传给 dead_letter 的标识，区分 requirement_extract / logic_graph / build_question
    """
    budget = _token_budget()
    if budget.is_hard_limited():
        raise LlmTokenBudgetError(
            f"今日 LLM token 用量已达上限（used={budget.consumed_today()}，limit={budget.daily_tokens}）"
        )

    # 组装消息
    messages: list[BaseMessage] = [SystemMessage(content=system_prompt)]
    if chat_history:
        messages.extend(m for m in chat_history if not isinstance(m, SystemMessage))
    messages.append(HumanMessage(content=user_prompt))

    providers = _candidates_providers()
    last_err: DevFlowError | None = None
    shrink_left = shrink_attempts

    # 模型切换循环（结构化 provider spec，每个 provider 独立熔断器）
    for mi, p_spec in enumerate(providers):
        try:
            llm = _get_model(p_spec)
        except Exception as e:
            logger.warning("模型 %s 加载失败，跳过: %s", p_spec.get("name"), e)
            last_err = wrap_exception(e, context=f"llm_load[{p_spec.get('name')}]")
            continue
        breaker = default_breaker(_breaker_name_for(p_spec))
        try:
            return await _invoke_json_once(
                llm=llm,
                messages=messages,
                response_model=response_model,
                json_schema=json_schema,
                max_retries=max_retries,
                breaker=breaker,
                budget=budget,
                response_type=response_type,
                model_spec=p_spec["name"],
            )
        except LlmContextOverflowError as e:
            if shrink_left <= 0:
                last_err = e
                continue
            messages = _shrink_messages(messages)
            shrink_left -= 1
            last_err = e
            try:
                return await _invoke_json_once(
                    llm=llm,
                    messages=messages,
                    response_model=response_model,
                    json_schema=json_schema,
                    max_retries=max_retries,
                    breaker=breaker,
                    budget=budget,
                    response_type=response_type,
                    model_spec=p_spec["name"],
                )
            except DevFlowError as e2:
                last_err = e2
                continue
        except LlmTokenBudgetError as e:
            last_err = e
            break
        except LlmRefusedError as e:
            last_err = e
            continue
        except DevFlowError as e:
            last_err = e
            continue

    # 走到这里，所有 provider 都失败
    assert last_err is not None
    # 保底：LLM_USE_MOCK_FALLBACK → 强制 MockLLM 兜底
    if _use_mock_fallback():
        logger.warning("所有真实 LLM 失败，强制走 Mock 兜底。response_type=%s", response_type)
        mock = _get_model("mock")
        return await _invoke_json_once(
            llm=mock, messages=messages,
            response_model=response_model, json_schema=json_schema,
            max_retries=1, breaker=breaker, budget=budget,
            response_type=response_type, model_spec="mock",
        )
    dead_letter_record(last_err, state_snapshot={"response_type": response_type})
    raise last_err


async def invoke_text(
    system_prompt: str,
    user_prompt: str,
    *,
    chat_history: list[BaseMessage] | None = None,
    max_retries: int = 2,
) -> str:
    """普通文本调用（压缩节点摘要用），async，走同一套治理。"""
    budget = _token_budget()
    if budget.is_hard_limited():
        raise LlmTokenBudgetError(
            f"今日 LLM token 用量已达上限（used={budget.consumed_today()}，limit={budget.daily_tokens}）"
        )
    messages: list[BaseMessage] = [SystemMessage(content=system_prompt)]
    if chat_history:
        messages.extend(m for m in chat_history if not isinstance(m, SystemMessage))
    messages.append(HumanMessage(content=user_prompt))

    last_err: DevFlowError | None = None
    shrink_left = 3
    for p_spec in _candidates_providers():
        try:
            llm = _get_model(p_spec)
        except Exception as e:
            logger.warning("模型 %s 加载失败，跳过: %s", p_spec.get("name"), e)
            last_err = wrap_exception(e, context=f"llm_load[{p_spec.get('name')}]")
            continue
        breaker = default_breaker(_breaker_name_for(p_spec))

        async def _run(m: list[BaseMessage]) -> str:
            async with breaker.guard():
                resp = await llm.ainvoke(m)
            content = resp.content if isinstance(resp, BaseMessage) else str(resp)
            chars = sum(len(str(getattr(msg, "content", ""))) for msg in m) + len(str(content))
            budget.consume(_estimate_tokens(chars))
            return str(content)

        policy = RetryPolicy(max_attempts=max_retries + 1, base_backoff=1.0, deadline_total=120.0)
        dec = retry_with_backoff(policy, wrap_context=f"llm.invoke_text[{p_spec['name']}]")
        do_run = dec(_run)

        try:
            return await do_run(messages)
        except LlmContextOverflowError:
            if shrink_left <= 0:
                continue
            messages = _shrink_messages(messages)
            shrink_left -= 1
            try:
                return await do_run(messages)
            except DevFlowError as e:
                last_err = e
                continue
        except LlmRefusedError as e:
            last_err = e
            continue
        except DevFlowError as e:
            last_err = e
            continue

    # 全部失败 → mock（可配置）
    if _use_mock_fallback():
        logger.warning("llm.invoke_text 全部模型失败，走 Mock。")
        return cast(str, (await _get_model("mock").ainvoke(messages)).content)
    assert last_err is not None
    raise last_err


# ═══════════════════════════════════════════════════════════════════
# 3. 单次模型调用内部实现（封装重试 + 熔断 + token 扣减 + format 校验重试）
# ═══════════════════════════════════════════════════════════════════

async def _invoke_json_once(
    *,
    llm: Any,
    messages: list[BaseMessage],
    response_model: Type[BaseModel] | None,
    json_schema: dict[str, Any] | None,
    max_retries: int,
    breaker: Any,
    budget: TokenBudget,
    response_type: str,
    model_spec: str,
) -> dict[str, Any]:
    # ── 分支 A：Pydantic 结构化（优先） ───────────────
    if response_model is not None:
        # D1：function_calling → json_mode 自动退化（兼容不支持 tools 的自建网关）
        structured = _make_structured(llm, response_model, method=None)

        async def run_structured(msgs: list[BaseMessage]) -> dict[str, Any]:
            async with breaker.guard():
                obj = await structured.ainvoke(msgs)
            # 结构化通过；obj 要么有 model_dump，要么我们兜底
            if hasattr(obj, "model_dump"):
                return obj.model_dump(mode="json")
            return dict(obj)

        pol = RetryPolicy(max_attempts=max_retries + 1, base_backoff=1.0, deadline_total=180.0)
        dec = retry_with_backoff(pol, wrap_context=f"llm.structured:{model_spec}")
        do_run = dec(run_structured)
        try:
            return await do_run(messages)
        except DevFlowError as e:
            # 结构化失败 → SPEC 5.1 里算 LLM.OUTPUT_FORMAT，这里不用转换，直接抛（上层 fallback）
            _account(messages, {}, budget)
            raise e

    # ── 分支 B：json_schema / 裸 parser ────────────────
    schema_hint = ""
    if json_schema is not None:
        schema_hint = (
            "\n\n# 输出约束\n"
            "请严格按照下方 JSON Schema 输出，不要加 Markdown 代码块，不要加额外文字：\n"
            f"```json\n{json.dumps(json_schema, ensure_ascii=False, indent=2)}\n```"
        )
    else:
        schema_hint = "\n\n请只输出合法 JSON，不要加额外文字。"

    # 把约束追加到最后一条 HumanMessage
    last_human_idx = max(
        (i for i, m in enumerate(messages) if isinstance(m, HumanMessage)), default=0
    )
    msgs2 = list(messages)
    extra_hint = schema_hint
    for attempt in range(max_retries + 1):
        try:
            async with breaker.guard():
                resp = await llm.ainvoke(msgs2)
            content = resp.content if isinstance(resp, BaseMessage) else str(resp)
            try:
                payload = JsonOutputParser().parse(content)
            except Exception as e:
                raise LlmOutputFormatError(
                    f"JsonOutputParser 解析失败: {e}",
                    validation_errors=[str(e)],
                    extra={"raw_tail": str(content)[-200:]},
                ) from e
            _account(msgs2, payload, budget)
            return payload
        except LlmOutputFormatError as e:
            if attempt == max_retries:
                _account(msgs2, {}, budget)
                raise e
            # 构造带错误提示的下一次请求：把原始 schema + 错误说明拼进 user message
            msgs2 = list(messages)
            prev_content = str(msgs2[last_human_idx].content) if last_human_idx < len(msgs2) else ""
            msgs2[last_human_idx] = HumanMessage(
                content=prev_content + extra_hint
                + "\n\n# 上一次错误输出（请修正）\n"
                + f"错误: {e.message}\n"
                + f"你输出的末尾: {(e.extra or {}).get('raw_tail', '')}\n"
                + "请严格按 JSON Schema 重新输出合法 JSON，不要加额外说明。"
            )
            continue
        except DevFlowError as e:
            _account(msgs2, {}, budget)
            raise e
    # 理论不会走到
    raise RuntimeError("unreachable")


def _account(messages: list[BaseMessage], output: Any, budget: TokenBudget) -> None:
    chars = sum(len(str(getattr(m, "content", ""))) for m in messages)
    try:
        chars += len(json.dumps(output, ensure_ascii=False))
    except Exception:
        chars += len(str(output))
    budget.consume(_estimate_tokens(chars))


def reset_model_cache() -> None:
    """测试隔离：清空 _model_cache 里所有懒加载单例（避免跨测试模型配置交叉污染）。"""
    _model_cache.clear()


# ═══════════════════════════════════════════════════════════════════
# D2: check-llm 连通性自检（最小 token 消耗）
# ═══════════════════════════════════════════════════════════════════

async def check_llm_provider(
    spec: LlmProviderSpec,
    *,
    timeout_sec: float = 20.0,
) -> dict[str, Any]:
    """对单个 provider 做一次最小调用，返回诊断报告（不抛异常）。

    返回字段：
      name / model / base_url / ok / elapsed_ms / reply(截断) /
      error_code / error_message
    """
    import asyncio
    import time

    from langchain_core.messages import HumanMessage

    name = spec["name"]
    start = time.monotonic()
    try:
        llm = _get_model(spec)
        resp = await asyncio.wait_for(
            llm.ainvoke([HumanMessage(content="ping: 请只回复 ok")]),
            timeout=timeout_sec,
        )
        elapsed = time.monotonic() - start
        text = str(resp.content) if hasattr(resp, "content") else str(resp)
        return {
            "name": name,
            "model": spec["model"],
            "base_url": spec["base_url"],
            "ok": True,
            "elapsed_ms": int(elapsed * 1000),
            "reply": text[:50],
            "error_code": None,
            "error_message": None,
        }
    except Exception as e:  # noqa: BLE001 - 自检必须吞掉全部错误转成报告
        elapsed = time.monotonic() - start
        err = wrap_exception(e, context=f"check-llm[{name}]")
        return {
            "name": name,
            "model": spec["model"],
            "base_url": spec["base_url"],
            "ok": False,
            "elapsed_ms": int(elapsed * 1000),
            "reply": None,
            "error_code": err.code,
            "error_message": err.message,
        }


async def check_llm_all() -> list[dict[str, Any]]:
    """遍历 settings.LLM_PROVIDERS，逐个自检，返回报告列表。"""
    reports: list[dict[str, Any]] = []
    for spec in _candidates_providers():
        reports.append(await check_llm_provider(spec))
    return reports
