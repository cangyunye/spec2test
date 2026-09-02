"""D2 check-llm 连通性自检测试。

覆盖：
  - check_llm_provider 成功路径（返回 ok + 耗时 + 回复摘要）
  - check_llm_provider 失败路径（裸异常 → 映射错误码）
  - check_llm_all 遍历所有 provider 返回列表
运行: pytest -v tests/test_check_llm.py
"""
from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage

from devflow.llm_client import check_llm_all, check_llm_provider


class _FakeOkLLM:
    async def ainvoke(self, messages: list[BaseMessage], **_: Any) -> BaseMessage:
        return AIMessage(content="ok")


class _FakeErrLLM:
    async def ainvoke(self, messages: list[BaseMessage], **_: Any) -> BaseMessage:
        raise ConnectionError("connection refused by upstream")


SPEC_OK = {
    "name": "deepseek",
    "base_url": "https://api.deepseek.com/v1",
    "api_key": "sk-dummy",
    "model": "deepseek-chat",
    "temperature": 0.1,
}
SPEC_ERR = {**SPEC_OK, "name": "bad-gateway"}


class TestCheckLlmProvider:
    @pytest.mark.asyncio
    async def test_ok_returns_report(self, monkeypatch):
        monkeypatch.setattr(
            "devflow.llm_client._get_model", lambda spec: _FakeOkLLM()
        )
        report = await check_llm_provider(SPEC_OK)
        assert report["ok"] is True
        assert report["name"] == "deepseek"
        assert report["model"] == "deepseek-chat"
        assert report["error_code"] is None
        assert report["elapsed_ms"] >= 0
        assert "ok" in report["reply"]

    @pytest.mark.asyncio
    async def test_failure_maps_error_code(self, monkeypatch):
        monkeypatch.setattr(
            "devflow.llm_client._get_model", lambda spec: _FakeErrLLM()
        )
        report = await check_llm_provider(SPEC_ERR)
        assert report["ok"] is False
        assert report["name"] == "bad-gateway"
        assert report["error_code"] == "HTTP.NETWORK"
        assert report["error_message"]

    @pytest.mark.asyncio
    async def test_missing_key_still_reports_not_ok(self, monkeypatch):
        """无 key / key 无效 → 报告 ok=False + 具体错误码，不抛异常。"""
        async def boom(spec):
            raise RuntimeError("unreachable in test")
        # 直接让 _get_model 抛（模拟构造失败）
        def bad_get_model(spec):
            raise ValueError("bad base_url")

        monkeypatch.setattr("devflow.llm_client._get_model", bad_get_model)
        report = await check_llm_provider({**SPEC_OK, "name": "x"})
        assert report["ok"] is False
        assert report["error_code"] is not None


class TestCheckLlmAll:
    @pytest.mark.asyncio
    async def test_returns_one_report_per_provider(self, monkeypatch):
        calls: list[str] = []

        def fake_get_model(spec):
            calls.append(spec["name"])
            if spec["name"] == "broken":
                return _FakeErrLLM()
            return _FakeOkLLM()

        monkeypatch.setattr("devflow.llm_client._get_model", fake_get_model)
        monkeypatch.setattr(
            "devflow.llm_client.settings",
            _FakeSettings(
                [SPEC_OK, {**SPEC_ERR, "name": "broken"}, {**SPEC_OK, "name": "sf"}]
            ),
        )
        reports = await check_llm_all()
        assert len(reports) == 3
        assert [r["name"] for r in reports] == ["deepseek", "broken", "sf"]
        assert reports[0]["ok"] is True
        assert reports[1]["ok"] is False
        assert len(calls) == 3


class _FakeSettings:
    def __init__(self, providers: list[dict[str, Any]]) -> None:
        self.LLM_PROVIDERS = providers
