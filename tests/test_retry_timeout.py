"""RetryPolicy.timeout_per_attempt（单次调用限时）测试。

背景：慢模型/流式拖尾会把总 deadline 一次吃光——表现为 check-llm（20s 单发探针）
通过、真实调用却 DEADLINE.EXCEEDED 全走 Mock。单次限时保证快速失败并轮换。
"""
from __future__ import annotations

import asyncio
import time

import pytest

from devflow.errors import CliTimeoutError, RetryPolicy
from devflow.resilience import retry_with_backoff


@pytest.mark.asyncio
async def test_per_attempt_timeout_fails_fast_and_rotates():
    """第 1 次尝试卡死 → 单次超时快速失败 → 第 2 次成功；总耗时不受卡死时长拖累。"""
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            await asyncio.sleep(5)  # 远超单次上限
        return "ok"

    pol = RetryPolicy(max_attempts=2, base_backoff=0.01, timeout_per_attempt=0.3,
                      deadline_total=30.0)
    t0 = time.monotonic()
    out = await retry_with_backoff(pol)(flaky)()
    elapsed = time.monotonic() - t0
    assert out == "ok"
    assert calls["n"] == 2
    assert elapsed < 2, f"单次超时应快速失败轮换，实际耗时 {elapsed:.1f}s"


@pytest.mark.asyncio
async def test_all_attempts_timeout_raises_timeout_error():
    """每次尝试都超时 → 抛最后一次的分类错误（CliTimeoutError，可重试类）。"""

    async def slow():
        await asyncio.sleep(3)

    pol = RetryPolicy(max_attempts=2, base_backoff=0.01, timeout_per_attempt=0.2,
                      deadline_total=30.0)
    with pytest.raises(CliTimeoutError):
        await retry_with_backoff(pol)(slow)()


@pytest.mark.asyncio
async def test_deadline_still_caps_total_time():
    """deadline_total 仍兜底：即使单次超时更长，总时长也不会超过 deadline。"""

    async def slow():
        await asyncio.sleep(10)

    pol = RetryPolicy(max_attempts=3, base_backoff=0.01, timeout_per_attempt=5.0,
                      deadline_total=0.6)
    t0 = time.monotonic()
    with pytest.raises(Exception):
        await retry_with_backoff(pol)(slow)()
    assert time.monotonic() - t0 < 3


# ═══════════════════════════════════════════════════════════════════
# 0 / None = 不限时（LLM_TIMEOUT_PER_ATTEMPT_SEC=0 的底层语义）
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("tout", [None, 0])
async def test_timeout_unset_or_zero_means_unlimited(tout):
    """timeout_per_attempt 为 None 或 0：慢调用完整跑完，不被 wait_for 掐断。

    （0 曾被 min(0, ...) 归一成 wait_for(0) 立即超时——0 必须视为不限时。）
    """
    calls = {"n": 0}

    async def slow_but_finishes():
        calls["n"] += 1
        await asyncio.sleep(0.4)
        return "ok"

    pol = RetryPolicy(max_attempts=2, base_backoff=0.01,
                      timeout_per_attempt=tout, deadline_total=None)
    t0 = time.monotonic()
    assert await retry_with_backoff(pol)(slow_but_finishes)() == "ok"
    assert time.monotonic() - t0 >= 0.4
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_per_attempt_timeout_works_without_deadline():
    """只设单次限时、不设 deadline：单次限时独立生效（不再被静默跳过）。"""

    async def slow():
        await asyncio.sleep(3)

    pol = RetryPolicy(max_attempts=2, base_backoff=0.01,
                      timeout_per_attempt=0.2, deadline_total=None)
    t0 = time.monotonic()
    with pytest.raises(CliTimeoutError):
        await retry_with_backoff(pol)(slow)()
    assert time.monotonic() - t0 < 2
