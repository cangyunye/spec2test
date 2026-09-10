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

    def test_emit_outside_runtime_is_silent_noop(self):
        # 无 LangGraph runtime 上下文：必须静默跳过而不是抛 RuntimeError
        _emit_stream_event({"type": "provider_used", "provider": "x", "model": "m"})
