"""Mock 兜底的任务分派回归：mock 按「任务提示词的固定身份串」精确识别请求类型。

背景：需求抽取提示词里出现过「制图」二字（如「这些字段会在制图前被要求用户确认」），
旧分派按关键词猜意图，把需求抽取误判成制图请求——mock 模式下返回 nodes/edges/mermaid_source，
需求校验直接报 "Additional properties are not allowed ('edges', ...)"。
运行: pytest -v tests/test_mock_dispatch.py
"""
from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from devflow.llm_client import _MockLLM, _MockStructured
from devflow.nodes.clarify import (
    SYSTEM_PROMPT_EXTRACT,
    SYSTEM_PROMPT_SESSION_TITLE,
    USER_PROMPT_TEMPLATE,
)
from devflow.providers.llm_testgen import SYSTEM_PROMPT_TEST_DESIGN
from devflow.schemas import empty_requirement


def _extract_messages(user_text: str) -> list:
    return [
        SystemMessage(content=SYSTEM_PROMPT_EXTRACT),
        HumanMessage(content=USER_PROMPT_TEMPLATE.format(
            existing_requirement=empty_requirement(),
            latest_ai_message="（无）",
            earlier_context="",  # 非承接轮次：无「更早的未写入对话」块
            latest_user_message=user_text,
        )),
    ]


class TestStructuredDispatch:
    @pytest.mark.asyncio
    async def test_extract_prompt_with_graph_words_still_returns_requirement(self):
        """含「制图」字样的抽取提示词 → 必须返回需求模板，而不是制图模板。"""
        out = await _MockStructured().ainvoke(_extract_messages("给商城下单支付加库存校验"))
        payload = out.model_dump()
        assert "nodes" not in payload and "mermaid_source" not in payload
        assert payload["project_context"]

    @pytest.mark.asyncio
    async def test_extract_mock_declares_inferred_fields(self):
        """mock 演示需求整体是编造的 → 如实标注 AI 推断，制图前门禁照常触发。"""
        out = await _MockStructured().ainvoke(_extract_messages("随便说点什么"))
        payload = out.model_dump()
        assert payload["inferred_fields"], "mock 需求应声明推断字段"
        assert "project_context" in payload["inferred_fields"]

    @pytest.mark.asyncio
    async def test_graph_prompt_still_returns_graph(self):
        """制图提示词（含 mermaid 特征串）→ 仍走制图模板。"""
        out = await _MockStructured().ainvoke([
            SystemMessage(content="你是架构师，请生成 flowchart 逻辑图"),
            HumanMessage(content="请生成逻辑图 mermaid flowchart TD"),
        ])
        payload = out.model_dump()
        assert "nodes" in payload and "mermaid_source" in payload

    @pytest.mark.asyncio
    async def test_test_design_prompt_returns_test_design(self):
        out = await _MockStructured().ainvoke([
            SystemMessage(content=SYSTEM_PROMPT_TEST_DESIGN),
            HumanMessage(content="请设计测试场景"),
        ])
        payload = out.model_dump()
        assert "scenarios" in payload and "overview" in payload


class TestTextDispatch:
    @pytest.mark.asyncio
    async def test_session_title_prompt_returns_title_not_requirement(self):
        """起名提示词同样含「需求分析师」，不能被需求抽取分支抢答成一大坨 JSON。"""
        resp = await _MockLLM().ainvoke([
            SystemMessage(content=SYSTEM_PROMPT_SESSION_TITLE),
            HumanMessage(content="需求信息：{}\n\n请输出会话名称（只输出名称本身）："),
        ])
        title = str(resp.content)
        assert title == "桌面计算器科学计算"
        assert "{" not in title

    @pytest.mark.asyncio
    async def test_extract_prompt_returns_requirement_json(self):
        resp = await _MockLLM().ainvoke(_extract_messages("给商城下单支付加库存校验"))
        content = str(resp.content)
        assert '"project_context"' in content
        assert '"nodes"' not in content
