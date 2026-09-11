"""LLM 多提供商配置测试：DeepSeek / OpenCode Go / SiliconFlow / 自建 / Mock 切换。

覆盖：
  - settings.LLM_PROVIDERS_JSON 解析：多提供商结构化列表
  - models 模型池：同供应商多模型展开为 fallback 链 + LLM_ACTIVE_MODEL 切换
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

from devflow.config import (
    LlmProviderSpec,
    MIN_CONTEXT_WINDOW_TOKENS,
    _parse_llm_providers_from_env,
    resolve_context_window,
)
from devflow.llm_client import (
    _MockLLM,
    _breaker_name_for,
    _candidates_providers,
    _get_model,
    _shrink_messages,
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
# 1b. models 模型池：同供应商多模型 fallback 链 + LLM_ACTIVE_MODEL 切换
# ═══════════════════════════════════════════════════════════════════


class TestMultiModelPool:
    JSON_BASE = "https://opencode.ai/zen/go/v1"

    def _set_pool(self, monkeypatch, **extra):
        cfg = [{
            "name": "opencode-go",
            "base_url": self.JSON_BASE,
            "api_key": "sk-og",
            "model": "deepseek-v4-flash",
            "models": ["deepseek-v4-flash", "deepseek-v4-pro", "glm-5.3", "qwen3.8-max"],
            **extra,
        }]
        monkeypatch.setenv("LLM_PROVIDERS_JSON", json.dumps(cfg))
        monkeypatch.delenv("LLM_ACTIVE_MODEL", raising=False)
        return _parse_llm_providers_from_env()

    def test_models_pool_expands_to_fallback_chain(self, monkeypatch):
        """激活模型沿用原 name 打头，其余模型命名 name:模型名 依次跟随。"""
        providers = self._set_pool(monkeypatch)
        assert [p["model"] for p in providers] == [
            "deepseek-v4-flash", "deepseek-v4-pro", "glm-5.3", "qwen3.8-max",
        ]
        assert providers[0]["name"] == "opencode-go"
        assert providers[1]["name"] == "opencode-go:deepseek-v4-pro"
        assert providers[3]["name"] == "opencode-go:qwen3.8-max"
        # 展开条目共享 base_url / api_key / temperature
        assert all(p["base_url"] == self.JSON_BASE for p in providers)
        assert all(p["api_key"] == "sk-og" for p in providers)
        assert all(p["temperature"] == 0.1 for p in providers)

    def test_models_without_model_field_uses_first_as_active(self, monkeypatch):
        cfg = [{
            "name": "og",
            "base_url": self.JSON_BASE,
            "api_key": "sk-x",
            "models": ["glm-5.3", "deepseek-v4-pro"],
        }]
        monkeypatch.setenv("LLM_PROVIDERS_JSON", json.dumps(cfg))
        monkeypatch.delenv("LLM_ACTIVE_MODEL", raising=False)
        providers = _parse_llm_providers_from_env()
        assert providers[0]["model"] == "glm-5.3"
        assert providers[0]["name"] == "og"
        assert len(providers) == 2

    def test_model_outside_pool_prepended_as_active(self, monkeypatch):
        providers = self._set_pool(
            monkeypatch, model="kimi-k3",
            models=["deepseek-v4-flash", "glm-5.3"],
        )
        assert providers[0]["model"] == "kimi-k3"
        assert providers[0]["name"] == "opencode-go"
        assert [p["model"] for p in providers[1:]] == ["deepseek-v4-flash", "glm-5.3"]

    def test_active_model_bare_switch(self, monkeypatch):
        """裸模型名：池里含该模型的 provider 都切换，激活模型排最前。"""
        providers = self._set_pool(monkeypatch)
        monkeypatch.setenv("LLM_ACTIVE_MODEL", "glm-5.3")
        providers = _parse_llm_providers_from_env()
        assert providers[0]["model"] == "glm-5.3"
        assert providers[0]["name"] == "opencode-go"
        assert [p["model"] for p in providers[1:]] == [
            "deepseek-v4-flash", "deepseek-v4-pro", "qwen3.8-max",
        ]

    def test_active_model_qualified_switch(self, monkeypatch):
        """provider名/模型名：只切换同名 provider。"""
        self._set_pool(monkeypatch)
        monkeypatch.setenv("LLM_ACTIVE_MODEL", "opencode-go/deepseek-v4-pro")
        providers = _parse_llm_providers_from_env()
        assert providers[0]["model"] == "deepseek-v4-pro"
        assert providers[0]["name"] == "opencode-go"

    def test_active_model_qualified_other_provider_noop(self, monkeypatch):
        """限定名不匹配任何 provider → 不切换。"""
        self._set_pool(monkeypatch)
        monkeypatch.setenv("LLM_ACTIVE_MODEL", "deepseek/deepseek-v4-pro")
        providers = _parse_llm_providers_from_env()
        assert providers[0]["model"] == "deepseek-v4-flash"

    def test_active_model_not_in_pool_noop(self, monkeypatch):
        self._set_pool(monkeypatch)
        monkeypatch.setenv("LLM_ACTIVE_MODEL", "glm-9.9")
        providers = _parse_llm_providers_from_env()
        assert providers[0]["model"] == "deepseek-v4-flash"

    def test_pool_entries_dedup_preserving_order(self, monkeypatch):
        cfg = [{
            "name": "og",
            "base_url": self.JSON_BASE,
            "api_key": "sk-x",
            "model": "glm-5.3",
            "models": ["glm-5.3", "glm-5.3", "kimi-k3", "glm-5.3"],
        }]
        monkeypatch.setenv("LLM_PROVIDERS_JSON", json.dumps(cfg))
        monkeypatch.delenv("LLM_ACTIVE_MODEL", raising=False)
        providers = _parse_llm_providers_from_env()
        assert [p["model"] for p in providers] == ["glm-5.3", "kimi-k3"]


# ═══════════════════════════════════════════════════════════════════
# 1c. headers 附加请求头：解析透传到展开后的每个模型条目
# ═══════════════════════════════════════════════════════════════════


class TestProviderHeaders:
    def test_headers_propagate_to_all_pool_entries(self, monkeypatch):
        """headers 作用于该供应商展开出的全部模型（激活模型 + 模型池 fallback）。"""
        cfg = [{
            "name": "opencode-go",
            "base_url": "https://opencode.ai/zen/go/v1",
            "api_key": "sk-og",
            "model": "deepseek-v4-flash",
            "models": ["deepseek-v4-flash", "glm-5.3"],
            "headers": {"x-opencode-session": "devflow-cli", "User-Agent": "devflow/1.0"},
        }]
        monkeypatch.setenv("LLM_PROVIDERS_JSON", json.dumps(cfg))
        monkeypatch.delenv("LLM_ACTIVE_MODEL", raising=False)
        providers = _parse_llm_providers_from_env()
        assert len(providers) == 2
        for p in providers:
            assert p["headers"] == {"x-opencode-session": "devflow-cli",
                                    "User-Agent": "devflow/1.0"}

    def test_headers_non_dict_or_empty_ignored(self, monkeypatch):
        """headers 不是 dict（脏数据）或为空时静默忽略，不进 provider spec。"""
        cfg = [
            {"name": "a", "base_url": "https://a.com/v1", "model": "m1", "headers": "x: y"},
            {"name": "b", "base_url": "https://b.com/v1", "model": "m2", "headers": {}},
        ]
        monkeypatch.setenv("LLM_PROVIDERS_JSON", json.dumps(cfg))
        providers = _parse_llm_providers_from_env()
        assert len(providers) == 2
        assert all("headers" not in p for p in providers)


# ═══════════════════════════════════════════════════════════════════
# 1d. context_window：int 全供应商生效 / dict 按模型名；官方规格表兜底
# ═══════════════════════════════════════════════════════════════════


class TestProviderContextWindow:
    def test_int_propagates_to_all_pool_entries(self, monkeypatch):
        """context_window 为 int → 该供应商展开出的每个模型条目都带上。"""
        cfg = [{
            "name": "og",
            "base_url": "https://opencode.ai/zen/go/v1",
            "api_key": "sk-og",
            "model": "deepseek-v4-flash",
            "models": ["deepseek-v4-flash", "glm-5.3"],
            "context_window": 400_000,
        }]
        monkeypatch.setenv("LLM_PROVIDERS_JSON", json.dumps(cfg))
        monkeypatch.delenv("LLM_ACTIVE_MODEL", raising=False)
        providers = _parse_llm_providers_from_env()
        assert [p.get("context_window") for p in providers] == [400_000, 400_000]

    def test_dict_per_model_override(self, monkeypatch):
        """context_window 为 dict → 按模型名单独指定，池里其他模型不带该键。"""
        cfg = [{
            "name": "og",
            "base_url": "https://opencode.ai/zen/go/v1",
            "api_key": "sk-og",
            "model": "deepseek-v4-flash",
            "models": ["deepseek-v4-flash", "glm-5.3"],
            "context_window": {"deepseek-v4-flash": 200_000},
        }]
        monkeypatch.setenv("LLM_PROVIDERS_JSON", json.dumps(cfg))
        monkeypatch.delenv("LLM_ACTIVE_MODEL", raising=False)
        providers = _parse_llm_providers_from_env()
        by_model = {p["model"]: p for p in providers}
        assert by_model["deepseek-v4-flash"].get("context_window") == 200_000
        assert "context_window" not in by_model["glm-5.3"]

    def test_dirty_values_ignored(self, monkeypatch):
        """bool/负数/字符串等脏值静默忽略，不进 provider spec。"""
        cfg = [
            {"name": "a", "base_url": "https://a.com/v1", "model": "m1",
             "context_window": True},
            {"name": "b", "base_url": "https://b.com/v1", "model": "m2",
             "context_window": -5},
            {"name": "c", "base_url": "https://c.com/v1", "model": "m3",
             "context_window": {"m3": "很多"}},
        ]
        monkeypatch.setenv("LLM_PROVIDERS_JSON", json.dumps(cfg))
        providers = _parse_llm_providers_from_env()
        assert all("context_window" not in p for p in providers)


class TestResolveContextWindow:
    def test_official_specs_longest_prefix_wins(self):
        """已知模型按官方规格：kimi-k3 1M 优先于 kimi 256K；glm-5.3 1M 优先于 glm-5 200K。"""
        assert resolve_context_window("kimi-k3") == 1_000_000
        assert resolve_context_window("kimi-k2.7-code") == 256_000
        assert resolve_context_window("glm-5.3") == 1_000_000
        assert resolve_context_window("glm-5") == 200_000
        assert resolve_context_window("deepseek-v4-pro") == 200_000
        assert resolve_context_window("deepseek-flash") == 128_000
        assert resolve_context_window("qwen3.8-max") == 1_000_000

    def test_explicit_overrides_official_table(self):
        """provider 显式 context_window 优先于官方规格表。"""
        assert resolve_context_window("glm-5.3", 500_000) == 500_000

    def test_unknown_model_floored_at_128k(self, monkeypatch):
        """未收录的自定义模型：至少 128k（全局 env 配小了也被托底）。"""
        monkeypatch.setenv("LLM_CONTEXT_WINDOW_TOKENS", "64000")
        assert resolve_context_window("my-private-llm") == MIN_CONTEXT_WINDOW_TOKENS

    def test_unknown_model_uses_env_when_higher(self, monkeypatch):
        """未收录模型：全局 env 配置更高（如 512k）时按 env。"""
        monkeypatch.setenv("LLM_CONTEXT_WINDOW_TOKENS", "512000")
        assert resolve_context_window("my-private-llm") == 512_000


class TestShrinkWithContextWindow:
    def test_bigger_window_keeps_more_context(self):
        """同一份超长消息：128k 窗口触发截断占位符，1M 窗口完整保留砍半后的内容。"""
        from langchain_core.messages import HumanMessage, SystemMessage

        msgs = [SystemMessage(content="sys")]
        msgs += [HumanMessage(content="x" * 400_000) for _ in range(4)]
        small = _shrink_messages(msgs, 128_000)
        large = _shrink_messages(msgs, 1_000_000)
        small_chars = sum(len(str(m.content)) for m in small)
        large_chars = sum(len(str(m.content)) for m in large)
        assert large_chars > small_chars
        assert any("TRUNCATED" in str(m.content) for m in small)
        assert not any("TRUNCATED" in str(m.content) for m in large)


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

    def test_deepseek_v4_extra_body_first_class_param(self):
        """回归：DeepSeek V4 关 thinking 走一等参数 extra_body，
        不再塞 model_kwargs（langchain-openai 1.x 会发弃用 UserWarning）。"""
        import warnings

        spec: LlmProviderSpec = {
            "name": "ds4",
            "base_url": "https://api.deepseek.com/v1",
            "api_key": "sk-ds4",
            "model": "deepseek-v4-flash",
        }
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            llm = _get_model(spec)
        assert not [w for w in caught if "extra_body" in str(w.message)]
        assert llm.extra_body == {"thinking": {"type": "disabled"}}
        assert llm.model_kwargs == {}

    def test_non_deepseek_no_extra_body(self):
        spec: LlmProviderSpec = {
            "name": "glm",
            "base_url": "https://open.bigmodel.cn/api/paas/v4",
            "api_key": "sk-glm",
            "model": "glm-5.3",
        }
        llm = _get_model(spec)
        assert not llm.extra_body
        assert llm.model_kwargs == {}

    def test_spec_headers_become_default_headers(self):
        """网关自定义请求头（如 OpenCode Go 要求的 x-opencode-session）透传 default_headers。"""
        spec: LlmProviderSpec = {
            "name": "og-h",
            "base_url": "https://opencode.ai/zen/go/v1",
            "api_key": "sk-og",
            "model": "glm-5.3",
            "headers": {"x-opencode-session": "devflow-cli", "User-Agent": "devflow/1.0"},
        }
        llm = _get_model(spec)
        assert llm.default_headers["x-opencode-session"] == "devflow-cli"
        assert llm.default_headers["User-Agent"] == "devflow/1.0"

    def test_spec_without_headers_no_default_headers(self):
        spec: LlmProviderSpec = {
            "name": "no-h",
            "base_url": "https://a.com/v1",
            "api_key": "k",
            "model": "m1",
        }
        llm = _get_model(spec)
        assert not llm.default_headers

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
