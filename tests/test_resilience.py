"""弹性治理单元测试：覆盖 errors / resilience / llm_client 的治理能力。

注意：所有涉及 sleep 的场景都把 base_backoff 压到 0.0001（1ms 量级），真实测试速度不慢。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from devflow.errors import (
    CIRCUIT_OPEN,
    CLI_EXIT_ERROR,
    CLI_NOT_FOUND,
    CLI_TIMEOUT,
    DEADLINE_EXCEEDED,
    DEFAULT_RETRYABLE_CODES,
    HTTP_AUTH,
    HTTP_LINT_FAILED,
    HTTP_NETWORK,
    HTTP_REQ_INVALID,
    HTTP_SESSION_TIMEOUT,
    HTTP_UPSTREAM,
    LLM_CONTEXT_OVERFLOW,
    LLM_OUTPUT_FORMAT,
    LLM_RATE_LIMIT,
    LLM_REFUSED,
    LLM_TOKEN_BUDGET,
    LLM_UPSTREAM,
    NODE_CONTEXT,
    CliExitError,
    CliIndexMissingError,
    CliNotFoundError,
    CliTimeoutError,
    CircuitOpenError,
    DeadlineExceededError,
    DevFlowError,
    HttpAuthError,
    HttpLintFailedError,
    HttpNetworkError,
    HttpReqInvalidError,
    HttpSessionTimeoutError,
    HttpUpstreamError,
    LlmContextOverflowError,
    LlmOutputFormatError,
    LlmRateLimitError,
    LlmRefusedError,
    LlmTokenBudgetError,
    LlmUpstreamError,
    NodeContextError,
    RetryPolicy,
    wrap_exception,
)
from devflow.resilience import (
    CircuitBreaker,
    TokenBudget,
    dead_letter_record,
    default_breaker,
    retry_with_backoff,
)


# ═══════════════════════════════════════════════════════════════════
# 1. wrap_exception：裸异常 → 语义错误
# ═══════════════════════════════════════════════════════════════════

def _make_http_err(code: int, body: str = "", cls_name: str = "HTTPError"):
    class FakeResponse:
        def __init__(self, b: str) -> None:
            self.text = b
            self.headers = {}
    class E(Exception):
        status_code = code
    e = E(f"HTTP {code}: {body}")
    e.response = FakeResponse(body)
    e.__class__.__name__ = cls_name
    return e


def test_wrap_http_status_401_is_auth():
    e = wrap_exception(_make_http_err(401, "Invalid API key"))
    assert isinstance(e, HttpAuthError)
    assert e.code == HTTP_AUTH
    assert e.retryable is False


def test_wrap_http_403_forbidden():
    e = wrap_exception(_make_http_err(403, "Forbidden: access denied"))
    assert e.code == HTTP_AUTH and not e.retryable


def test_wrap_http_408_session_timeout():
    e = wrap_exception(_make_http_err(408))
    assert isinstance(e, HttpSessionTimeoutError)
    assert e.code == HTTP_SESSION_TIMEOUT and e.retryable


def test_wrap_http_422_lint_failed():
    e = wrap_exception(_make_http_err(422, "LINT_FAILED: flake8 errors"))
    assert isinstance(e, HttpLintFailedError) and e.code == HTTP_LINT_FAILED and e.retryable


def test_wrap_http_429_quota_exceeded_is_refused():
    e = wrap_exception(_make_http_err(429, "insufficient_quota You exceeded your current quota"))
    assert isinstance(e, LlmRefusedError) and e.code == LLM_REFUSED and not e.retryable


def test_wrap_http_429_rate_limit():
    err = _make_http_err(429, "rate limit exceeded")
    err.response.headers = {"Retry-After": "5"}
    e = wrap_exception(err)
    assert isinstance(e, LlmRateLimitError) and e.code == LLM_RATE_LIMIT and e.retryable
    assert e.retry_after_sec == 5.0


def test_wrap_http_400_context_overflow():
    e = wrap_exception(_make_http_err(400, "context_length_exceeded model_max_length"))
    assert isinstance(e, LlmContextOverflowError) and e.code == LLM_CONTEXT_OVERFLOW and e.retryable


def test_wrap_http_400_invalid_params():
    e = wrap_exception(_make_http_err(400, "INVALID_PARAMS: request_id missing"))
    assert isinstance(e, HttpReqInvalidError) and e.code == HTTP_REQ_INVALID and not e.retryable


def test_wrap_http_502_bad_gateway():
    e = wrap_exception(_make_http_err(502, "Bad Gateway"))
    assert isinstance(e, HttpUpstreamError) and e.code == HTTP_UPSTREAM and e.retryable


def test_wrap_file_not_found_is_cli_notfound():
    e = wrap_exception(FileNotFoundError("[Errno 2] No such file or directory: 'codegraph'"))
    assert isinstance(e, CliNotFoundError) and e.code == CLI_NOT_FOUND and not e.retryable


def test_wrap_timeout_error():
    e = wrap_exception(TimeoutError("codegraph explore timed out"))
    assert isinstance(e, CliTimeoutError) and e.code == CLI_TIMEOUT and e.retryable


def test_wrap_json_decode_error_is_output_format():
    raw = """```json
    { bad json
    """
    err = json.JSONDecodeError("Expecting value", raw, 10)
    e = wrap_exception(err)
    assert isinstance(e, LlmOutputFormatError) and e.code == LLM_OUTPUT_FORMAT and e.retryable


def test_wrap_keyerror_is_node_context():
    e = wrap_exception(KeyError("logic_graph is missing 'nodes'"))
    assert isinstance(e, NodeContextError) and e.code == NODE_CONTEXT and not e.retryable


def test_wrap_connection_error():
    class MyConnErr(Exception):
        pass
    e = wrap_exception(MyConnErr("ConnectionRefusedError: cannot connect to host: HTTP connection failed"))
    assert isinstance(e, HttpNetworkError) and e.code == HTTP_NETWORK and e.retryable


# SPEC 5.1 DEFAULT_RETRYABLE_CODES 抽样
def test_default_retryable_codes_integrity():
    assert LLM_RATE_LIMIT in DEFAULT_RETRYABLE_CODES
    assert LLM_UPSTREAM in DEFAULT_RETRYABLE_CODES
    assert LLM_OUTPUT_FORMAT in DEFAULT_RETRYABLE_CODES
    assert LLM_CONTEXT_OVERFLOW in DEFAULT_RETRYABLE_CODES
    assert HTTP_NETWORK in DEFAULT_RETRYABLE_CODES
    assert CLI_TIMEOUT in DEFAULT_RETRYABLE_CODES
    # 不可重试
    assert LLM_REFUSED not in DEFAULT_RETRYABLE_CODES
    assert HTTP_AUTH not in DEFAULT_RETRYABLE_CODES
    assert CLI_NOT_FOUND not in DEFAULT_RETRYABLE_CODES


# ═══════════════════════════════════════════════════════════════════
# 2. retry_with_backoff
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_retry_success_on_second_attempt():
    call_counter = {"n": 0}
    fast_pol = RetryPolicy(max_attempts=3, base_backoff=0.001, multiplier=1.1, use_jitter=False)

    @retry_with_backoff(fast_pol)
    async def fn() -> str:
        call_counter["n"] += 1
        if call_counter["n"] < 2:
            raise LlmUpstreamError("503 temporarily unavailable")
        return "ok"

    assert await fn() == "ok"
    assert call_counter["n"] == 2


@pytest.mark.asyncio
async def test_retry_exhausted_raises_last():
    call_counter = {"n": 0}
    fast_pol = RetryPolicy(max_attempts=3, base_backoff=0.001, multiplier=1.1)

    @retry_with_backoff(fast_pol)
    async def fn() -> str:
        call_counter["n"] += 1
        raise HttpNetworkError("always fail")

    with pytest.raises(HttpNetworkError):
        await fn()
    # max_attempts=3 表示总尝试 3 次
    assert call_counter["n"] == 3


@pytest.mark.asyncio
async def test_retry_fatal_never_retries():
    call_counter = {"n": 0}
    fast_pol = RetryPolicy(max_attempts=5, base_backoff=0.001)

    @retry_with_backoff(fast_pol)
    async def fn() -> str:
        call_counter["n"] += 1
        raise LlmRefusedError("invalid api key")

    with pytest.raises(LlmRefusedError):
        await fn()
    # 立刻失败，不重试
    assert call_counter["n"] == 1


@pytest.mark.asyncio
async def test_retry_backoff_respects_retry_after_header(monkeypatch):
    sleeps: list[float] = []
    fast_pol = RetryPolicy(max_attempts=3, base_backoff=0.001, respect_retry_after_header=True, use_jitter=False)
    real_sleep = asyncio.sleep

    async def fake_sleep(s: float):
        sleeps.append(s)
        # 真实 sleep 0 让 event loop 放行，但不会再次进入 fake_sleep（无递归）
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    @retry_with_backoff(fast_pol)
    async def fn() -> str:
        raise LlmRateLimitError("chill", retry_after_sec=0.1)

    with pytest.raises(LlmRateLimitError):
        await fn()
    # 第一次 retry sleep 应该等于 retry_after_sec (0.1)，而不是 base_backoff (0.001)
    assert any(s > 0.05 for s in sleeps)


@pytest.mark.asyncio
async def test_retry_deadline_exceeded():
    pol = RetryPolicy(max_attempts=50, base_backoff=0.05, deadline_total=0.05, use_jitter=False)

    @retry_with_backoff(pol)
    async def fn() -> str:
        raise HttpNetworkError("network gone")

    with pytest.raises(DeadlineExceededError) as exc:
        await fn()
    assert exc.value.code == DEADLINE_EXCEEDED and exc.value.retryable is False


@pytest.mark.asyncio
async def test_retry_wraps_bare_exception(monkeypatch):
    fast_pol = RetryPolicy(max_attempts=2, base_backoff=0.0001)

    @retry_with_backoff(fast_pol, wrap_context="demo")
    async def fn() -> str:
        raise RuntimeError("connection refused by server")  # 裸异常

    with pytest.raises(DevFlowError) as exc:
        await fn()
    assert exc.value.code == HTTP_NETWORK  # 关键词 connection
    assert "demo" in exc.value.message


# ═══════════════════════════════════════════════════════════════════
# 3. CircuitBreaker
# ═══════════════════════════════════════════════════════════════════

def test_breaker_initial_closed():
    b = CircuitBreaker("t", min_samples=3, failure_threshold_pct=0.5, window_size_sec=60.0,
                       open_window_sec=0.05)
    assert b.state() == CircuitBreaker.STATE_CLOSED


def test_breaker_opens_after_high_fail_rate():
    b = CircuitBreaker("t", min_samples=3, failure_threshold_pct=0.5, window_size_sec=60.0,
                       open_window_sec=0.05)
    # 3 个请求，2 失败 → 失败率 66% ≥ 50%
    for ok, ex in [(True, None), (False, RuntimeError), (False, RuntimeError)]:
        try:
            with b.guard_sync():
                if not ok:
                    raise ex  # type: ignore[misc]
        except Exception:
            pass
    assert b.state() == CircuitBreaker.STATE_OPEN
    # Open 状态立刻阻断
    with pytest.raises(CircuitOpenError) as exc:
        with b.guard_sync():
            pass
    assert exc.value.code == CIRCUIT_OPEN


def test_breaker_transitions_to_half_open_and_recover(monkeypatch):
    b = CircuitBreaker("t", min_samples=1, failure_threshold_pct=1.0,
                       open_window_sec=0.01, half_open_probes=1,
                       window_size_sec=60.0)
    # 1 次失败就开
    try:
        with b.guard_sync():
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert b.state() == CircuitBreaker.STATE_OPEN
    # 等 open_window_sec
    async def wait_and_check():
        await asyncio.sleep(0.02)
        assert b.state() == CircuitBreaker.STATE_HALF_OPEN
        # 成功探测
        with b.guard_sync():
            pass
        assert b.state() == CircuitBreaker.STATE_CLOSED
    asyncio.run(wait_and_check())


# ═══════════════════════════════════════════════════════════════════
# 4. TokenBudget
# ═══════════════════════════════════════════════════════════════════

def test_token_budget_soft_and_hard_limits():
    tb = TokenBudget(daily_tokens=1000, soft_threshold_pct=0.7, hard_threshold_pct=0.9)
    assert not tb.is_soft_limited() and not tb.is_hard_limited()
    tb.consume(700)  # 70%
    assert tb.is_soft_limited() and not tb.is_hard_limited()
    tb.consume(250)  # 95%
    assert tb.is_hard_limited()
    assert tb.consumed_today() == 950
    assert tb.remaining_today() == 50


# ═══════════════════════════════════════════════════════════════════
# 5. 死信记录
# ═══════════════════════════════════════════════════════════════════

def test_dead_letter_record_writes_jsonl(tmp_path: Path):
    folder = tmp_path / "dlq"
    err = LlmRefusedError("bad key", extra={"raw_tail": "Invalid API key"})
    state_snapshot = {
        "messages": [{"role": "user", "content": "x" * 1000}],
        "code_context": [{"file_path": "a.py"}] * 3,
        "logic_graph": {"graph_id": "g", "nodes": [{}], "edges": []},
        "foo": "bar",
    }
    path = dead_letter_record(err, state_snapshot=state_snapshot, root_dir=folder)
    assert path.exists()
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["code"] == LLM_REFUSED
    # messages PII 被脱敏：只保留 count
    assert rec["state"]["messages"] == {"count": 1}
    # code_context 脱敏：count
    assert rec["state"]["code_context"] == {"count": 3}
    # logic_graph 只保留 id + 节点/边数量
    assert rec["state"]["logic_graph"] == {"graph_id": "g", "nodes": 1, "edges": 0}
    # 小字段正常
    assert rec["state"]["foo"] == "bar"
    # extra 完整
    assert rec["extra"]["raw_tail"] == "Invalid API key"
