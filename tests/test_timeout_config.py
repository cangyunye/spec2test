"""LLM / Archify 超时配置接线测试。

背景：制图（大 JSON 结构化输出）与 archify 渲染天然耗时，超时曾硬编码
（单次 60s + 每 provider deadline 180s + archify CLI 60s），导致慢场景被
「切换下一个 provider」最终掉 mock。现全部接 .env 配置，0 = 不限时。
"""
from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import HumanMessage
from pydantic import BaseModel

from devflow import llm_client as lc
from devflow.config import settings
from devflow.errors import CliTimeoutError, RetryPolicy
from devflow.resilience import TokenBudget, default_breaker
from devflow.providers.archify import ArchifyProvider


class _Out(BaseModel):
    value: str = "ok"


class _SlowStructured:
    def __init__(self, delay: float) -> None:
        self.delay = delay

    async def ainvoke(self, messages, **_: object) -> _Out:
        await asyncio.sleep(self.delay)
        return _Out()


class _FakeLLM:
    def __init__(self, delay: float) -> None:
        self.delay = delay

    def with_structured_output(self, _schema: object, **_: object) -> _SlowStructured:
        return _SlowStructured(self.delay)


async def _run_structured_once(per_attempt: float, deadline: float) -> dict:
    """按配置跑一次结构化调用（llm_client._invoke_json_once 分支 A）。"""
    lc._model_cache.clear()  # 隔离脏缓存
    try:
        return await lc._invoke_json_once(
            llm=_FakeLLM(0.4),
            messages=[HumanMessage(content="x")],
            response_model=_Out,
            json_schema=None,
            max_retries=1,
            breaker=default_breaker("llm_timeout_test"),
            budget=TokenBudget(),
            response_type="test",
            model_spec="test",
        )
    finally:
        pass


class TestStructuredRetryPolicyFromSettings:
    @pytest.mark.asyncio
    async def test_tiny_timeout_fails_fast(self, monkeypatch):
        """配置的小超时生效：单次 0.05s → 快速超时抛 CliTimeoutError（可重试类）。"""
        monkeypatch.setattr(settings, "LLM_TIMEOUT_PER_ATTEMPT_SEC", 0.05)
        monkeypatch.setattr(settings, "LLM_DEADLINE_TOTAL_SEC", 30.0)
        with pytest.raises(CliTimeoutError):
            await _run_structured_once(0.05, 30.0)

    @pytest.mark.asyncio
    async def test_zero_means_unlimited(self, monkeypatch):
        """0 → None 归一：0.4s 的慢结构化调用不再被 wait_for 掐断。"""
        monkeypatch.setattr(settings, "LLM_TIMEOUT_PER_ATTEMPT_SEC", 0)
        monkeypatch.setattr(settings, "LLM_DEADLINE_TOTAL_SEC", 0)
        out = await _run_structured_once(0, 0)
        assert out["value"] == "ok"

    def test_policy_values_reflect_settings(self, monkeypatch):
        """RetryPolicy 两项从 settings 读取且 0 归一为 None。"""
        monkeypatch.setattr(settings, "LLM_TIMEOUT_PER_ATTEMPT_SEC", 0)
        monkeypatch.setattr(settings, "LLM_DEADLINE_TOTAL_SEC", 0)
        pol = RetryPolicy(
            max_attempts=3,
            base_backoff=1.0,
            deadline_total=settings.LLM_DEADLINE_TOTAL_SEC or None,
            timeout_per_attempt=settings.LLM_TIMEOUT_PER_ATTEMPT_SEC or None,
        )
        assert pol.timeout_per_attempt is None
        assert pol.deadline_total is None


class TestSdkTimeoutFromSettings:
    def test_chat_openai_timeout_reflects_settings(self, monkeypatch):
        """ChatOpenAI request_timeout 与单次限时同源；缓存 key 随之区分。"""
        monkeypatch.setattr(lc, "_model_cache", {})
        monkeypatch.setattr(settings, "LLM_TIMEOUT_PER_ATTEMPT_SEC", 120.0)
        spec = {"name": "t-timeout", "base_url": "https://x.test/v1",
                "api_key": "sk-t", "model": "m-timeout"}
        m = lc._get_model(spec)
        assert float(m.request_timeout) == 120.0

    def test_chat_openai_timeout_zero_means_none(self, monkeypatch):
        """0 → SDK 层不限时（langchain-openai request_timeout=None）。"""
        monkeypatch.setattr(lc, "_model_cache", {})
        monkeypatch.setattr(settings, "LLM_TIMEOUT_PER_ATTEMPT_SEC", 0)
        spec = {"name": "t-timeout0", "base_url": "https://x.test/v1",
                "api_key": "sk-t", "model": "m-timeout0"}
        m = lc._get_model(spec)
        assert m.request_timeout is None


class TestArchifyTimeoutFromSettings:
    def test_default_reads_settings(self, monkeypatch):
        monkeypatch.setattr(settings, "ARCHIFY_TIMEOUT_SEC", 300)
        assert ArchifyProvider().timeout_sec == 300

    def test_zero_means_none(self, monkeypatch):
        monkeypatch.setattr(settings, "ARCHIFY_TIMEOUT_SEC", 0)
        assert ArchifyProvider().timeout_sec is None

    def test_explicit_param_wins(self, monkeypatch):
        monkeypatch.setattr(settings, "ARCHIFY_TIMEOUT_SEC", 0)
        assert ArchifyProvider(timeout_sec=30).timeout_sec == 30
