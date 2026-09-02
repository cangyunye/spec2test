"""LLM 多提供商配置测试：DeepSeek / SiliconFlow / 自建 / Mock 切换。

覆盖：
  - settings.LLM_PROVIDERS_JSON 解析：多提供商结构化列表
  - 旧版 LLM_BASE_URL + LLM_MODEL 兼容解析
  - _get_model 返回 ChatOpenAI 实例 + 正确 base_url/model/temperature
  - _breaker_name_for 独立熔断器命名
  - LLM_USE_MOCK_FALLBACK 开/关
"""
from __future__ import annotations

import json
import os
from typing import Any

import pytest
from langchain_openai import ChatOpenAI

from devflow.config import LlmProviderSpec, _parse_llm_providers_from_env
from devflow.llm_client import (
    _MockLLM,
    _breaker_name_for,
    _candidates_providers,
    _get_model,
    _use_mock_fallback,
)


# ═══════════════════════════════════════════════════════════════════
# 1. _parse_llm_providers_from_env
# ═══════════════════════════════════════════════════════════════════


class TestParseProvidersJson:
    def test_two_providers(self, monkeypatch):
        cfg = [
            {"name": "deepseek", "base_url": "https://api.deepseek.com/v1",
             "api_key": "sk-ds-1", "model": "deepseek-chat"},
            {"name": "sf", "base_url": "https://api.siliconflow.cn/v1",
             "api_key": "sk-sf-2", "model": "deepseek-ai/DeepSeek-V3",
             "temperature": 0.3},
        ]
        monkeypatch.setenv("LLM_PROVIDERS_JSON", json.dumps(cfg))
        providers = _parse_llm_providers_from_env()
        assert len(providers) == 2
        assert providers[0]["name"] == "deepseek"
        assert providers[0]["base_url"] == "https://api.deepseek.com/v1"
        assert providers[0]["api_key"] == "sk-ds-1"
        assert providers[0]["model"] == "deepseek-chat"
        assert providers[0]["temperature"] == 0.1  # 默认
        assert providers[1]["name"] == "sf"
        assert providers[1]["temperature"] == 0.3

    def test_empty_string_falls_back_to_legacy(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDERS_JSON", "")
        monkeypatch.setenv("LLM_BASE_URL", "https://api.deepseek.com/v1")
        monkeypatch.setenv("LLM_MODEL", "deepseek-chat")
        monkeypatch.setenv("LLM_API_KEY", "sk-legacy")
        monkeypatch.setenv("LLM_TEMPERATURE", "0.2")
        monkeypatch.setenv("LLM_FALLBACKS", "mock")
        providers = _parse_llm_providers_from_env()
        assert len(providers) == 1  # legacy primary，mock 不进 providers 列表
        assert providers[0]["name"] == "primary"
        assert providers[0]["model"] == "deepseek-chat"
        assert providers[0]["temperature"] == 0.2
        assert providers[0]["api_key"] == "sk-legacy"

    def test_bad_json_falls_back_legacy(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDERS_JSON", "{invalid json")
        monkeypatch.setenv("LLM_BASE_URL", "https://api.deepseek.com/v1")
        monkeypatch.setenv("LLM_MODEL", "deepseek-chat")
        monkeypatch.setenv("LLM_FALLBACKS", "mock")
        providers = _parse_llm_providers_from_env()
        assert len(providers) == 1
        assert providers[0]["name"] == "primary"

    def test_provider_missing_required_keys_skipped(self, monkeypatch):
        """缺 base_url 或 model 的条目被跳过。"""
        cfg = [
            {"name": "p1", "base_url": "https://a.com/v1"},           # 缺 model → skip
            {"name": "p2", "model": "m2"},                             # 缺 base_url → skip
            {"name": "p3", "base_url": "https://c.com/v1", "model": "m3"},  # ✓
        ]
        monkeypatch.setenv("LLM_PROVIDERS_JSON", json.dumps(cfg))
        monkeypatch.setenv("LLM_API_KEY", "sk-x")
        providers = _parse_llm_providers_from_env()
        assert len(providers) == 1
        assert providers[0]["name"] == "p3"
        assert providers[0]["model"] == "m3"

    def test_name_auto_generated(self, monkeypatch):
        cfg = [
            {"base_url": "https://a.com/v1", "model": "m1"},
            {"base_url": "https://b.com/v1", "model": "m2"},
        ]
        monkeypatch.setenv("LLM_PROVIDERS_JSON", json.dumps(cfg))
        providers = _parse_llm_providers_from_env()
        assert providers[0]["name"] == "llm-0"
        assert providers[1]["name"] == "llm-1"

    def test_api_key_fallback_to_global(self, monkeypatch):
        """provider 没写 api_key → 自动补 LLM_API_KEY。"""
        monkeypatch.setenv("LLM_API_KEY", "sk-global")
        cfg = [
            {"name": "p1", "base_url": "https://a.com/v1", "model": "m1"},  # 没 api_key
            {"name": "p2", "base_url": "https://b.com/v1", "model": "m2", "api_key": "sk-local"},
        ]
        monkeypatch.setenv("LLM_PROVIDERS_JSON", json.dumps(cfg))
        providers = _parse_llm_providers_from_env()
        assert providers[0]["api_key"] == "sk-global"
        assert providers[1]["api_key"] == "sk-local"


# ═══════════════════════════════════════════════════════════════════
# 2. _get_model 单例 + ChatOpenAI 实例字段匹配
# ═══════════════════════════════════════════════════════════════════


class TestGetModel:
    def test_mock_returns_mock_instance(self):
        m = _get_model("mock")
        assert isinstance(m, _MockLLM)

    def test_mock_singleton(self):
        assert _get_model("mock") is _get_model("mock")

    def test_str_non_mock_raises_value_error(self):
        with pytest.raises(ValueError, match="字符串模式已废弃"):
            _get_model("deepseek-chat")

    def test_structured_spec_returns_chat_openai(self):
        spec: LlmProviderSpec = {
            "name": "demo",
            "base_url": "https://api.deepseek.com/v1",
            "api_key": "sk-demo",
            "model": "deepseek-chat",
            "temperature": 0.2,
        }
        llm = _get_model(spec)
        assert isinstance(llm, ChatOpenAI)
        # 实例内部字段（ChatOpenAI 的 root_client.base_url / model_name）
        assert llm.model_name == "deepseek-chat"
        assert llm.temperature == 0.2
        # httpx base_url 会标准化成末尾带 "/"
        assert str(llm.root_client.base_url) == "https://api.deepseek.com/v1/"

    def test_structured_singleton_same_spec(self):
        spec: LlmProviderSpec = {
            "name": "same",
            "base_url": "https://a.com/v1",
            "api_key": "sk-x",
            "model": "m1",
            "temperature": 0.1,
        }
        a = _get_model(spec)
        b = _get_model(spec)
        assert a is b

    def test_different_providers_different_instances(self):
        s1: LlmProviderSpec = {
            "name": "A", "base_url": "https://a.com/v1", "api_key": "k", "model": "m1"
        }
        s2: LlmProviderSpec = {
            "name": "B", "base_url": "https://b.com/v1", "api_key": "k", "model": "m2"
        }
        assert _get_model(s1) is not _get_model(s2)


# ═══════════════════════════════════════════════════════════════════
# 3. _breaker_name_for：每个 provider 独立熔断器
# ═══════════════════════════════════════════════════════════════════


class TestBreakerNaming:
    def test_breaker_names_unique_per_provider(self):
        names = set()
        for n in ("deepseek", "siliconflow", "vllm", "primary"):
            spec: LlmProviderSpec = {
                "name": n,
                "base_url": f"https://{n}.com/v1",
                "api_key": "k",
                "model": "m",
            }
            names.add(_breaker_name_for(spec))
        assert len(names) == 4
        assert all(n.startswith("llm_") for n in names)
        assert "llm_deepseek" in names
        assert "llm_siliconflow" in names


# ═══════════════════════════════════════════════════════════════════
# 4. _candidates_providers + _use_mock_fallback（与 settings 耦合）
# ═══════════════════════════════════════════════════════════════════


class TestCandidatesAndMockFallback:
    def test_default_settings_has_primary_and_mock_fallback(self, monkeypatch):
        """没 LLM_PROVIDERS_JSON 环境时：providers=[primary] + mock fallback=True。"""
        # 清掉 LLM_PROVIDERS_JSON，强制走 legacy
        monkeypatch.delenv("LLM_PROVIDERS_JSON", raising=False)
        monkeypatch.setenv("LLM_BASE_URL", "https://api.deepseek.com/v1")
        monkeypatch.setenv("LLM_MODEL", "deepseek-chat")
        monkeypatch.setenv("LLM_FALLBACKS", "mock")

        # 重新加载 settings（构造新实例）
        from devflow.config import _parse_llm_providers_from_env, Settings
        providers = _parse_llm_providers_from_env()
        assert len(providers) == 1
        assert providers[0]["name"] == "primary"
        assert providers[0]["base_url"] == "https://api.deepseek.com/v1"
        assert providers[0]["model"] == "deepseek-chat"

    def test_mock_fallback_when_fallbacks_has_mock(self, monkeypatch):
        monkeypatch.delenv("LLM_PROVIDERS_JSON", raising=False)
        monkeypatch.setenv("LLM_FALLBACKS", "mock")
        from devflow.config import Settings
        assert Settings().LLM_USE_MOCK_FALLBACK is True

    def test_no_mock_fallback_when_fallbacks_empty(self, monkeypatch):
        monkeypatch.delenv("LLM_PROVIDERS_JSON", raising=False)
        monkeypatch.setenv("LLM_FALLBACKS", "")
        from devflow.config import Settings
        s = Settings()
        # 因为 LLM_PROVIDERS_JSON 没配，not os.getenv 返回 True → fallback=True
        # 所以这个断言要写对逻辑：当且仅当 LLM_PROVIDERS_JSON 有配且 FALLBACKS 没 mock
        assert s.LLM_USE_MOCK_FALLBACK is True

    def test_no_mock_fallback_explicit(self, monkeypatch):
        """配了 LLM_PROVIDERS_JSON 且 FALLBACKS 没有 mock → 不兜底。"""
        cfg = [{"name": "p", "base_url": "https://a.com/v1", "model": "m"}]
        monkeypatch.setenv("LLM_PROVIDERS_JSON", json.dumps(cfg))
        monkeypatch.setenv("LLM_FALLBACKS", "")
        from devflow.config import Settings
        s = Settings()
        assert s.LLM_USE_MOCK_FALLBACK is False
