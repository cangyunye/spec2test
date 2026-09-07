"""统一异常分类：所有会跨节点 / 跨模块传递的错误都继承 DevFlowError。

对应 SPEC 5.1 的 category / code / retryable 三要素：
  category : 大类（LLM / HTTP / CLI / NODE），便于路由和观测
  code     : 具体枚举值（如 LLM.CONTEXT_OVERFLOW）
  retryable: True 才会被 resilience.retry_with_backoff 重试

使用方式：节点层一般不直接构造子类，而是用本模块暴露的 wrap(e) 将裸异常映射到规范错误。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable


# ═══════════════════════════════════════════════════════════════════
# 枚举：code & category
# ═══════════════════════════════════════════════════════════════════
LLM_CONTEXT_OVERFLOW = "LLM.CONTEXT_OVERFLOW"
LLM_REFUSED = "LLM.REFUSED"
LLM_RATE_LIMIT = "LLM.RATE_LIMIT"
LLM_UPSTREAM = "LLM.UPSTREAM"
LLM_OUTPUT_FORMAT = "LLM.OUTPUT_FORMAT"
LLM_TOKEN_BUDGET = "LLM.TOKEN_BUDGET_EXHAUSTED"

HTTP_NETWORK = "HTTP.NETWORK"
HTTP_AUTH = "HTTP.AUTH"
HTTP_REQ_INVALID = "HTTP.REQ_INVALID"
HTTP_LINT_FAILED = "HTTP.LINT_FAILED"
HTTP_SESSION_TIMEOUT = "HTTP.SESSION_TIMEOUT"
HTTP_UPSTREAM = "HTTP.UPSTREAM"

CLI_NOT_FOUND = "CLI.NOT_FOUND"
CLI_TIMEOUT = "CLI.TIMEOUT"
CLI_INDEX_MISSING = "CLI.INDEX_MISSING"
CLI_EXIT_ERROR = "CLI.EXIT_ERROR"

NODE_CONTEXT = "NODE.CONTEXT"
CIRCUIT_OPEN = "CIRCUIT.OPEN"
DEADLINE_EXCEEDED = "DEADLINE.EXCEEDED"
CLARIFY_LOOP_EXHAUSTED = "CLARIFY.LOOP_EXHAUSTED"
EXEC_APPLY_FAILED = "EXEC.APPLY_FAILED"     # diff 落盘失败（上下文不匹配/路径非法），可回 code_gen 重新生成


# SPEC 5.1 默认可重试集合
DEFAULT_RETRYABLE_CODES: frozenset[str] = frozenset([
    LLM_RATE_LIMIT,
    LLM_UPSTREAM,
    LLM_OUTPUT_FORMAT,
    LLM_CONTEXT_OVERFLOW,       # 降级重试：先压缩再打，所以算 retryable
    HTTP_NETWORK,
    HTTP_LINT_FAILED,
    HTTP_SESSION_TIMEOUT,
    HTTP_UPSTREAM,
    CLI_TIMEOUT,
    CLI_INDEX_MISSING,
    CLI_EXIT_ERROR,             # CodeGraph JSON 偶尔 bad output，算可重试（限次）
    EXEC_APPLY_FAILED,          # diff 应用失败 → 回 code_gen 重新生成（限次）
])

DEFAULT_FALLBACK_CODES: frozenset[str] = frozenset([
    LLM_REFUSED,
    LLM_TOKEN_BUDGET,
    HTTP_AUTH,
    HTTP_REQ_INVALID,
    CLI_NOT_FOUND,
    NODE_CONTEXT,
    CIRCUIT_OPEN,
])


# ═══════════════════════════════════════════════════════════════════
# 基类
# ═══════════════════════════════════════════════════════════════════

class DevFlowError(Exception):
    """统一错误基类。

    字段对齐 SPEC 5.1：
      code            : 枚举码，路由依据
      retryable       : 是否允许重试（由 @retry_with_backoff 检查）
      category        : 大类，兼容 code 的前缀（如 LLM.XX → category="LLM"）
      retry_after_sec : 服务端主动要求的退避秒数（如 429 Retry-After）
      cause           : 原始异常（可空）
      extra           : 诊断附加信息（脱敏 payload、fallback 链路、token 用量…）
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool | None = None,
        category: str | None = None,
        retry_after_sec: float | None = None,
        cause: BaseException | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.category = category or (code.split(".", 1)[0] if "." in code else code)
        self.retryable = (
            retryable if retryable is not None else code in DEFAULT_RETRYABLE_CODES
        )
        self.retry_after_sec = retry_after_sec
        self.cause = cause
        self.extra = dict(extra or {})

    def __str__(self) -> str:  # pragma: no cover - 纯展示
        tag = "RETRY" if self.retryable else "FATAL"
        return f"[{tag}] {self.code}: {self.message}"


# ═══════════════════════════════════════════════════════════════════
# 语义友好的派生类（用 isinstance 判断）
# ═══════════════════════════════════════════════════════════════════
class LlmContextOverflowError(DevFlowError):
    """LLM 上下文超限：需要压缩 messages / 采样 code_context。"""

    def __init__(self, message: str, **kw: Any) -> None:
        super().__init__(LLM_CONTEXT_OVERFLOW, message, retryable=True, **kw)


class LlmRefusedError(DevFlowError):
    """鉴权/配额/安全策略拒绝：切 fallback 模型。"""

    def __init__(self, message: str, **kw: Any) -> None:
        super().__init__(LLM_REFUSED, message, retryable=False, **kw)


class LlmRateLimitError(DevFlowError):
    def __init__(self, message: str, *, retry_after_sec: float | None = None, **kw: Any) -> None:
        super().__init__(LLM_RATE_LIMIT, message, retryable=True,
                         retry_after_sec=retry_after_sec, **kw)


class LlmUpstreamError(DevFlowError):
    def __init__(self, message: str, **kw: Any) -> None:
        super().__init__(LLM_UPSTREAM, message, retryable=True, **kw)


class LlmOutputFormatError(DevFlowError):
    """LLM 返回的 JSON / Schema 不合规。"""

    def __init__(self, message: str, *, validation_errors: Iterable[str] | None = None, **kw: Any) -> None:
        extra = dict(kw.pop("extra", None) or {})
        if validation_errors is not None:
            extra["validation_errors"] = list(validation_errors)
        super().__init__(LLM_OUTPUT_FORMAT, message, retryable=True, extra=extra, **kw)


class LlmTokenBudgetError(DevFlowError):
    """配额烧完。"""

    def __init__(self, message: str, **kw: Any) -> None:
        super().__init__(LLM_TOKEN_BUDGET, message, retryable=False, **kw)


class HttpNetworkError(DevFlowError):
    def __init__(self, message: str, **kw: Any) -> None:
        super().__init__(HTTP_NETWORK, message, retryable=True, **kw)


class HttpAuthError(DevFlowError):
    def __init__(self, message: str, **kw: Any) -> None:
        super().__init__(HTTP_AUTH, message, retryable=False, **kw)


class HttpReqInvalidError(DevFlowError):
    def __init__(self, message: str, **kw: Any) -> None:
        super().__init__(HTTP_REQ_INVALID, message, retryable=False, **kw)


class HttpLintFailedError(DevFlowError):
    def __init__(self, message: str, **kw: Any) -> None:
        super().__init__(HTTP_LINT_FAILED, message, retryable=True, **kw)


class HttpSessionTimeoutError(DevFlowError):
    def __init__(self, message: str, **kw: Any) -> None:
        super().__init__(HTTP_SESSION_TIMEOUT, message, retryable=True, **kw)


class HttpUpstreamError(DevFlowError):
    def __init__(self, message: str, **kw: Any) -> None:
        super().__init__(HTTP_UPSTREAM, message, retryable=True, **kw)


class CliNotFoundError(DevFlowError):
    def __init__(self, message: str, **kw: Any) -> None:
        super().__init__(CLI_NOT_FOUND, message, retryable=False, **kw)


class CliTimeoutError(DevFlowError):
    def __init__(self, message: str, **kw: Any) -> None:
        super().__init__(CLI_TIMEOUT, message, retryable=True, **kw)


class CliIndexMissingError(DevFlowError):
    def __init__(self, message: str, **kw: Any) -> None:
        super().__init__(CLI_INDEX_MISSING, message, retryable=True, **kw)


class CliExitError(DevFlowError):
    """CLI 非 0 return。"""

    def __init__(self, message: str, *, stderr_tail: str | None = None, **kw: Any) -> None:
        extra = dict(kw.pop("extra", None) or {})
        if stderr_tail:
            extra["stderr_tail"] = stderr_tail
        super().__init__(CLI_EXIT_ERROR, message, retryable=True, extra=extra, **kw)


class ExecApplyFailedError(DevFlowError):
    """diff 落盘失败（上下文不匹配 / 路径非法 / 写入失败）。可重试：回 code_gen 重新生成 diff。"""

    def __init__(self, message: str, *, files: list[dict[str, Any]] | None = None, **kw: Any) -> None:
        extra = dict(kw.pop("extra", None) or {})
        if files:
            extra["files"] = files
        super().__init__(EXEC_APPLY_FAILED, message, retryable=True, extra=extra, **kw)


class NodeContextError(DevFlowError):
    """节点内部状态/断言失败（程序 bug），不可重试。"""
    def __init__(self, message: str, **kw: Any) -> None:
        super().__init__(NODE_CONTEXT, message, retryable=False, **kw)


class CircuitOpenError(DevFlowError):
    def __init__(self, breaker_name: str, **kw: Any) -> None:
        super().__init__(CIRCUIT_OPEN, f"熔断器 {breaker_name} 处于 Open 状态，请求被阻断",
                         retryable=False, **kw)


class DeadlineExceededError(DevFlowError):
    def __init__(self, total_sec: float, **kw: Any) -> None:
        super().__init__(DEADLINE_EXCEEDED,
                         f"总 deadline 已超 {total_sec:.1f}s，停止重试",
                         retryable=False, **kw)


class ClarifyLoopExhaustedError(DevFlowError):
    """需求澄清轮次达到上限，仍有缺失字段 → 不可重试终止。"""

    def __init__(self, message: str, **kw: Any) -> None:
        super().__init__(CLARIFY_LOOP_EXHAUSTED, message, retryable=False, **kw)


# ═══════════════════════════════════════════════════════════════════
# RetryPolicy 数据类
# ═══════════════════════════════════════════════════════════════════
@dataclass
class RetryPolicy:
    """SPEC 5.2 统一重试策略。"""

    max_attempts: int = 3
    base_backoff: float = 1.0
    max_backoff: float = 60.0
    multiplier: float = 2.0
    use_jitter: bool = True
    respect_retry_after_header: bool = True
    timeout_per_attempt: float | None = None
    deadline_total: float | None = None
    retryable_codes: frozenset[str] = field(default_factory=lambda: DEFAULT_RETRYABLE_CODES)

    def is_retryable(self, err: DevFlowError) -> bool:
        if isinstance(err, DeadlineExceededError) or isinstance(err, CircuitOpenError):
            return False
        return err.retryable and err.code in self.retryable_codes


# ═══════════════════════════════════════════════════════════════════
# 裸异常 → 语义错误的自动映射（wrap）
# ═══════════════════════════════════════════════════════════════════

_CTX_OVERFLOW_HINTS = (
    "context_length_exceeded",
    "context_window",
    "max_tokens is too large",
    "model_max_length",
    "prompt is too long",
    "prompt too long",
    "maximum context length",
    "reached max input length",
    "token length exceed",
)
_REFUSED_HINTS = (
    "invalid api key",
    "api key required",
    "incorrect api key",
    "wrong api key",
    "provider quota exceeded",
    "you exceeded your current quota",
    "your account is not approved",
    "quota exhausted",
    "no credit",
    "insufficient_quota",
    "content_filter",
    "content policy violation",
    "access denied",
    "forbidden",
    "unauthorized",
    "authentication error",
    "signature mismatch",
)
_RATE_LIMIT_HINTS = ("rate limit", "rate_limit", "too many requests", "retry-after")


def _match(text: str, hints: tuple[str, ...]) -> bool:
    t = (text or "").lower()
    return any(h in t for h in hints)


def wrap_exception(err: BaseException, *, context: str = "") -> DevFlowError:
    """把任意外部异常（HTTP/JSON/CLI/LLM/原生）映射成 DevFlowError。

    判断顺序（先具体后宽泛）：
      1. 已经是 DevFlowError 直接返回
      2. 根据 HTTP 状态码 / 响应体关键词匹配
      3. 根据 str(err) 关键词匹配
      4. 兜底：若是 Exception → 归类到 NODE.CONTEXT（不可重试，避免 bug 反复重试）
    """
    if isinstance(err, DevFlowError):
        return err

    status_code: int | None = getattr(err, "status_code", None)
    response_text: str = ""
    # 常见 HTTP 客户端结构：httpx.HTTPError 的 .response, aiohttp.ClientResponseError 的 .message
    resp = getattr(err, "response", None)
    if resp is not None:
        try:
            response_text = resp.text if callable(getattr(resp, "text", None)) else str(resp)
        except Exception:
            response_text = str(resp)

    message = str(err) or repr(err)
    combined = f"{message} {response_text}"
    prefixed = f"{context}: {message}" if context else message

    # ── HTTP 状态码优先 ───────────────────────────────────
    if status_code is None:
        # aiohttp 风格：从 status / code 属性拿
        for attr in ("status", "code"):
            v = getattr(err, attr, None)
            if isinstance(v, int):
                status_code = v
                break

    if isinstance(status_code, int):
        if status_code == 401 or status_code == 403:
            return HttpAuthError(prefixed, cause=err)
        if status_code == 408:
            return HttpSessionTimeoutError(prefixed, cause=err)
        if status_code == 422:
            return HttpLintFailedError(prefixed, cause=err)
        if status_code == 429:
            # 可能是 rate limit，也可能是 quota exceeded；关键词匹配区分
            if _match(combined, _REFUSED_HINTS):
                return LlmRefusedError(prefixed, cause=err)
            retry_after = _parse_retry_after(err)
            return LlmRateLimitError(prefixed, cause=err, retry_after_sec=retry_after)
        if status_code == 400:
            if _match(combined, _CTX_OVERFLOW_HINTS):
                return LlmContextOverflowError(prefixed, cause=err)
            if _match(combined, _REFUSED_HINTS):
                return LlmRefusedError(prefixed, cause=err)
            if _match(combined, _RATE_LIMIT_HINTS):
                return LlmRateLimitError(prefixed, cause=err)
            return HttpReqInvalidError(prefixed, cause=err)
        if 500 <= status_code < 600:
            return HttpUpstreamError(prefixed, cause=err)

    # ── 关键词兜底匹配（无状态码场景：CLI / JSON / LLM SDK） ─
    err_cls_name = type(err).__name__
    # 超时类
    if isinstance(err, (TimeoutError, TimeoutError)) or "timeout" in err_cls_name.lower():
        return CliTimeoutError(prefixed, cause=err)
    # 文件/二进制不存在
    if isinstance(err, FileNotFoundError) or "no such file" in combined.lower():
        return CliNotFoundError(prefixed, cause=err)
    # JSON 解码失败 → 格式类（可能是 LLM 输出坏了，也可能是 CLI 输出坏了）
    if isinstance(err, (ValueError, json.JSONDecodeError if False else Exception)):
        # JSONDecodeError 本身就是 ValueError 子类
        if isinstance(err, (ValueError,)) and (
            "json" in err_cls_name.lower() or _match(combined, ("expecting value", "unexpected "))
        ):
            return LlmOutputFormatError(prefixed, cause=err)

    # LLM 关键字
    if _match(combined, _CTX_OVERFLOW_HINTS):
        return LlmContextOverflowError(prefixed, cause=err)
    if _match(combined, _REFUSED_HINTS):
        return LlmRefusedError(prefixed, cause=err)
    if _match(combined, _RATE_LIMIT_HINTS):
        retry_after = _parse_retry_after(err)
        return LlmRateLimitError(prefixed, cause=err, retry_after_sec=retry_after)

    # 网络/连接类
    conn_keywords = (
        "connection", "connect", "network", "dns", "tls", "ssl", "certificate",
        "eof", "broken pipe", "remote protocol", "name or service not known",
        "temporary failure in name resolution",
    )
    if _match(combined, conn_keywords):
        return HttpNetworkError(prefixed, cause=err)

    # 子进程 exit 非 0
    if isinstance(err, (ChildProcessError,)) or "returned non-zero exit" in combined.lower():
        return CliExitError(prefixed, cause=err)

    # 最终兜底：不要把程序 bug 标记成可重试
    return NodeContextError(prefixed, cause=err)


def _parse_retry_after(err: BaseException) -> float | None:
    """从异常对象中提取 Retry-After 头（秒）。"""
    resp = getattr(err, "response", None)
    headers = getattr(resp, "headers", None)
    if not headers:
        return None
    raw = None
    if isinstance(headers, dict):
        raw = headers.get("Retry-After") or headers.get("retry-after")
    else:
        for k in ("Retry-After", "retry-after"):
            try:
                raw = headers.get(k)
            except Exception:
                continue
            if raw is not None:
                break
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


# 不把 json 暴露在模块作用域 import 里以免循环
import json as _json  # noqa: E402
