"""弹性治理模块：重试 / 熔断 / token 预算 / deadline / 死信。

对外暴露：
  - @retry_with_backoff(policy, on_error_wrap=True)  async 函数装饰器
  - CircuitBreaker(name, ...)      上下文管理器 + 装饰器
  - TokenBudget(daily_tokens, ...) 全局扣减器
  - dead_letter_record(err, state_snapshot)  写死信 JSONL
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Deque, Iterator, ParamSpec, TypeVar

from .errors import (
    CIRCUIT_OPEN,
    DEADLINE_EXCEEDED,
    CircuitOpenError,
    DeadlineExceededError,
    DevFlowError,
    RetryPolicy,
    wrap_exception,
)

logger = logging.getLogger(__name__)

P = ParamSpec("P")
R = TypeVar("R")


# ═══════════════════════════════════════════════════════════════════
# 1. 指数退避 + 重试装饰器
# ═══════════════════════════════════════════════════════════════════

def retry_with_backoff(
    policy: RetryPolicy | None = None,
    *,
    on_error_wrap: bool = True,
    wrap_context: str = "",
):
    """给 async 函数套重试。

    规则：
      - 裸异常先 wrap_exception（除非 on_error_wrap=False）
      - DevFlowError.retryable=False 立刻抛
      - 否则按 RetryPolicy 计算 sleep 时间并尊重 Retry-After
      - `attempt_index` 从 1 开始（第 1 次是首次真实调用）

    示例：
        @retry_with_backoff(RetryPolicy(max_attempts=3, base_backoff=1.0))
        async def call_llm(...): ...
    """
    pol = policy or RetryPolicy()

    def decorator(fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            deadline_end = (
                (time.monotonic() + pol.deadline_total)
                if pol.deadline_total else None
            )
            last_exc: DevFlowError | None = None
            for attempt in range(1, pol.max_attempts + 1):
                if deadline_end is not None:
                    remain = deadline_end - time.monotonic()
                    if remain <= 0:
                        raise DeadlineExceededError(pol.deadline_total or 0)

                try:
                    # None / 0 = 单次不限时（0 的 falsy 语义与 deadline_total 一致，
                    # 避免被 min() 归一成 wait_for(0) 立即超时）
                    if not pol.timeout_per_attempt:
                        return await fn(*args, **kwargs)
                    # deadline_total 存在时取更紧的；无 deadline 单次限时独立生效
                    tout = (
                        pol.timeout_per_attempt
                        if deadline_end is None
                        else min(pol.timeout_per_attempt, max(0.1, remain))
                    )
                    return await asyncio.wait_for(fn(*args, **kwargs), timeout=tout)
                except DevFlowError as e:
                    err = e
                except Exception as e:  # noqa: BLE001 - 需要 wrap 全部
                    err = (
                        wrap_exception(e, context=wrap_context or fn.__name__)
                        if on_error_wrap else NodeContextError(
                            f"{wrap_context or fn.__name__}: {e}", cause=e
                        ) if False else wrap_exception(e, context=wrap_context or fn.__name__)
                    )

                # 到这里说明本次失败，决定是否重试
                last_exc = err
                if attempt >= pol.max_attempts or not pol.is_retryable(err):
                    break
                # deadline 最后一次机会
                if deadline_end is not None:
                    remaining_total = deadline_end - time.monotonic()
                    if remaining_total <= 0:
                        raise DeadlineExceededError(pol.deadline_total or 0)
                sleep_sec = _backoff_seconds(pol, attempt, err)
                if deadline_end is not None:
                    sleep_sec = min(sleep_sec, max(0.0, deadline_end - time.monotonic() - 0.05))
                    if sleep_sec <= 0:
                        raise DeadlineExceededError(pol.deadline_total or 0)
                await asyncio.sleep(sleep_sec)
            # 所有尝试用尽
            assert last_exc is not None
            raise last_exc

        # 便于外部拿到原函数（调试 / 测试）
        wrapper.__wrapped__ = fn  # type: ignore[attr-defined]
        wrapper.__name__ = fn.__name__
        wrapper.__qualname__ = fn.__qualname__
        return wrapper  # type: ignore[return-value]

    return decorator


def _backoff_seconds(policy: RetryPolicy, attempt: int, err: DevFlowError) -> float:
    """第 attempt 次（>=1）失败后，下一次等待秒数。"""
    if (
        policy.respect_retry_after_header
        and err.retry_after_sec is not None
        and err.retry_after_sec > 0
    ):
        base = float(err.retry_after_sec)
    else:
        exp = max(attempt - 1, 0)
        base = min(policy.base_backoff * (policy.multiplier ** exp), policy.max_backoff)
    if policy.use_jitter:
        # ±25%
        jitter = 1 + random.uniform(-0.25, 0.25)  # noqa: S311 - 非安全场景
        base = max(0.05, base * jitter)
    return base


# ═══════════════════════════════════════════════════════════════════
# 2. 熔断器（Circuit Breaker）
# ═══════════════════════════════════════════════════════════════════

@dataclass
class _Window:
    events: Deque[tuple[float, bool]] = field(default_factory=lambda: deque())
    total_in_half_open: int = 0
    failed_in_half_open: int = 0


class CircuitBreaker:
    """SPEC 5.4 熔断器，线程/协程安全（用 threading.Lock，async 协程也不会并发到不同 OS 线程）。

    状态切换：
      Closed: 正常计数；最近 `window_size` 秒内失败 >= failure_threshold_pct 且样本量 >= min_samples → Open
      Open  : 任何调用立刻抛 CircuitOpenError；open_window_sec 后切 Half-Open
      Half-Open: 放 `half_open_probes` 个请求；全部成功→ Closed，任一失败→ Open
    """

    STATE_CLOSED = "CLOSED"
    STATE_OPEN = "OPEN"
    STATE_HALF_OPEN = "HALF_OPEN"

    def __init__(
        self,
        name: str,
        *,
        window_size_sec: float = 60.0,
        min_samples: int = 5,
        failure_threshold_pct: float = 0.5,
        open_window_sec: float = 30.0,
        half_open_probes: int = 1,
    ) -> None:
        self.name = name
        self.window_size_sec = window_size_sec
        self.min_samples = min_samples
        self.failure_threshold_pct = failure_threshold_pct
        self.open_window_sec = open_window_sec
        self.half_open_probes = half_open_probes

        self._lock = threading.Lock()
        self._state = self.STATE_CLOSED
        self._opened_at: float | None = None
        self._window = _Window()
        self._half_open_left: int = 0

    # ── 状态查询 ────────────────────────────────────────
    def state(self) -> str:
        with self._lock:
            self._maybe_tick_nolock()
            return self._state

    # ── 同步上下文（给同步函数用） ────────────────────────
    @contextlib.contextmanager
    def guard_sync(self) -> Iterator[None]:
        self._preflight()
        failed = False
        try:
            yield
        except BaseException:
            failed = True
            raise
        finally:
            self._record_result(not failed)

    # ── async 上下文（给 async 函数 / provider 用） ──────
    @contextlib.asynccontextmanager
    async def guard(self) -> AsyncIterator[None]:
        self._preflight()
        failed = False
        try:
            yield
        except BaseException:
            failed = True
            raise
        finally:
            self._record_result(not failed)

    # ── 装饰器（支持 async） ────────────────────────────
    def __call__(self, fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            self._preflight()
            failed = False
            try:
                return await fn(*args, **kwargs)
            except BaseException:
                failed = True
                raise
            finally:
                self._record_result(not failed)
        wrapper.__name__ = fn.__name__  # type: ignore[attr-defined]
        wrapper.__wrapped__ = fn  # type: ignore[attr-defined]
        return wrapper  # type: ignore[return-value]

    # ── 内部 ────────────────────────────────────────────
    def _preflight(self) -> None:
        with self._lock:
            self._maybe_tick_nolock()
            if self._state == self.STATE_OPEN:
                raise CircuitOpenError(self.name)
            if self._state == self.STATE_HALF_OPEN:
                if self._half_open_left <= 0:
                    # 半开探测配额用尽，临时拒绝其余请求，避免并发打穿
                    raise CircuitOpenError(self.name)
                self._half_open_left -= 1

    def _record_result(self, ok: bool) -> None:
        with self._lock:
            self._maybe_tick_nolock()
            now = time.monotonic()
            if self._state == self.STATE_CLOSED:
                self._window.events.append((now, ok))
                self._evict_nolock(now)
                self._evaluate_closed_nolock()
            elif self._state == self.STATE_HALF_OPEN:
                self._window.total_in_half_open += 1
                if not ok:
                    self._window.failed_in_half_open += 1
                # 探测全部返回 → 决定是否切回 Closed
                if self._half_open_left <= 0:
                    failed_pct = (
                        self._window.failed_in_half_open / self._window.total_in_half_open
                        if self._window.total_in_half_open else 0
                    )
                    if failed_pct > 0:
                        self._set_state_nolock(self.STATE_OPEN)
                    else:
                        self._set_state_nolock(self.STATE_CLOSED)

    def _maybe_tick_nolock(self) -> None:
        """把 OPEN → HALF_OPEN 的时间到点跳转。"""
        now = time.monotonic()
        if self._state == self.STATE_OPEN and self._opened_at is not None:
            if now - self._opened_at >= self.open_window_sec:
                self._window = _Window()
                self._half_open_left = self.half_open_probes
                self._set_state_nolock(self.STATE_HALF_OPEN)
        if self._state == self.STATE_CLOSED:
            self._evict_nolock(now)

    def _evict_nolock(self, now: float) -> None:
        w = self._window.events
        while w and (now - w[0][0]) > self.window_size_sec:
            w.popleft()

    def _evaluate_closed_nolock(self) -> None:
        events = self._window.events
        total = len(events)
        if total < self.min_samples:
            return
        failed = sum(1 for _, ok in events if not ok)
        if failed / total >= self.failure_threshold_pct:
            self._set_state_nolock(self.STATE_OPEN)

    def _set_state_nolock(self, state: str) -> None:
        self._state = state
        if state == self.STATE_OPEN:
            self._opened_at = time.monotonic()
        else:
            self._opened_at = None

    def reset(self) -> None:
        """测试/调试用：强制重置熔断器到 Closed 初始状态（清空计数窗口）。"""
        with self._lock:
            self._state = self.STATE_CLOSED
            self._opened_at = None
            self._window = _Window()
            self._half_open_left = 0


# ═══════════════════════════════════════════════════════════════════
# 3. Token 预算（TokenBudget）
# ═══════════════════════════════════════════════════════════════════

class TokenBudget:
    """SPEC 5.5：按自然日 24h 计算预算消耗（进程内内存统计；生产可接 Redis）。"""

    def __init__(
        self,
        *,
        daily_tokens: int = 0,  # 0 = 不启用预算限制
        soft_threshold_pct: float = 0.70,
        hard_threshold_pct: float = 0.95,
    ) -> None:
        self.daily_tokens = daily_tokens
        self.soft_threshold_pct = soft_threshold_pct
        self.hard_threshold_pct = hard_threshold_pct
        self._lock = threading.Lock()
        self._day: date | None = None
        self._used: int = 0

    def consumed_today(self) -> int:
        with self._lock:
            self._rotate_if_needed_nolock()
            return self._used

    def is_soft_limited(self) -> bool:
        if self.daily_tokens <= 0:
            return False
        with self._lock:
            self._rotate_if_needed_nolock()
            return self._used >= int(self.daily_tokens * self.soft_threshold_pct)

    def is_hard_limited(self) -> bool:
        if self.daily_tokens <= 0:
            return False
        with self._lock:
            self._rotate_if_needed_nolock()
            return self._used >= int(self.daily_tokens * self.hard_threshold_pct)

    def consume(self, tokens: int) -> None:
        """预算只扣不扣负；用于观测 + 判断，扣完不直接抛错（由调用方决定抛 LlmTokenBudgetError）。"""
        if tokens <= 0 or self.daily_tokens <= 0:
            return
        with self._lock:
            self._rotate_if_needed_nolock()
            self._used += int(tokens)

    def remaining_today(self) -> int:
        if self.daily_tokens <= 0:
            return 0
        with self._lock:
            self._rotate_if_needed_nolock()
            return max(0, self.daily_tokens - self._used)

    def _rotate_if_needed_nolock(self) -> None:
        today = datetime.now(timezone.utc).date()
        if self._day != today:
            self._day = today
            self._used = 0


# ═══════════════════════════════════════════════════════════════════
# 4. 死信记录（SPEC 5.7）
#    两种形式：
#      a) 进程内 State 队列: DeadLetter dataclass + push_dead_letter(list, dl, max_items=N)
#      b) 落盘 JSONL:      dead_letter_record(err, state_snapshot=..., root_dir=...)
# ═══════════════════════════════════════════════════════════════════


@dataclass
class DeadLetter:
    """进程内一条死信（仅保留必要字段，不占内存）。"""
    id: str
    node: str
    error_code: str
    error_message: str
    retryable: bool
    retry_after_sec: float | None
    stage: str
    snapshot: dict[str, Any] = field(default_factory=dict)
    cause_repr: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(
        default_factory=lambda: datetime.now(tz=timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "node": self.node,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "retryable": self.retryable,
            "retry_after_sec": self.retry_after_sec,
            "stage": self.stage,
            "snapshot": self.snapshot,
            "cause_repr": self.cause_repr,
            "extra": self.extra,
            "created_at": self.created_at,
        }


def push_dead_letter(
    queue: list[dict[str, Any] | DeadLetter],
    item: DeadLetter,
    *,
    max_items: int = 200,
) -> None:
    """把死信追加到 State 队列（FIFO，限长 max_items）。"""
    queue.append(item.to_dict())
    if len(queue) > max_items:
        # 滚动丢弃头部（保留最新 max_items 条；对 State 内存友好）
        del queue[: len(queue) - max_items]


def dead_letter_record(
    err: DevFlowError,
    *,
    state_snapshot: dict[str, Any] | None = None,
    root_dir: str | Path = "./data/dead_letter",
) -> Path:
    """把最终未被重试解决的错误写成 JSONL。

    state_snapshot 会被**脱敏**：自动把 messages / code_context / logic_graph.mermaid_source
    等大体量或可能带 PII 的字段仅保留 count / 首行摘要。
    """
    folder = Path(root_dir)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{date.today().isoformat()}.jsonl"
    line = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "code": err.code,
        "category": err.category,
        "retryable": err.retryable,
        "message": err.message,
        "cause": (str(err.cause) if err.cause else None),
        "extra": {k: _safe_shrink(v) for k, v in (err.extra or {}).items()},
        "state": _sanitize_state(state_snapshot or {}),
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
    return path


def _safe_shrink(value: Any) -> Any:
    if isinstance(value, str) and len(value) > 500:
        return f"<str len={len(value)} head={value[:80]!r}>"
    if isinstance(value, list) and len(value) > 50:
        return f"<list len={len(value)}>"
    if isinstance(value, dict) and len(value) > 50:
        return f"<dict keys={list(value)[:10]} total={len(value)}>"
    return value


def _sanitize_state(state: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in state.items():
        if k in {"messages", "conversation_history"}:
            out[k] = {"count": len(v) if isinstance(v, list) else None}
        elif k == "code_context":
            out[k] = {"count": len(v) if isinstance(v, list) else None}
        elif k == "logic_graph":
            if isinstance(v, dict):
                out[k] = {
                    "graph_id": v.get("graph_id"),
                    "nodes": len(v.get("nodes", []) or []),
                    "edges": len(v.get("edges", []) or []),
                }
            else:
                out[k] = _safe_shrink(v)
        else:
            out[k] = _safe_shrink(v)
    return out


# ═══════════════════════════════════════════════════════════════════
# 单例：全局默认熔断器（3 个外部依赖各一个，可注入覆盖）
# ═══════════════════════════════════════════════════════════════════

_DEFAULT_BREAKERS: dict[str, CircuitBreaker] = {
    "opencode_http": CircuitBreaker("opencode_http"),
    "codegraph_cli": CircuitBreaker("codegraph_cli"),
    "archify_cli": CircuitBreaker("archify_cli"),
    "llm_primary": CircuitBreaker("llm_primary", failure_threshold_pct=0.7, min_samples=8),
}


def default_breaker(name: str) -> CircuitBreaker:
    """获取命名熔断器（按需创建）。"""
    if name not in _DEFAULT_BREAKERS:
        _DEFAULT_BREAKERS[name] = CircuitBreaker(name)
    return _DEFAULT_BREAKERS[name]


def reset_all_default_breakers() -> None:
    """测试隔离：把所有已注册的默认熔断器重置为初始 Closed 状态。"""
    for cb in _DEFAULT_BREAKERS.values():
        cb.reset()


DEFAULT_TOKEN_BUDGET: TokenBudget = TokenBudget()


def reset_default_token_budget() -> None:
    """测试隔离：把默认 token 预算清零。"""
    with DEFAULT_TOKEN_BUDGET._lock:
        DEFAULT_TOKEN_BUDGET._day = None
        DEFAULT_TOKEN_BUDGET._used = 0

# import 循环保护（errors 最后一行 import 了 json，我们这里用到 DevFlowError 不涉及递归）
from .errors import NodeContextError  # noqa: E402,F401
