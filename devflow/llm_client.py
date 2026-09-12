"""LLM 调用封装：统一走 ChatOpenAI（兼容所有 OpenAI 格式服务）+ SPEC 5 弹性治理。

模型配置支持两种方式：
  1) 推荐：LLM_PROVIDERS_JSON = [{name, base_url, api_key, model, models?, temperature}, ...]
           每个 provider 独立熔断器，接口统一走 langchain_openai.ChatOpenAI
           兼容：DeepSeek / OpenCode Go / SiliconFlow / vLLM / OneAPI / Ollama OpenAI 兼容层 / ...
           可选 models 模型池在 config 解析时展开为同供应商多模型 fallback 链，
           LLM_ACTIVE_MODEL 环境变量可切换激活模型（不改 JSON）
  2) 旧版兼容：LLM_BASE_URL + LLM_MODEL + LLM_FALLBACKS（自动转成 providers 列表）

最后可选 mock 兜底（LLM_USE_MOCK_FALLBACK=True）。
"""
from __future__ import annotations

import json
import logging
import re
import textwrap
from typing import Any, Iterator, Type, cast

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.output_parsers import JsonOutputParser
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from .config import LlmProviderSpec, resolve_context_window, settings
from .errors import (
    HTTP_AUTH,
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

# Mock 兜底用的完整演示需求：保证「无 Key 演示模式」下澄清校验可通过、
# 全链路能跑通到门禁与测试场景（残缺模板会在 clarify_validate 处死循环）。
_MOCK_DEMO_REQUIREMENT: dict[str, Any] = {
    "req_type": "component_iteration",
    "project_root": ".",
    "project_context": "Mock 演示项目：桌面 GUI 计算器应用（tkinter），含加减乘除与错误提示",
    "target_modules": ["calculator/calc.py"],
    "existing_code_accessible": True,
    "io_constraints": {"input": "按钮点击与表达式输入", "output": "运算结果或错误提示"},
    "edge_cases": ["除零", "连续运算", "负数", "小数"],
    "acceptance_criteria": ["四则运算结果正确", "除零给出错误提示", "GUI 可启动"],
    # 演示需求整体是编造的：如实标注为「AI 推断」，让制图前的需求确认门禁照常触发
    "inferred_fields": [
        "project_context",
        "target_modules",
        "io_constraints.input",
        "io_constraints.output",
        "edge_cases",
        "acceptance_criteria",
    ],
}

# Mock 兜底用的测试设计模板（response_type=test_design）：按系统化设计策略
# 产出 正向/边界值/反向 三类演示用例，保证无 Key 演示模式下用例表有完整结构。
_MOCK_DEMO_TEST_DESIGN: dict[str, Any] = {
    "overview": "Mock 演示：围绕需求要点设计端到端测试场景（功能优先，正向/反向/边界结合）。",
    "scenarios": [
        {
            "tier": "functional", "priority": "P0",
            "title": "用户按主流程完成一次核心操作，结果符合验收标准",
            "case_type": "正向", "target": "核心流程",
            "precondition": "应用已启动，环境就绪",
            "steps": "1. 打开应用\n2. 按主流程执行一次核心操作",
            "expected": "输出与验收标准一致，界面/状态正确更新",
            "rationale": "核心 happy path",
        },
        {
            "tier": "functional", "priority": "P1",
            "title": "边界输入（0 / 空 / 超长）被正确处理",
            "case_type": "边界值", "target": "输入边界",
            "steps": "分别输入 0、空值、超长文本并提交",
            "expected": "给出明确提示，不崩溃、不产生脏数据",
            "rationale": "边界值策略",
        },
        {
            "tier": "security", "priority": "P0",
            "title": "非法/越权输入被拒绝且无副作用",
            "case_type": "反向", "target": "异常与权限",
            "steps": "输入非法字符或执行越权操作",
            "expected": "操作被拒绝并提示，状态不变",
            "rationale": "反向校验",
        },
    ],
    "self_check": [
        "核心功能均有正向用例",
        "边界值与反向场景已覆盖",
        "用例类型与优先级已标注",
    ],
    "summary": "Mock 演示：3 条场景（正向/边界值/反向）",
}


# Mock 兜底用的各图种类模板（response_type=logic_graph）：保证无 Key 演示模式下
# sequence / state / er / journey 四种制图也有合法结构化产物（flowchart 沿用下方内联模板）。
_MOCK_DEMO_SEQUENCE: dict[str, Any] = {
    "participants": [
        {"alias": "USER", "label": "操作用户", "kind": "actor", "is_modified": False},
        {"alias": "APP", "label": "Mock 演示服务", "kind": "service", "is_modified": True},
        {"alias": "EXT", "label": "外部依赖", "kind": "external", "is_modified": False},
    ],
    "messages": [
        {"msg_id": "m1", "from_participant": "USER", "to_participant": "APP",
         "label": "发起请求", "kind": "sync", "is_modified": False},
        {"msg_id": "m2", "from_participant": "APP", "to_participant": "EXT",
         "label": "调用外部能力", "kind": "sync", "is_modified": True},
        {"msg_id": "m3", "from_participant": "EXT", "to_participant": "APP",
         "label": "返回结果", "kind": "return", "is_modified": False},
        {"msg_id": "m4", "from_participant": "APP", "to_participant": "USER",
         "label": "给出响应", "kind": "return", "is_modified": False},
    ],
    "mermaid_source": (
        "sequenceDiagram\n"
        "  actor USER as 操作用户\n"
        "  participant APP as Mock 演示服务\n"
        "  participant EXT as 外部依赖\n"
        "  USER->>APP: 发起请求\n"
        "  APP->>EXT: 调用外部能力\n"
        "  EXT-->>APP: 返回结果\n"
        "  APP-->>USER: 给出响应"
    ),
}

_MOCK_DEMO_STATE: dict[str, Any] = {
    "states": [
        {"state_id": "s_start", "label": "初始", "kind": "initial", "is_modified": False},
        {"state_id": "s_proc", "label": "处理中", "kind": "normal", "is_modified": True},
        {"state_id": "s_done", "label": "完成", "kind": "final", "is_modified": False},
    ],
    "transitions": [
        {"trans_id": "t1", "from_state": "s_start", "to_state": "s_proc",
         "event": "提交", "is_modified": False},
        {"trans_id": "t2", "from_state": "s_proc", "to_state": "s_done",
         "event": "校验通过", "is_modified": True},
    ],
    "mermaid_source": (
        "stateDiagram-v2\n"
        "  [*] --> s_start\n"
        "  s_start --> s_proc: 提交\n"
        "  s_proc : 处理中\n"
        "  s_proc --> s_done: 校验通过\n"
        "  s_done --> [*]"
    ),
}

_MOCK_DEMO_ER: dict[str, Any] = {
    "entities": [
        {"e_id": "n-user", "table": "USER", "is_modified": False, "attributes": [
            {"name": "user_id", "type": "int", "is_pk": True},
            {"name": "name", "type": "string", "is_pk": False},
        ]},
        {"e_id": "n-order", "table": "ORDER", "is_modified": True, "attributes": [
            {"name": "order_id", "type": "int", "is_pk": True},
        ]},
    ],
    "relations": [
        {"rel_id": "r1", "from_entity": "n-user", "to_entity": "n-order",
         "cardinality": "one_to_many", "label": "places", "is_modified": True},
    ],
    "mermaid_source": (
        "erDiagram\n"
        "  USER ||--o{ ORDER : places\n"
        "  USER {\n"
        "    int user_id PK\n"
        "    string name\n"
        "  }\n"
        "  ORDER {\n"
        "    int order_id PK\n"
        "  }"
    ),
}

_MOCK_DEMO_JOURNEY: dict[str, Any] = {
    "sections": [
        {"section_id": "sec1", "label": "开始使用", "is_modified": False},
        {"section_id": "sec2", "label": "核心操作", "is_modified": True},
    ],
    "tasks": [
        {"task_id": "j1", "label": "打开应用", "score": 7, "actors": ["用户"],
         "section_id": "sec1", "is_modified": False},
        {"task_id": "j2", "label": "执行核心操作", "score": 5,
         "actors": ["用户", "Mock 演示服务"], "section_id": "sec2", "is_modified": True},
        {"task_id": "j3", "label": "获得结果", "score": 8, "actors": ["用户"],
         "section_id": "sec2", "is_modified": False},
    ],
    "mermaid_source": (
        "journey\n"
        "  title Mock 演示用户旅程\n"
        "  section 开始使用\n"
        "    打开应用: 7: 用户\n"
        "  section 核心操作\n"
        "    执行核心操作: 5: 用户, Mock 演示服务\n"
        "    获得结果: 8: 用户"
    ),
}

# 制图提示词里的种类特征串 → mock 模板（检测顺序：先种类后通用，避免被通用分支截胡）
_MOCK_GRAPH_BY_MARKER: list[tuple[str, dict[str, Any]]] = [
    ("sequenceDiagram", _MOCK_DEMO_SEQUENCE),
    ("stateDiagram-v2", _MOCK_DEMO_STATE),
    ("erDiagram", _MOCK_DEMO_ER),
    # journey 的声明头本身就是小写单词，直接用首行声明做特征串
    ("journey", _MOCK_DEMO_JOURNEY),
]


def _mock_graph_payload(prompt_text: str) -> dict[str, Any] | None:
    """按提示词特征返回对应图种类的 mock 结构化数据；非制图请求返回 None。"""
    for marker, payload in _MOCK_GRAPH_BY_MARKER:
        if marker in prompt_text:
            return json.loads(json.dumps(payload, ensure_ascii=False))
    return None


class _MockLLM:
    """SPEC 5.3 最后兜底：不依赖任何外部服务，始终返回模板 JSON / 文本。"""

    async def ainvoke(self, messages: list[BaseMessage], **_: Any) -> BaseMessage:
        # 依据 prompt 猜需求：要求 extract requirement 时给空模板，graph 给模板
        joined = "\n".join(str(getattr(m, "content", "")) for m in messages)
        # 需求抽取按身份串精确识别（提示词里出现「制图」等词时不至于被制图分支抢答）
        if "严谨的需求分析师" in joined:
            return AIMessage(content=json.dumps(_MOCK_DEMO_REQUIREMENT, ensure_ascii=False))
        graph_payload = _mock_graph_payload(joined)
        if graph_payload is not None:
            return AIMessage(content=json.dumps(
                {"graph_id": "mock-graph", **graph_payload}, ensure_ascii=False))
        if "测试架构师" in joined:
            return AIMessage(content=json.dumps(_MOCK_DEMO_TEST_DESIGN, ensure_ascii=False))
        if "会话名称" in joined:
            return AIMessage(content="桌面计算器科学计算")
        if "requirement" in joined.lower() and "json" in joined.lower():
            return AIMessage(content=json.dumps(_MOCK_DEMO_REQUIREMENT, ensure_ascii=False))
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
                "mermaid_source": "flowchart TD\n  n-1([Input]) --> n-2[Process]",
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
                return dict(_MOCK_DEMO_REQUIREMENT)

        class _GraphMocker:
            def __init__(self, payload: dict[str, Any]) -> None:
                self._payload = payload

            def model_dump(self, mode: str = "python") -> dict[str, Any]:
                return json.loads(json.dumps(self._payload, ensure_ascii=False))

        class _TestDesignMocker:
            def model_dump(self, mode: str = "python") -> dict[str, Any]:
                return json.loads(json.dumps(_MOCK_DEMO_TEST_DESIGN, ensure_ascii=False))

        # 先按「任务提示词的固定身份串」精确分派：需求抽取是默认档，但它和制图共享
        # 环境词（如提示词里出现「制图」二字），按关键词猜会被抢答，必须显式识别。
        if "严谨的需求分析师" in joined:
            return _ReqMocker()
        # 中英文关键词都匹配（test_design 提示词含"测试架构师"，须先于制图分支判断）
        if "测试架构师" in joined:
            return _TestDesignMocker()
        # 按图种类分派（种类特征串检查须先于通用制图关键词）
        typed = _mock_graph_payload(joined)
        if typed is not None:
            return _GraphMocker(typed)
        if any(kw in joined_lower for kw in ("logic", "graph", "逻辑图", "制图", "mermaid")):
            return _GraphMocker({
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
            })
        return _ReqMocker()


_model_cache: dict[str, Any] = {}

# 供应商运行期状态（进程内，重启清零）：
#   _disabled_providers —— 服务端已确认无效（鉴权 401/403）的 provider，调用时直接跳过
#   _sticky_provider    —— 最近一次成功的 provider，下一轮排最前以命中其上下文缓存，
#                          避免链上多个供应商来回跳
_disabled_providers: set[str] = set()
_sticky_provider: str | None = None


def _is_auth_error(err: DevFlowError) -> bool:
    """鉴权类错误（401/403）：确定性失效，重试与熔断都无意义，应停用。"""
    return err.code == HTTP_AUTH


def disable_provider(name: str, reason: str = "") -> bool:
    """停用 provider（鉴权确认无效）；已停用返回 False。检测通过可 enable 解除。"""
    global _sticky_provider
    if name in _disabled_providers:
        return False
    _disabled_providers.add(name)
    if _sticky_provider == name:
        _sticky_provider = None
    _emit_stream_event({"type": "provider_disabled", "provider": name, "reason": reason[:160]})
    return True


def enable_provider(name: str) -> bool:
    """解除停用（面板检测通过时调用）；原本未停用返回 False。"""
    if name not in _disabled_providers:
        return False
    _disabled_providers.discard(name)
    return True


def _candidates_providers() -> list[LlmProviderSpec]:
    """本次调用的 fallback 链：过滤已停用 → 粘性成功者提前（命中其上下文缓存）。"""
    chain = [p for p in settings.LLM_PROVIDERS if p.get("name") not in _disabled_providers]
    if _sticky_provider:
        for i, p in enumerate(chain):
            if p.get("name") == _sticky_provider and i > 0:
                chain.insert(0, chain.pop(i))
                break
    return chain


def _use_mock_fallback() -> bool:
    return bool(settings.LLM_USE_MOCK_FALLBACK)


def _emit_stream_event(payload: dict[str, Any]) -> None:
    """在 LangGraph 运行时内发自定义事件（Web 流消费，provider 切换/使用可见化）。

    CLI / 单测等无 runtime 上下文时 get_stream_writer 会抛 RuntimeError，静默跳过。
    """
    try:
        from langgraph.config import get_stream_writer

        get_stream_writer()(payload)
    except Exception:
        pass


def _mark_provider_success(p_spec: LlmProviderSpec) -> None:
    """记录粘性成功供应商（下一轮排最前，命中其上下文缓存）并广播使用事件。"""
    global _sticky_provider
    _sticky_provider = p_spec["name"]
    _emit_stream_event({
        "type": "provider_used",
        "provider": p_spec["name"],
        "model": p_spec["model"],
    })


def _log_provider_skip(p_name: Any, err: DevFlowError, *, ctx: str = "") -> None:
    """provider 调用失败的可见化日志：走到 Mock 兜底时能从日志直接看出每个 provider 的原因。

    鉴权类失败（401/403）是确定性失效：重试与熔断都无意义，直接停用该 provider
    （面板检测通过后自动解除），不再让每一轮调用都白白撞一次。
    """
    logger.warning(
        "[llm] provider「%s」%s失败（%s）: %s —— 切换下一个 provider",
        p_name, f"{ctx} " if ctx else "", err.code, err.message[:300],
    )
    _emit_stream_event({
        "type": "provider_skip",
        "provider": str(p_name),
        "code": err.code,
        "message": err.message[:160],
        "ctx": ctx,
    })
    if _is_auth_error(err):
        disable_provider(str(p_name), err.message)


def _log_mock_fallback(response_type: str, last_err: DevFlowError | None) -> None:
    """Mock 兜底日志：附带最后一个真实 provider 的错误码与摘要，方便定位为什么走到 mock。"""
    if last_err is not None:
        logger.warning(
            "[llm] 所有真实 provider 均失败，走 Mock 兜底（response_type=%s）。"
            "最后错误 [%s] %s",
            response_type, last_err.code, last_err.message[:300],
        )
    else:
        logger.warning("[llm] 未配置任何真实 provider，直接走 Mock 兜底（response_type=%s）", response_type)


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
    # SDK 层 request_timeout 与单次限时同源（0 = 不限时）；纳入缓存 key 防配置变更后拿到脏模型
    sdk_timeout = settings.LLM_TIMEOUT_PER_ATTEMPT_SEC or None
    key = f"provider::{spec['name']}::{spec['base_url']}::{spec['model']}::{sdk_timeout}"
    if key not in _model_cache:
        kwargs: dict[str, Any] = {}
        # ponytail: DeepSeek V4 默认开 thinking 且拒绝 tool_choice（HTTP 400），
        # 导致 function_calling 结构化输出必败。本工具链以结构化 JSON 为主，
        # 直接关掉 thinking；如需推理能力改用非结构化 invoke_text 或换模型。
        if spec["model"].startswith("deepseek-v4"):
            # extra_body 是 langchain-openai 的一等参数，塞 model_kwargs 会触发弃用告警
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        # 网关自定义请求头（如 OpenCode Go 要求 x-opencode-session 会话头）
        if spec.get("headers"):
            kwargs["default_headers"] = dict(spec["headers"])
        _model_cache[key] = ChatOpenAI(
            model=spec["model"],
            base_url=spec["base_url"],
            api_key=spec["api_key"],
            temperature=spec.get("temperature", 0.1),
            # SDK 层超时：langchain-openai 的 None = 不限（httpx 不掐长生成）；
            # 限时策略由我们自己的 retry_with_backoff 负责（LLM_TIMEOUT_PER_ATTEMPT_SEC）
            timeout=sdk_timeout,
            max_retries=0,  # 不在 SDK 层重试，我们自己的 retry_with_backoff 负责
            **kwargs,
        )
    return _model_cache[key]


def _extract_json_object(text: str) -> dict[str, Any]:
    """从模型输出提取 JSON object（prompt_json 档专用）。

    JsonOutputParser 已能处理裸 JSON / ```json 围栏 / 尾部散文；这里补它不认的
    散文前后包裹（「好的，结果如下：{...} 以上。」）——扫描首个 '{' 做括号配对
    （跳过字符串字面量内的括号）。裸标量（数字开头的纯文本等）归一为
    LlmOutputFormatError，绝不把非 dict 透传给调用方。
    """
    try:
        payload = JsonOutputParser().parse(text)
    except Exception:
        payload = None
    if isinstance(payload, dict):
        return payload

    start = text.find("{")
    if start != -1:
        depth = 0
        in_str = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        cand = json.loads(text[start:i + 1])
                    except Exception:
                        break  # 括号配对成功但内容不是合法 JSON → 放弃扫描
                    if isinstance(cand, dict):
                        return cand
                    break

    type_desc = type(payload).__name__ if payload is not None else "不可解析文本"
    raise LlmOutputFormatError(
        f"prompt_json 解析失败（期望 JSON object，实际 {type_desc}）: {text[:80]}",
        validation_errors=[f"expected object, got {type_desc}"],
        extra={"raw_tail": text[-200:]},
    )


def _is_hollow(payload: Any) -> bool:
    """结构化结果是否为「空心对象」：所有字段（含嵌套）都没填出内容。

    空串/纯空白、空容器、None 视为空；数字与布尔（含 0 / False）视为有值。
    schema 全字段可选时（如 RequirementExtract），网关在 function_calling 下只回
    tool_call(arguments={}) 时，langchain 会用默认值造出一个「合法但全空」的对象，
    既不报错也不返回 None，只能靠这里识别出来走退化链（json_mode / prompt_json）。
    """
    def _empty(v: Any) -> bool:
        if v is None:
            return True
        if isinstance(v, str):
            return not v.strip()
        if isinstance(v, (list, tuple, set)):
            return all(_empty(x) for x in v)
        if isinstance(v, dict):
            return all(_empty(x) for x in v.values())
        return False

    if hasattr(payload, "model_dump"):
        payload = payload.model_dump()
    if not isinstance(payload, dict):
        return False
    return all(_empty(v) for v in payload.values())


def _make_structured(llm: Any, response_model: Type[BaseModel], method: str | None = None):
    """结构化输出兼容层（D1）。

    不同 OpenAI 兼容供应商对 with_structured_output 的支持不一致：
      - OpenAI / DeepSeek（deepseek-chat）     : 支持 function_calling 与 json_mode
      - 部分自建 vLLM / OneAPI 网关           : 不支持 tools，需退化 json_mode
      - 裸网关（vLLM 未开 tool-choice / 旧 Ollama 兼容层）: 两者皆不支持，
        退化 prompt_json——把 schema 写进提示词 + 本地解析校验，只要求能补全

    method=None（auto）时按 function_calling → json_mode → prompt_json 逐档退化；
    全部失败则抛最后一个错误（不吞异常）。method 显式指定时不加 prompt_json 档。

    D1b：模型输出不严格符合 json_schema 时（如 code_ref 输出成字符串），langchain
    会抛 OutputParserException。此处统一转成 LlmOutputFormatError（retryable=True），
    让上层 retry_with_backoff 有机会重试，而不是被 wrap 成 NODE.CONTEXT（不可重试）直接放弃。

    D1d：模型/gateway 没把 tool 填出来时 langchain 不报错——没吐 tool_call 返回 None，
    吐了空参数 tool_call 则用默认值造出「全空对象」。两种都按解析失败降级，
    否则全字段可选的 schema（RequirementExtract）会把空壳当成功、json_mode 永不触发。
    """
    from langchain_core.exceptions import OutputParserException
    from pydantic import ValidationError

    methods: list[str] = ["function_calling", "json_mode"] if method is None else [method]
    if method is None:
        methods.append("prompt_json")
    # 单调下探 memo：记住已探测到的最深档位，纠错重试从该档继续，
    # 不再反复撞已知会 400 的 function_calling / json_mode（省往返也省熔断样本）
    deepest: list[str] = []

    def _schema_instruction() -> str:
        schema = response_model.model_json_schema()
        return (
            "\n\n# 输出约束\n"
            "请只输出一个符合以下 JSON Schema 的 JSON 对象，"
            "不要加 Markdown 代码块，不要加任何额外文字：\n"
            f"{json.dumps(schema, ensure_ascii=False, indent=2)}"
        )

    class _StructuredWithFallback:
        async def ainvoke(self, msgs: list[BaseMessage], **_: Any) -> Any:
            start = methods.index(deepest[0]) if deepest else 0
            order = methods[start:]
            last_err: Exception | None = None
            for m in order:
                deepest[:] = [m]  # 无论成败，下次从当前档位继续
                try:
                    if m == "prompt_json":
                        out = await self._ainvoke_prompt_json(msgs)
                    else:
                        structured = llm.with_structured_output(response_model, method=m)
                        out = await structured.ainvoke(msgs)
                        # 模型未触发 tool call 时（如 DeepSeek 偶发纯文本回答），
                        # with_structured_output 不抛错而是返回 None；这里必须转成
                        # 可重试的 OUTPUT_FORMAT 错误走退化链，否则上层 dict(None)
                        # 会炸成不可重试的 TypeError('NoneType' object is not iterable)
                        if out is None:
                            raise OutputParserException(
                                f"模型未返回 tool_call（method={m}），未触发 function calling"
                            )
                        # 网关只回空参数 tool_call（arguments={}）时，langchain 会用默认值
                        # 造出「合法但全空」的对象；它不算成功语义，须降级到 json_mode /
                        # prompt_json 让模型真正填字段。声明了 allow_hollow_result 的
                        # schema（如 RouteMatch：空 = 无匹配）不受此检查影响。
                        if not getattr(response_model, "allow_hollow_result", False) and _is_hollow(out):
                            raise OutputParserException(
                                f"结构化输出为空壳（method={m}，所有字段均为空）"
                            )
                    return out
                except OutputParserException as e:
                    # 模型输出不合 schema → 归类 LLM.OUTPUT_FORMAT（可重试）
                    last_err = LlmOutputFormatError(
                        f"结构化输出解析失败（method={m}）: {e}", cause=e
                    )
                except Exception as e:  # noqa: BLE001 - 兼容层需兜底全部创建/调用异常
                    last_err = e
                    # 还有下一个 method 可试 → 继续退化；否则抛出最后一个错误
                    if m is order[-1]:
                        break
            assert last_err is not None
            raise last_err

        async def _ainvoke_prompt_json(self, msgs: list[BaseMessage]) -> BaseModel:
            """prompt_json 档：schema 进提示词，裸补全 + 本地解析校验。

            自带一次纠错重试：解析/校验失败时把错误与原始输出尾拼回提示词再要一次
            （弱模型首轮常见散文包裹 / 缺字段，盲重同样输入大概率复刻同样错误）。
            """
            msgs2 = list(msgs)
            last_human = max(
                (i for i, mm in enumerate(msgs2) if isinstance(mm, HumanMessage)), default=0
            )
            base_content = str(msgs2[last_human].content)
            last_err: LlmOutputFormatError | None = None
            raw_tail = ""
            for attempt in range(2):
                hint = "" if attempt == 0 else (
                    "\n\n# 上一次错误输出（请修正）\n"
                    f"错误: {last_err.message}\n"
                    f"你输出的末尾: {raw_tail}\n"
                    "请只输出一个符合 schema 的合法 JSON 对象，不要加任何说明文字。"
                )
                msgs2[last_human] = HumanMessage(content=base_content + _schema_instruction() + hint)
                resp = await llm.ainvoke(msgs2)
                text = str(getattr(resp, "content", "") or "")
                # 自部署推理模型（DeepSeek-R1 系蒸馏等）常把思考过程放在 <think> 块里，先剥掉
                text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
                raw_tail = text[-200:]
                try:
                    payload = _extract_json_object(text)
                except LlmOutputFormatError as e:
                    last_err = e
                    continue
                try:
                    return response_model.model_validate(payload)
                except ValidationError as e:
                    last_err = LlmOutputFormatError(
                        f"prompt_json 输出不符合 {response_model.__name__} schema: {e}",
                        validation_errors=[err["msg"] for err in e.errors()],
                        extra={"raw_tail": raw_tail},
                    )
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


def _messages_too_big(messages: list[BaseMessage], context_window: int) -> bool:
    chars = sum(len(str(getattr(m, "content", ""))) for m in messages)
    limit = int(context_window * 3.5 * 0.9)
    return chars > limit


def _shrink_messages(messages: list[BaseMessage], context_window: int) -> list[BaseMessage]:
    """SPEC 5.1 / 5.6 降级压缩：系统 prompt 不动，最早的 50% history 砍掉；
    如果仍超限，把每条 HumanMessage 取前 80%。窗口按当前调用的模型解析
    （官方规格 / provider 显式 context_window / 128k 下限）。
    """
    if len(messages) <= 2:
        return messages
    system = messages[0]
    mid = messages[1:-1]  # 去掉 system 和最后一条 user
    # 砍最早一半
    keep = mid[max(0, len(mid) // 2):]
    rebuilt: list[BaseMessage] = [system, *keep, messages[-1]]
    if not _messages_too_big(rebuilt, context_window):
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
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """异步版 JSON 结构化调用，套 SPEC 5 的完整治理。

    参数:
        response_type: 传给 dead_letter 的标识，区分 requirement_extract / logic_graph / build_question
        meta: 可选出参 dict。走 mock 兜底时写入 meta["mock"]=True，调用方据此识别
              「成功」结果其实是编造数据（真实 provider 全挂才会走到 mock）。
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
        # 上下文窗口按当前模型解析（官方规格表 / provider 显式 context_window / 128k 下限）
        context_window = resolve_context_window(
            str(p_spec.get("model") or ""), p_spec.get("context_window"))
        try:
            result = await _invoke_json_once(
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
            _mark_provider_success(p_spec)
            return result
        except LlmContextOverflowError as e:
            _log_provider_skip(p_spec["name"], e, ctx="上下文超限")
            if shrink_left <= 0:
                last_err = e
                continue
            messages = _shrink_messages(messages, context_window)
            shrink_left -= 1
            last_err = e
            try:
                result = await _invoke_json_once(
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
                _emit_stream_event({
                    "type": "provider_used",
                    "provider": p_spec["name"],
                    "model": p_spec["model"],
                })
                return result
            except DevFlowError as e2:
                _log_provider_skip(p_spec["name"], e2, ctx="压缩重试")
                last_err = e2
                continue
        except LlmTokenBudgetError as e:
            _log_provider_skip(p_spec["name"], e, ctx="token 预算触顶")
            last_err = e
            break
        except LlmRefusedError as e:
            _log_provider_skip(p_spec["name"], e, ctx="模型拒答")
            last_err = e
            continue
        except DevFlowError as e:
            _log_provider_skip(p_spec["name"], e)
            last_err = e
            continue

    # 走到这里，所有 provider 都失败（或一个都没配置，如测试强制 Mock 模式）
    if _use_mock_fallback():
        _log_mock_fallback(response_type, last_err)
        if meta is not None:
            meta["mock"] = True
        mock = _get_model("mock")
        return await _invoke_json_once(
            llm=mock, messages=messages,
            response_model=response_model, json_schema=json_schema,
            max_retries=1, breaker=default_breaker("llm_mock"),
            budget=budget, response_type=response_type, model_spec="mock",
        )
    assert last_err is not None, "未配置任何 LLM provider 且 LLM_USE_MOCK_FALLBACK 未开启"
    dead_letter_record(last_err, state_snapshot={"response_type": response_type})
    raise last_err


async def invoke_text(
    system_prompt: str,
    user_prompt: str,
    *,
    chat_history: list[BaseMessage] | None = None,
    max_retries: int = 2,
    meta: dict[str, Any] | None = None,
) -> str:
    """普通文本调用（压缩节点摘要用），async，走同一套治理。

    meta 含义同 invoke_json：mock 兜底时写入 meta["mock"]=True。
    """
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
        # 上下文窗口按当前模型解析（官方规格表 / provider 显式 context_window / 128k 下限）
        context_window = resolve_context_window(
            str(p_spec.get("model") or ""), p_spec.get("context_window"))

        async def _run(m: list[BaseMessage]) -> str:
            async with breaker.guard():
                resp = await llm.ainvoke(m)
            content = resp.content if isinstance(resp, BaseMessage) else str(resp)
            chars = sum(len(str(getattr(msg, "content", ""))) for msg in m) + len(str(content))
            budget.consume(_estimate_tokens(chars))
            _mark_provider_success(p_spec)
            return str(content)

        # 单次调用必须限时：慢模型/流式拖尾会吃满总 deadline（120s），表现为
        # check-llm（20s 单发探针）通过、真实调用却 DEADLINE.EXCEEDED 全走 Mock。
        # 45s 单发上限保证单次尝试能失败并轮换下一个 provider，而不是拖垮整个 deadline。
        policy = RetryPolicy(
            max_attempts=max_retries + 1,
            base_backoff=1.0,
            deadline_total=120.0,
            timeout_per_attempt=45.0,
        )
        dec = retry_with_backoff(policy, wrap_context=f"llm.invoke_text[{p_spec['name']}]")
        do_run = dec(_run)

        try:
            return await do_run(messages)
        except LlmContextOverflowError as e:
            _log_provider_skip(p_spec["name"], e, ctx="上下文超限")
            if shrink_left <= 0:
                last_err = e
                continue
            messages = _shrink_messages(messages, context_window)
            shrink_left -= 1
            try:
                return await do_run(messages)
            except DevFlowError as e2:
                _log_provider_skip(p_spec["name"], e2, ctx="压缩重试")
                last_err = e2
                continue
        except LlmRefusedError as e:
            _log_provider_skip(p_spec["name"], e, ctx="模型拒答")
            last_err = e
            continue
        except DevFlowError as e:
            _log_provider_skip(p_spec["name"], e)
            last_err = e
            continue

    # 全部失败（或未配置任何 provider）→ mock（可配置）
    if _use_mock_fallback():
        _log_mock_fallback("text", last_err)
        if meta is not None:
            meta["mock"] = True
        return cast(str, (await _get_model("mock").ainvoke(messages)).content)
    assert last_err is not None, "未配置任何 LLM provider 且 LLM_USE_MOCK_FALLBACK 未开启"
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

        # 同 invoke_text：单次调用限时（结构化输出更大更慢），避免慢模型把总
        # deadline 拖空——超时即失败并轮换下一个 provider。
        # 限时可配置（.env LLM_TIMEOUT_PER_ATTEMPT_SEC / LLM_DEADLINE_TOTAL_SEC）：
        # 制图 / 测试设计等大 JSON 生成天然慢，设 0 = 完全不限时
        pol = RetryPolicy(
            max_attempts=max_retries + 1,
            base_backoff=1.0,
            deadline_total=settings.LLM_DEADLINE_TOTAL_SEC or None,
            timeout_per_attempt=settings.LLM_TIMEOUT_PER_ATTEMPT_SEC or None,
        )
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
            # JsonOutputParser 对宽容解析很放开：模型输出「1. …」这类数字开头的纯文本
            # 会成功解析成裸 int/float 而不是抛错。schema 声明 object 时必须卡住类型，
            # 否则裸标量流到调用方下标取值处炸 TypeError（不可重试、错误码失真）。
            if (
                json_schema is not None
                and json_schema.get("type") == "object"
                and not isinstance(payload, dict)
            ):
                raise LlmOutputFormatError(
                    f"要求输出 JSON object，实际解析到 {type(payload).__name__}: {str(payload)[:80]}",
                    validation_errors=[f"expected object, got {type(payload).__name__}"],
                    extra={"raw_tail": str(content)[-200:]},
                )
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
    """测试隔离：清空模型实例缓存与 provider 运行期状态（避免跨测试交叉污染）。"""
    global _sticky_provider
    _model_cache.clear()
    _disabled_providers.clear()
    _sticky_provider = None


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
