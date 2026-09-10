"""供应商面板 API（/api/providers*）与 provider 可见化事件测试。

覆盖：
  - 兜底链读取 / 运行期改首选（settings 进程内重排，不改 .env）
  - 连通性检测端点（monkeypatch，不打真实 API）
  - events 层对 langgraph custom 流事件的解码（provider_skip / provider_used）
  - llm_client 在无 runtime 上下文时发事件静默跳过
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from devflow.events import _events_for_mode
from devflow.llm_client import _emit_stream_event


def _set_chain(monkeypatch, chain: list[dict]) -> None:
    from devflow.config import settings

    monkeypatch.setattr(settings, "LLM_PROVIDERS", chain, raising=False)


def _chain() -> list[dict]:
    from devflow.config import settings

    return settings.LLM_PROVIDERS


class TestProviderChainApi:
    def test_list_providers_returns_ordered_chain(self, monkeypatch):
        from web.server import list_providers

        _set_chain(monkeypatch, [
            {"name": "a", "model": "m-a", "base_url": "http://a/v1", "api_key": "k"},
            {"name": "b", "model": "m-b", "base_url": "http://b/v1", "api_key": "k",
             "models": ["m-b", "m-b2"]},
        ])
        out = list_providers()
        assert [c["name"] for c in out["chain"]] == ["a", "b"]
        assert out["chain"][1]["models"] == ["m-b", "m-b2"]
        assert out["mock_fallback"] in (True, False)

    def test_set_active_moves_entry_to_front(self, monkeypatch):
        from web.server import set_active_provider

        _set_chain(monkeypatch, [
            {"name": "a", "model": "m-a", "base_url": "u", "api_key": "k"},
            {"name": "b", "model": "m-b", "base_url": "u", "api_key": "k"},
        ])
        out = set_active_provider(type("B", (), {"name": "b", "model": "m-b"})())
        assert [c["name"] for c in out["chain"]] == ["b", "a"]
        assert _chain()[0]["model"] == "m-b"
        assert _chain()[1]["model"] == "m-a"  # 原链顺序保留，仅重排

    def test_set_active_unknown_name_raises_404(self, monkeypatch):
        from fastapi import HTTPException

        from web.server import set_active_provider

        _set_chain(monkeypatch, [{"name": "a", "model": "m-a", "base_url": "u", "api_key": "k"}])
        with pytest.raises(HTTPException) as ei:
            set_active_provider(type("B", (), {"name": "ghost", "model": None})())
        assert ei.value.status_code == 404

    def test_set_active_matches_name_and_model(self, monkeypatch):
        """模型池展开后的同供应商多模型条目：name 相同时按 model 精确匹配。"""
        from web.server import set_active_provider

        _set_chain(monkeypatch, [
            {"name": "b:m1", "model": "m1", "base_url": "u", "api_key": "k"},
            {"name": "b:m2", "model": "m2", "base_url": "u", "api_key": "k"},
        ])
        set_active_provider(type("B", (), {"name": "b:m2", "model": "m2"})())
        assert _chain()[0]["name"] == "b:m2"


class TestCheckProvidersApi:
    @pytest.mark.asyncio
    async def test_check_single_provider_uses_llm_client(self, monkeypatch):
        import web.server as srv

        captured: dict = {}

        async def fake_check(spec, *, timeout_sec=20):
            captured["spec"] = spec
            captured["timeout_sec"] = timeout_sec
            return {"name": spec["name"], "ok": True, "elapsed_ms": 5}

        monkeypatch.setattr("devflow.llm_client.check_llm_provider", fake_check)
        _set_chain(monkeypatch, [{"name": "a", "model": "m-a", "base_url": "u", "api_key": "k"}])
        out = await srv.check_providers(type("B", (), {"name": "a"})())
        assert out["reports"][0]["ok"] is True
        assert captured["spec"]["name"] == "a"
        assert captured["timeout_sec"] == 12  # 面板检测用短超时

    @pytest.mark.asyncio
    async def test_check_all_without_name(self, monkeypatch):
        import web.server as srv

        async def fake_all():
            return [{"name": "a", "ok": False}]

        monkeypatch.setattr("devflow.llm_client.check_llm_all", fake_all)
        out = await srv.check_providers(None)
        assert out["reports"] == [{"name": "a", "ok": False}]

    @pytest.mark.asyncio
    async def test_check_unknown_name_raises_404(self, monkeypatch):
        from fastapi import HTTPException

        import web.server as srv

        _set_chain(monkeypatch, [])
        with pytest.raises(HTTPException) as ei:
            await srv.check_providers(type("B", (), {"name": "ghost"})())
        assert ei.value.status_code == 404


class TestProviderStreamEvents:
    def test_custom_mode_decodes_skip_and_used(self):
        skip = _events_for_mode("custom", {
            "type": "provider_skip", "provider": "deepseek",
            "code": "LLM.UPSTREAM", "message": "400", "ctx": "",
        })
        used = _events_for_mode("custom", {"type": "provider_used", "provider": "deepseek",
                                           "model": "deepseek-v4-flash"})
        assert list(skip) == [{"type": "provider", "status": "skip",
                               "provider": "deepseek", "code": "LLM.UPSTREAM",
                               "message": "400", "ctx": ""}]
        used_ev = list(used)[0]
        assert used_ev["status"] == "used"
        assert used_ev["model"] == "deepseek-v4-flash"

    def test_custom_mode_ignores_unrelated_payloads(self):
        assert list(_events_for_mode("custom", {"foo": 1})) == []
        assert list(_events_for_mode("custom", "text")) == []

    def test_custom_mode_decodes_disabled(self):
        ev = list(_events_for_mode("custom", {
            "type": "provider_disabled", "provider": "a", "reason": "401 Invalid API key",
        }))
        assert ev == [{"type": "provider", "status": "disabled",
                       "provider": "a", "reason": "401 Invalid API key"}]

    def test_emit_outside_runtime_is_silent_noop(self):
        # 无 LangGraph runtime 上下文：必须静默跳过而不是抛 RuntimeError
        _emit_stream_event({"type": "provider_used", "provider": "x", "model": "m"})


class TestDisableAndSticky:
    """服务端确认无效（鉴权 401/403）→ 停用不再调用；最近成功者粘性提前命中缓存。"""

    def _chain(self, monkeypatch):
        _set_chain(monkeypatch, [
            {"name": "a", "model": "m-a", "base_url": "u", "api_key": "k"},
            {"name": "b", "model": "m-b", "base_url": "u", "api_key": "k"},
        ])

    def test_auth_skip_disables_provider(self, monkeypatch):
        from devflow.errors import HttpAuthError
        from devflow.llm_client import _candidates_providers, _log_provider_skip

        self._chain(monkeypatch)
        _log_provider_skip("a", HttpAuthError("401 Invalid API key"))
        assert [p["name"] for p in _candidates_providers()] == ["b"]  # a 已停用不再进链

    def test_non_auth_skip_does_not_disable(self, monkeypatch):
        from devflow.errors import LlmRateLimitError
        from devflow.llm_client import _candidates_providers, _log_provider_skip

        self._chain(monkeypatch)
        _log_provider_skip("a", LlmRateLimitError("rate limited"))
        assert [p["name"] for p in _candidates_providers()] == ["a", "b"]  # 瞬时失败不停用

    def test_disable_enable_roundtrip(self, monkeypatch):
        from devflow.llm_client import _candidates_providers, disable_provider, enable_provider

        self._chain(monkeypatch)
        assert disable_provider("a", "401") is True
        assert disable_provider("a", "401") is False  # 重复停用幂等
        assert enable_provider("a") is True
        assert [p["name"] for p in _candidates_providers()] == ["a", "b"]

    def test_sticky_success_moves_to_front(self, monkeypatch):
        from devflow.llm_client import (
            _candidates_providers,
            _mark_provider_success,
            reset_model_cache,
        )

        self._chain(monkeypatch)
        _mark_provider_success({"name": "b", "model": "m-b"})
        assert [p["name"] for p in _candidates_providers()] == ["b", "a"]
        reset_model_cache()
        assert [p["name"] for p in _candidates_providers()] == ["a", "b"]  # 重置后回配置顺序

    def test_sticky_cleared_when_provider_disabled(self, monkeypatch):
        from devflow.llm_client import (
            _candidates_providers,
            _mark_provider_success,
            disable_provider,
        )

        self._chain(monkeypatch)
        _mark_provider_success({"name": "b", "model": "m-b"})
        disable_provider("b", "401")
        assert [p["name"] for p in _candidates_providers()] == ["a"]

    def test_chain_api_marks_disabled_and_sticky(self, monkeypatch):
        from devflow.llm_client import _mark_provider_success, disable_provider
        from web.server import list_providers

        self._chain(monkeypatch)
        disable_provider("a", "401 Invalid API key")
        _mark_provider_success({"name": "b", "model": "m-b"})
        out = list_providers()
        names = [c["name"] for c in out["chain"]]
        assert names == ["b", "a"]  # 停用的排末尾
        assert out["chain"][0]["sticky"] is True
        assert out["chain"][1]["disabled"] is True
        assert out["sticky"] == "b"

    @pytest.mark.asyncio
    async def test_check_disables_on_auth_and_enables_on_ok(self, monkeypatch):
        import web.server as srv
        from devflow.llm_client import disable_provider

        async def fake_check(spec, *, timeout_sec=20):
            if spec["name"] == "a":
                return {"name": "a", "ok": False, "error_code": "HTTP.AUTH",
                        "error_message": "401 Invalid API key"}
            return {"name": spec["name"], "ok": True}

        monkeypatch.setattr("devflow.llm_client.check_llm_provider", fake_check)
        self._chain(monkeypatch)
        disable_provider("b", "历史遗留")  # 之前被停用的 b

        # 一键检测只查存活链（已停用的 b 不在其中）：a 鉴权失败 → 停用；b 保持停用
        out = await srv.check_providers(None)
        assert [c["name"] for c in out["chain"] if c["disabled"]] == ["a", "b"]
        assert [c["name"] for c in out["chain"] if not c["disabled"]] == []

        # 单独检测已停用的 b：通过 → 自动恢复
        out = await srv.check_providers(type("B", (), {"name": "b"})())
        live = [c["name"] for c in out["chain"] if not c["disabled"]]
        assert live == ["b"]
        assert [c["name"] for c in out["chain"] if c["disabled"]] == ["a"]


class TestPostMessageStream:
    """消息走 POST 请求体（修长文本 GET URL 400）：50KB 文本必须正常出流。"""

    @pytest.mark.asyncio
    async def test_50kb_text_streams_without_error(self):
        import json

        import web.server as srv

        tid = srv.create_session(
            type("C", (), {"thread_id": None, "set_fields": []})()
        )["thread_id"]
        big_text = "需求：商城下单支付流程，含库存校验与超时取消。" * 2600  # ≈50KB
        resp = await srv.send_message(tid, srv.SendMessage(text=big_text))
        assert resp.status_code == 200

        payloads = []
        async for chunk in resp.body_iterator:
            for line in str(chunk).splitlines():
                if line.startswith("data:"):
                    try:
                        payloads.append(json.loads(line[5:].strip()))
                    except Exception:
                        pass
        kinds = [e.get("type") for e in payloads]
        assert "stream_end" in kinds, f"流未正常收尾: {kinds[-6:]}"
        assert "error" not in kinds, (
            f"流中出现错误事件: {next((e for e in payloads if e.get('type') == 'error'), None)}"
        )
        # 基本流程仍然完整：澄清 → 需求确认门禁
        assert "gate" in kinds
