"""澄清节点组增强测试（评审稿 §10 TC-C1~TC-C15 核心落地版）。

不依赖真实 LLM：LLM 调用走 mock 兜底或 monkeypatch。

覆盖：
  - TC-C5/C6/C7: clarify_build_question 无缺失/成功/降级
  - TC-C10:     clarify_validate 澄清轮次计数 + 6 轮上限终止
  - TC-C15:     compress_messages 热记忆保留最近 N 轮
  - 对话式澄清：头脑风暴 / 拷问模式（入口指令、轮次上限、一轮一问）
运行: pytest -v tests/test_clarify.py
"""
from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from devflow.config import settings
from devflow.errors import CLARIFY_LOOP_EXHAUSTED, LlmRefusedError
from devflow.nodes.clarify import (
    clarify_build_question_async,
    clarify_extract_async,
    clarify_validate,
    detect_mode_switch,
    pick_dialog_field,
)
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


# ═══════════════════════════════════════════════════════════════════
# 对话式澄清：头脑风暴 / 拷问模式
# ═══════════════════════════════════════════════════════════════════


class TestDetectModeSwitch:
    def test_plain_keywords_enter_modes(self):
        assert detect_mode_switch("拷问") == ("grill", "")
        assert detect_mode_switch("拷问 ") == ("grill", "")
        assert detect_mode_switch("头脑风暴") == ("brainstorm", "")

    def test_exit_prefix_returns_normal(self):
        assert detect_mode_switch("退出拷问") == ("normal", "")
        assert detect_mode_switch("结束头脑风暴模式") == ("normal", "")

    def test_keyword_with_suffix_context(self):
        mode, rest = detect_mode_switch("头脑风暴：我想做个番茄钟")
        assert mode == "brainstorm"
        assert rest == "我想做个番茄钟"
        mode, rest = detect_mode_switch("拷问：重点盯着边界情况")
        assert mode == "grill"
        assert rest == "重点盯着边界情况"

    def test_normal_message_untouched(self):
        text = "这个功能主要给运营后台的管理员用"
        assert detect_mode_switch(text) == (None, text)

    def test_keyword_mid_sentence_not_triggered(self):
        text = "请不要拷问我了，直接开工"
        assert detect_mode_switch(text) == (None, text)

    def test_empty_message(self):
        assert detect_mode_switch("   ") == (None, "")


class TestPickDialogField:
    def test_priority_order(self):
        missing = [
            "acceptance_criteria: 至少定义 1 条验收标准",
            "req_type: 必填字段缺失",
            "io_constraints.input: 必须填写输入约束",
        ]
        key, entry = pick_dialog_field(missing)
        assert key == "req_type"
        assert "req_type" in entry

    def test_unknown_fields_rank_last(self):
        missing = ["weird_field: schema 报错原文", "project_context: 必须填写项目背景简述"]
        key, _ = pick_dialog_field(missing)
        assert key == "project_context"


class TestDialogModeExtract:
    @pytest.mark.asyncio
    async def test_mode_switch_short_circuits_extraction(self, monkeypatch):
        """纯指令消息 → 只切模式 + 追加确认消息，不调 LLM 抽取。"""
        calls: list[dict] = []

        async def fake(**kwargs):
            calls.append(kwargs)
            return {}

        monkeypatch.setattr("devflow.nodes.clarify.invoke_json", fake)
        state = {
            "messages": [HumanMessage(content="拷问")],
            "requirement": empty_requirement(),
            "clarify_mode": "normal",
        }
        out = await clarify_extract_async(state)  # type: ignore[arg-type]
        assert out["clarify_mode"] == "grill"
        assert out["requirement"] == empty_requirement()
        assert calls == []  # 指令消息不做需求抽取
        msgs = out["messages"]
        assert len(msgs) == 1 and isinstance(msgs[0], AIMessage)
        assert "拷问模式" in msgs[0].content
        assert "退出拷问" in msgs[0].content  # 告知退出方式

    @pytest.mark.asyncio
    async def test_switch_to_same_mode_no_announce(self, monkeypatch):
        """已是 grill 再发「拷问」→ 不重复发确认消息。"""
        state = {
            "messages": [HumanMessage(content="拷问")],
            "requirement": empty_requirement(),
            "clarify_mode": "grill",
        }
        out = await clarify_extract_async(state)  # type: ignore[arg-type]
        assert out["clarify_mode"] == "grill"
        assert "messages" not in out

    @pytest.mark.asyncio
    async def test_brainstorm_suffix_still_extracts(self, monkeypatch):
        """「头脑风暴：xxx」→ 切模式 + 用剩余文本抽取（指令关键词不进抽取）。"""
        captured: dict = {}

        async def fake(**kwargs):
            captured.update(kwargs)
            return {"project_context": "番茄钟桌面应用，帮助专注工作"}

        monkeypatch.setattr("devflow.nodes.clarify.invoke_json", fake)
        state = {
            "messages": [HumanMessage(content="头脑风暴：我想做个番茄钟应用")],
            "requirement": empty_requirement(),
            "clarify_mode": "normal",
        }
        out = await clarify_extract_async(state)  # type: ignore[arg-type]
        assert out["clarify_mode"] == "brainstorm"
        assert "头脑风暴：我想做个番茄钟应用" not in captured["user_prompt"]
        assert "我想做个番茄钟应用" in captured["user_prompt"]
        assert out["requirement"]["project_context"] == "番茄钟桌面应用，帮助专注工作"
        # 切模式确认消息与抽取结果同轮返回
        assert isinstance(out["messages"][0], AIMessage)

    @pytest.mark.asyncio
    async def test_exit_mode(self, monkeypatch):
        """「退出头脑风暴」→ 回 normal，不发确认时（同模式切 normal 仍发确认）。"""
        async def fake(**kwargs):
            return {}

        monkeypatch.setattr("devflow.nodes.clarify.invoke_json", fake)
        state = {
            "messages": [HumanMessage(content="退出头脑风暴")],
            "requirement": empty_requirement(),
            "clarify_mode": "brainstorm",
        }
        out = await clarify_extract_async(state)  # type: ignore[arg-type]
        assert out["clarify_mode"] == "normal"
        assert "普通模式" in out["messages"][0].content

    @pytest.mark.asyncio
    async def test_confirmation_resolves_against_last_ai_question(self, monkeypatch):
        """拷问模式下回复「同意」→ 抽取 prompt 带上轮 AI 追问做对应。"""
        captured: dict = {}

        async def fake(**kwargs):
            captured.update(kwargs)
            return {"req_type": "new_feature"}

        monkeypatch.setattr("devflow.nodes.clarify.invoke_json", fake)
        req = empty_requirement()
        state = {
            "messages": [
                AIMessage(content="【拷问】这个需求是全新功能吗？推荐答案：new_feature（首次提出）。"),
                HumanMessage(content="同意"),
            ],
            "requirement": req,
            "clarify_mode": "grill",
        }
        out = await clarify_extract_async(state)  # type: ignore[arg-type]
        assert "推荐答案" in captured["user_prompt"]  # 上轮追问进了上下文
        assert "同意" in captured["user_prompt"]
        assert out["requirement"]["req_type"] == "new_feature"


class TestDialogModeValidate:
    def _state(self, mode: str, cnt: int) -> dict:
        return {
            "requirement": empty_requirement(),
            "clarify_mode": mode,
            "retry_count": {"clarify_loop_cnt": cnt},
        }

    def test_dialog_mode_relaxed_cap(self):
        """拷问模式第 12 轮仍缺 → 不终止（上限 12）；第 13 轮 → LOOP_EXHAUSTED。"""
        out = clarify_validate(self._state("grill", settings.CLARIFY_DIALOG_MAX_ROUNDS - 1))  # type: ignore[arg-type]
        assert out.get("last_error_code") is None
        assert out["retry_count"]["clarify_loop_cnt"] == settings.CLARIFY_DIALOG_MAX_ROUNDS

        out = clarify_validate(self._state("grill", settings.CLARIFY_DIALOG_MAX_ROUNDS))  # type: ignore[arg-type]
        assert out["last_error_code"] == CLARIFY_LOOP_EXHAUSTED

    def test_brainstorm_mode_same_relaxed_cap(self):
        out = clarify_validate(self._state("brainstorm", settings.CLARIFY_DIALOG_MAX_ROUNDS - 1))  # type: ignore[arg-type]
        assert out.get("last_error_code") is None

    def test_normal_mode_keeps_original_cap(self):
        out = clarify_validate(self._state("normal", settings.CLARIFY_MAX_ROUNDS))  # type: ignore[arg-type]
        assert out["last_error_code"] == CLARIFY_LOOP_EXHAUSTED

    def test_missing_mode_field_defaults_normal(self):
        """旧 checkpoint 无 clarify_mode 字段 → 按 normal 处理。"""
        out = clarify_validate({"requirement": empty_requirement(),
                                "retry_count": {"clarify_loop_cnt": settings.CLARIFY_MAX_ROUNDS}})  # type: ignore[arg-type]
        assert out["last_error_code"] == CLARIFY_LOOP_EXHAUSTED


class TestDialogModeBuildQuestion:
    @pytest.mark.asyncio
    async def test_grill_asks_single_highest_priority_field(self, monkeypatch):
        """拷问模式 → prompt 只含优先级最高的一个缺口，不列清单。"""
        captured: dict = {}

        async def fake(**kwargs):
            captured.update(kwargs)
            return {"questions": "这个需求是全新功能、迭代还是缺陷修复？推荐：new_feature。"}

        monkeypatch.setattr("devflow.nodes.clarify.invoke_json", fake)
        state = {
            "missing_fields": [
                "acceptance_criteria: 至少定义 1 条验收标准",
                "req_type: 必填字段缺失",
            ],
            "requirement": empty_requirement(),
            "clarify_mode": "grill",
            "retry_count": {"clarify_loop_cnt": 2},
        }
        out = await clarify_build_question_async(state)  # type: ignore[arg-type]
        # 缺口条目行只允许出现最高优先级的那一条（requirement JSON 里的键名不算）
        assert "req_type: 必填字段缺失" in captured["user_prompt"]
        assert "acceptance_criteria: 至少定义 1 条验收标准" not in captured["user_prompt"]
        assert "只问这一个" in captured["user_prompt"]
        assert len(out["messages"]) == 1

    @pytest.mark.asyncio
    async def test_brainstorm_uses_brainstorm_prompt(self, monkeypatch):
        """头脑风暴模式 → 用头脑风暴 prompt（候选方向 / 你来定）。"""
        captured: dict = {}

        async def fake(**kwargs):
            captured.update(kwargs)
            return {"questions": "你想解决什么问题？候选：a/b/c，也可以说「你来定」。"}

        monkeypatch.setattr("devflow.nodes.clarify.invoke_json", fake)
        state = {
            "missing_fields": ["edge_cases: 至少列出 1 个边界场景"],
            "requirement": empty_requirement(),
            "clarify_mode": "brainstorm",
            "retry_count": {"clarify_loop_cnt": 2},
        }
        await clarify_build_question_async(state)  # type: ignore[arg-type]
        assert captured["system_prompt"] != "" 
        assert "候选方向" in captured["system_prompt"] or "头脑风暴" in captured["system_prompt"]

    @pytest.mark.asyncio
    async def test_grill_fallback_keeps_mode_tag(self, monkeypatch):
        """拷问模式 LLM 挂掉 → 降级文本仍标注拷问模式与目标字段。"""
        async def boom(**kwargs):
            raise LlmRefusedError("provider quota exceeded")

        monkeypatch.setattr("devflow.nodes.clarify.invoke_json", boom)
        state = {
            "missing_fields": ["req_type: 必填字段缺失"],
            "requirement": empty_requirement(),
            "clarify_mode": "grill",
            "retry_count": {"clarify_loop_cnt": 2},
        }
        out = await clarify_build_question_async(state)  # type: ignore[arg-type]
        content = out["messages"][0].content
        assert "拷问模式" in content and "req_type" in content

    @pytest.mark.asyncio
    async def test_normal_first_round_sets_mode_choice_flag(self, monkeypatch):
        """普通模式首轮追问 → clarify_mode_prompt=True（客户端弹选择卡）；后续轮 False。"""
        async def fake(**kwargs):
            return {"questions": "请补充验收标准"}

        monkeypatch.setattr("devflow.nodes.clarify.invoke_json", fake)
        base = {
            "missing_fields": ["acceptance_criteria: 至少定义 1 条验收标准"],
            "requirement": empty_requirement(),
            "clarify_mode": "normal",
        }
        out = await clarify_build_question_async({**base, "retry_count": {"clarify_loop_cnt": 1}})  # type: ignore[arg-type]
        assert out["clarify_mode_prompt"] is True

        out = await clarify_build_question_async({**base, "retry_count": {"clarify_loop_cnt": 3}})  # type: ignore[arg-type]
        assert out["clarify_mode_prompt"] is False

    @pytest.mark.asyncio
    async def test_dialog_modes_never_show_mode_choice(self, monkeypatch):
        """头脑风暴 / 拷问模式下不再弹选择卡（已在对模式里了）。"""
        async def fake(**kwargs):
            return {"questions": "只问一个问题"}

        monkeypatch.setattr("devflow.nodes.clarify.invoke_json", fake)
        for mode in ("grill", "brainstorm"):
            out = await clarify_build_question_async({
                "missing_fields": ["req_type: 必填字段缺失"],
                "requirement": empty_requirement(),
                "clarify_mode": mode,
                "retry_count": {"clarify_loop_cnt": 1},
            })  # type: ignore[arg-type]
            assert out["clarify_mode_prompt"] is False


class TestClarifyCompleteConfirm:
    """需求完备时校验节点的确定性确认消息（不花 LLM token）。"""

    @staticmethod
    def _complete_req() -> dict:
        """无代码模式的完备需求样例（可通过 validate_requirement 全部校验）。"""
        return {
            "req_type": "new_feature",
            "project_root": "",
            "project_context": "桌面番茄钟应用，帮助专注工作",
            "target_modules": [],
            "existing_code_accessible": False,
            "reference_files": [],
            "io_constraints": {"input": "点击开始/暂停", "output": "倒计时与统计提示"},
            "edge_cases": ["计时中关闭窗口"],
            "acceptance_criteria": ["25 分钟倒计时准确"],
        }

    def test_complete_on_first_shot_confirms_clear(self):
        """一次说清 → 「已足够清晰，无需再发散」确认 + 直接推进制图。"""
        out = clarify_validate({
            "requirement": self._complete_req(),
            "clarify_mode": "normal",
            "missing_fields": [],
            "retry_count": {},
        })  # type: ignore[arg-type]
        assert out["current_stage"] == "graph"
        assert "足够清晰" in out["messages"][0].content
        assert out["clarify_mode_prompt"] is False

    def test_completed_after_missing_confirms_filled(self):
        """上轮有缺口、本轮补齐 → 「缺口已补齐」确认。"""
        out = clarify_validate({
            "requirement": self._complete_req(),
            "clarify_mode": "grill",
            "missing_fields": ["acceptance_criteria: 至少定义 1 条验收标准"],
            "retry_count": {"clarify_loop_cnt": 2},
        })  # type: ignore[arg-type]
        assert out["current_stage"] == "graph"
        assert "缺口已补齐" in out["messages"][0].content

    def test_still_incomplete_no_confirm(self):
        """仍有缺失 → 无确认消息，继续追问路径。"""
        out = clarify_validate({
            "requirement": empty_requirement(),
            "clarify_mode": "normal",
            "missing_fields": ["req_type: 必填字段缺失"],
            "retry_count": {"clarify_loop_cnt": 1},
        })  # type: ignore[arg-type]
        assert out["current_stage"] == "clarify"
        assert "messages" not in out

    def test_second_clean_pass_no_repeat(self):
        """完备后再走一遍校验（如检索空集回澄清）→ 不重复确认。"""
        out = clarify_validate({
            "requirement": self._complete_req(),
            "clarify_mode": "normal",
            "missing_fields": [],
            "retry_count": {"clarify_loop_cnt": 3},
        })  # type: ignore[arg-type]
        assert "messages" not in out
