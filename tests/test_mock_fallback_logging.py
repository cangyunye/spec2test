"""Mock 兜底可观测性回归：所有真实 provider 失败后走 Mock 时，
日志必须逐 provider 留痕（名称/错误码/原因），兜底行附带最后一个真实错误。

背景：制图偶尔「与输入需求无关」（计算器示例图），排查时无法从日志判断
是哪个 provider、因何失败、何时落到 Mock。此文件锁定这条日志链路。
运行: pytest -v tests/test_mock_fallback_logging.py
"""
from __future__ import annotations

import logging

import pytest

from devflow.errors import LlmRefusedError
from devflow.llm_client import _MOCK_DEMO_REQUIREMENT, invoke_json, invoke_text


def _two_fake_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    from devflow.config import settings

    monkeypatch.setattr(
        settings, "LLM_PROVIDERS",
        [
            {"name": "p1", "base_url": "http://fake", "api_key": "k", "model": "m1"},
            {"name": "p2", "base_url": "http://fake", "api_key": "k", "model": "m2"},
        ],
        raising=False,
    )


@pytest.mark.asyncio
async def test_invoke_json_logs_each_provider_failure_then_mock(caplog, monkeypatch):
    """invoke_json：每个 provider 失败各留一条日志，mock 兜底行带最后错误码。"""
    _two_fake_providers(monkeypatch)

    async def _fail_real_succeed_mock(*, model_spec: str = "", **_: object) -> dict:
        if model_spec == "mock":
            return dict(_MOCK_DEMO_REQUIREMENT)
        raise LlmRefusedError("模型拒绝回答（模拟）")

    monkeypatch.setattr("devflow.llm_client._invoke_json_once", _fail_real_succeed_mock)

    with caplog.at_level(logging.WARNING, logger="devflow.llm_client"):
        result = await invoke_json(
            system_prompt="s", user_prompt="u",
            response_model=None, response_type="logic_graph",
        )

    # 落到 Mock：返回演示需求模板
    assert result["project_context"].startswith("Mock 演示")

    p1_logs = [r for r in caplog.records if "provider「p1」" in r.getMessage()]
    p2_logs = [r for r in caplog.records if "provider「p2」" in r.getMessage()]
    assert p1_logs and p2_logs, "每个 provider 失败都必须有独立日志"
    assert "LLM.REFUSED" in p1_logs[0].getMessage()
    assert "模型拒绝回答（模拟）" in p1_logs[0].getMessage()

    fallback_logs = [r for r in caplog.records if "走 Mock 兜底" in r.getMessage()]
    assert fallback_logs, "必须有 mock 兜底日志"
    assert "logic_graph" in fallback_logs[0].getMessage()
    assert "LLM.REFUSED" in fallback_logs[0].getMessage(), "兜底行应附带最后一个真实错误码"


@pytest.mark.asyncio
async def test_invoke_text_logs_mock_fallback_with_last_error(caplog, monkeypatch):
    """invoke_text：全部失败走 mock 时同样留痕（response_type=text）。"""
    _two_fake_providers(monkeypatch)
    import devflow.llm_client as lc

    real_get_model = lc._get_model

    def _fail_real(spec: object):
        if spec == "mock":
            return real_get_model("mock")
        raise LlmRefusedError("文本调用失败（模拟）")

    monkeypatch.setattr(lc, "_get_model", _fail_real)

    with caplog.at_level(logging.WARNING, logger="devflow.llm_client"):
        text = await invoke_text(system_prompt="s", user_prompt="u")

    assert "mock fallback" in text
    fallback_logs = [r for r in caplog.records if "走 Mock 兜底" in r.getMessage()]
    assert fallback_logs
    assert "text" in fallback_logs[0].getMessage()
    assert "LLM.REFUSED" in fallback_logs[0].getMessage()


@pytest.mark.asyncio
async def test_no_providers_configured_logs_plain_mock(caplog, monkeypatch):
    """未配置任何 provider（测试/演示模式）→ 兜底日志明确说明，不误导为「全部失败」。"""
    from devflow.config import settings

    monkeypatch.setattr(settings, "LLM_PROVIDERS", [], raising=False)

    # "requirement"+"json" 关键词命中 mock 的需求模板分支，走可解析 JSON 路径
    with caplog.at_level(logging.WARNING, logger="devflow.llm_client"):
        result = await invoke_json(
            system_prompt="s", user_prompt="extract requirement as json",
            response_type="logic_graph",
        )

    assert result["project_context"].startswith("Mock 演示")
    fallback_logs = [r for r in caplog.records if "未配置任何真实 provider" in r.getMessage()]
    assert fallback_logs
