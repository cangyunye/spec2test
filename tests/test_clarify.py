"""澄清节点组增强测试（评审稿 §10 TC-C1~TC-C15 核心落地版）。

不依赖真实 LLM：LLM 调用走 mock 兜底或 monkeypatch。

覆盖：
  - TC-C5/C6/C7: clarify_build_question 无缺失/成功/降级
  - TC-C10:     clarify_validate 澄清轮次计数 + 6 轮上限终止
  - TC-C15:     compress_messages 热记忆保留最近 N 轮
运行: pytest -v tests/test_clarify.py
"""
from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from devflow.config import settings
from devflow.errors import CLARIFY_LOOP_EXHAUSTED, LlmRefusedError
from devflow.nodes.clarify import clarify_build_question_async, clarify_validate
from devflow.nodes.compress import compress_messages
from devflow.orchestrator import _route_after_validate
from devflow.schemas import empty_requirement


# ═══════════════════════════════════════════════════════════════════
# TC-C10: 澄清轮次上限（子步骤 A 新行为）
# ═══════════════════════════════════════════════════════════════════


class TestClarifyLoopLimit:
    def test_validate_increments_loop_cnt(self):
        """clarify_validate 每次进入把 retry_count['clarify_loop_cnt'] +1。"""
        state = {"requirement": {}, "retry_count": {}}
        out = clarify_validate(state)  # type: ignore[arg-type]
        assert out["retry_count"]["clarify_loop_cnt"] == 1

    def test_loop_cnt_accumulates_from_existing(self):
        """已有计数 5 时再进入 → 6。"""
        state = {"requirement": {}, "retry_count": {"clarify_loop_cnt": 5}}
        out = clarify_validate(state)  # type: ignore[arg-type]
        assert out["retry_count"]["clarify_loop_cnt"] == 6

    def test_over_limit_writes_exhausted_error(self):
        """第 6 轮仍有缺失 → 写 last_error=CLARIFY.LOOP_EXHAUSTED + retryable=False。"""
        req = empty_requirement()
        state = {
            "requirement": req,
            "retry_count": {"clarify_loop_cnt": settings.CLARIFY_MAX_ROUNDS},
        }
        out = clarify_validate(state)  # type: ignore[arg-type]
        assert out["last_error_code"] == CLARIFY_LOOP_EXHAUSTED
        assert out["last_error_retryable"] is False
        assert out["missing_fields"]  # 缺失清单保留
        assert out["retry_count"]["clarify_loop_cnt"] == settings.CLARIFY_MAX_ROUNDS + 1

    def test_route_after_validate_aborts_when_exhausted(self):
        """澄清耗尽 → 路由必须 abort（否则无限循环追问）。"""
        state = {
            "requirement": empty_requirement(),
            "missing_fields": ["target_modules"],
            "last_error_code": CLARIFY_LOOP_EXHAUSTED,
            "last_error_retryable": False,
            "retry_count": {"clarify_loop_cnt": settings.CLARIFY_MAX_ROUNDS + 1},
        }
        assert _route_after_validate(state) == "abort"

    def test_route_normal_missing_still_asks(self):
        """未超限且缺字段 → 仍返回 need_more_info 继续追问。"""
        state = {
            "requirement": empty_requirement(),
            "missing_fields": ["target_modules"],
            "last_error_code": None,
            "last_error_retryable": False,
            "retry_count": {"clarify_loop_cnt": 2},
        }
        assert _route_after_validate(state) == "need_more_info"


# ═══════════════════════════════════════════════════════════════════
# TC-C5/C6/C7: clarify_build_question
# ═══════════════════════════════════════════════════════════════════


class TestBuildQuestion:
    @pytest.mark.asyncio
    async def test_c5_no_missing_returns_empty(self):
        """没有缺失字段 → 不追加消息、不调 LLM。"""
        state = {"missing_fields": [], "requirement": empty_requirement()}
        out = await clarify_build_question_async(state)  # type: ignore[arg-type]
        assert out == {}

    @pytest.mark.asyncio
    async def test_c6_success_appends_aimessage(self, monkeypatch):
        """LLM 成功 → 追加 1 条 AIMessage 且内容为 questions。"""
        async def fake_invoke_json(**kwargs):
            return {"questions": "请补充：\n- 项目根目录？\n- 验收标准？"}

        monkeypatch.setattr("devflow.nodes.clarify.invoke_json", fake_invoke_json)
        state = {
            "missing_fields": ["project_root", "acceptance_criteria"],
            "requirement": empty_requirement(),
        }
        out = await clarify_build_question_async(state)  # type: ignore[arg-type]
        msgs = out["messages"]
        assert len(msgs) == 1
        assert isinstance(msgs[0], AIMessage)
        assert "项目根目录" in msgs[0].content

    @pytest.mark.asyncio
    async def test_c7_llm_error_falls_back_to_missing_list(self, monkeypatch):
        """LLM 抛 DevFlowError → 降级为缺失字段原样列表（流程不中断）。"""
        async def boom(**kwargs):
            raise LlmRefusedError("provider quota exceeded")

        monkeypatch.setattr("devflow.nodes.clarify.invoke_json", boom)
        state = {
            "missing_fields": ["edge_cases", "io_constraints.input"],
            "requirement": empty_requirement(),
        }
        out = await clarify_build_question_async(state)  # type: ignore[arg-type]
        msgs = out["messages"]
        assert len(msgs) == 1
        content = msgs[0].content
        assert "edge_cases" in content and "io_constraints.input" in content
        assert "LLM.REFUSED" in content  # 降级文本带错误码便于诊断


# ═══════════════════════════════════════════════════════════════════
# TC-C15: compress_messages 热记忆保留
# ═══════════════════════════════════════════════════════════════════


class TestCompressHotMemory:
    def test_keeps_last_n_rounds(self):
        """10 轮消息（20 条）压缩后只剩最近 N 轮。"""
        msgs: list = []
        for i in range(10):
            msgs.append(HumanMessage(content=f"u{i}"))
            msgs.append(AIMessage(content=f"a{i}"))
        state = {"messages": list(msgs)}
        out = compress_messages(state)  # type: ignore[arg-type]
        assert len(out["messages"]) == settings.HOT_MEMORY_LAST_N * 2
        assert out["messages"][-1].content == "a9"
        assert out["messages"][0].content == f"u{10 - settings.HOT_MEMORY_LAST_N}"

    def test_short_history_untouched(self):
        """消息数 ≤ N*2 时不压缩。"""
        msgs = [HumanMessage(content="u0"), AIMessage(content="a0")]
        state = {"messages": list(msgs)}
        out = compress_messages(state)  # type: ignore[arg-type]
        assert out == {}
