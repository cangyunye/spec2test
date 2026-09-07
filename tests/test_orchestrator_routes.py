"""orchestrator 路由函数单元测试。

直接构造含错 / 无错 / 超限的 fake State dict，调用 7 条 _route_after_* 函数
和 dead_letter_drain_node，断言所有分支的返回标签。

不跑真实 LangGraph graph，不调 LLM，纯函数测试（< 1 s）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from devflow.orchestrator import (
    _GRAPH_RETRY_CAP_PER_NODE,
    _error_retry_or,
    _route_after_build_question,
    _route_after_code_gen,
    _route_after_code_search,
    _route_after_extract,
    _route_after_graph_generate,
    _route_after_test_gen,
    _route_after_validate,
    dead_letter_drain_node,
    initial_state,
)


# ═══════════════════════════════════════════════════════════════════
# 辅助：构造 fake state
# ═══════════════════════════════════════════════════════════════════


def _state(
    *,
    error_code: str | None = None,
    retryable: bool | None = None,
    retry_count: dict[str, int] | None = None,
    error_msg: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """快速构造一个含错误字段的 fake GlobalState。"""
    s: dict[str, Any] = {
        "last_error_code": error_code,
        "last_error_retryable": retryable,
        "retry_count": retry_count or {},
        "last_error": error_msg,
    }
    s.update(kwargs)
    return s


# ═══════════════════════════════════════════════════════════════════
# 1. _error_retry_or — 通用错误路由辅助
# ═══════════════════════════════════════════════════════════════════


class TestErrorRetryOr:
    """4 条分支：无错→fallback / 可重试+未超限→retry / 可重试+超限→fallback / 不可重试→fallback。"""

    def test_no_error_returns_fallback(self):
        s = _state(error_code=None)
        assert _error_retry_or(s, "node_a", fallback_label="ok") == "ok"

    def test_retryable_within_cap_returns_retry(self):
        s = _state(
            error_code="LLM.UPSTREAM",
            retryable=True,
            retry_count={"node_a": 1},
        )
        assert _error_retry_or(s, "node_a", fallback_label="ok") == "retry"

    def test_retryable_at_cap_returns_retry(self):
        """count == cap (3) 仍然可重试（<= 判断）。"""
        s = _state(
            error_code="LLM.RATE_LIMIT",
            retryable=True,
            retry_count={"node_a": _GRAPH_RETRY_CAP_PER_NODE},
        )
        assert _error_retry_or(s, "node_a", fallback_label="ok") == "retry"

    def test_retryable_over_cap_returns_fallback(self):
        s = _state(
            error_code="LLM.RATE_LIMIT",
            retryable=True,
            retry_count={"node_a": _GRAPH_RETRY_CAP_PER_NODE + 1},
        )
        assert _error_retry_or(s, "node_a", fallback_label="ok") == "ok"

    def test_not_retryable_returns_fallback(self):
        s = _state(
            error_code="LLM.REFUSED",
            retryable=False,
            retry_count={"node_a": 0},
        )
        assert _error_retry_or(s, "node_a", fallback_label="abort") == "abort"

    def test_retryable_none_treated_as_false(self):
        """retryable=None 时 bool(None)=False → fallback。"""
        s = _state(error_code="LLM.UPSTREAM", retryable=None)
        assert _error_retry_or(s, "node_a", fallback_label="done") == "done"


# ═══════════════════════════════════════════════════════════════════
# 2. _route_after_extract
# ═══════════════════════════════════════════════════════════════════


class TestRouteAfterExtract:
    """clarify_extract 后：无错→ok / 可重试→retry / 不可重试→ok。"""

    def test_no_error_ok(self):
        s = _state(error_code=None)
        assert _route_after_extract(s) == "ok"

    def test_retryable_error_retry(self):
        s = _state(
            error_code="LLM.UPSTREAM",
            retryable=True,
            retry_count={"clarify_extract": 1},
        )
        assert _route_after_extract(s) == "retry"

    def test_not_retryable_error_ok(self):
        """不可重试错（如 LLM.REFUSED）→ 不重试，推进到 validate。"""
        s = _state(
            error_code="LLM.REFUSED",
            retryable=False,
            retry_count={"clarify_extract": 0},
        )
        assert _route_after_extract(s) == "ok"

    def test_retryable_over_cap_ok(self):
        s = _state(
            error_code="LLM.RATE_LIMIT",
            retryable=True,
            retry_count={"clarify_extract": _GRAPH_RETRY_CAP_PER_NODE + 1},
        )
        assert _route_after_extract(s) == "ok"


# ═══════════════════════════════════════════════════════════════════
# 3. _route_after_validate
# ═══════════════════════════════════════════════════════════════════


class TestRouteAfterValidate:
    """validate 后：有缺失→need_more_info / 无缺失+不可重试错→abort / 无缺失无错→info_complete。"""

    def test_missing_fields_need_more_info(self):
        s = _state(missing_fields=["project_root", "io_constraints"])
        assert _route_after_validate(s) == "need_more_info"

    def test_no_missing_no_error_info_complete(self):
        s = _state(missing_fields=[])
        assert _route_after_validate(s) == "info_complete"

    def test_no_missing_not_retryable_error_abort(self):
        s = _state(
            error_code="LLM.REFUSED",
            retryable=False,
            missing_fields=[],
        )
        assert _route_after_validate(s) == "abort"

    def test_no_missing_retryable_error_info_complete(self):
        """可重试错 + 无缺失 → 正常推进（extract 那轮的 retry 由 _route_after_extract 管）。"""
        s = _state(
            error_code="LLM.UPSTREAM",
            retryable=True,
            missing_fields=[],
        )
        assert _route_after_validate(s) == "info_complete"

    def test_missing_overrides_error(self):
        """有缺失字段时优先 need_more_info，不管有没有错误。"""
        s = _state(
            error_code="LLM.REFUSED",
            retryable=False,
            missing_fields=["project_root"],
        )
        assert _route_after_validate(s) == "need_more_info"


# ═══════════════════════════════════════════════════════════════════
# 4. _route_after_build_question
# ═══════════════════════════════════════════════════════════════════


class TestRouteAfterBuildQuestion:
    """build_question 后：无错→__end__ / 可重试→retry / 不可重试→__end__。"""

    def test_no_error_end(self):
        s = _state(error_code=None)
        assert _route_after_build_question(s) == "__end__"

    def test_retryable_error_retry(self):
        s = _state(
            error_code="LLM.UPSTREAM",
            retryable=True,
            retry_count={"clarify_build_question": 1},
        )
        assert _route_after_build_question(s) == "retry"

    def test_not_retryable_error_end(self):
        s = _state(
            error_code="LLM.REFUSED",
            retryable=False,
        )
        assert _route_after_build_question(s) == "__end__"

    def test_retryable_over_cap_end(self):
        s = _state(
            error_code="LLM.RATE_LIMIT",
            retryable=True,
            retry_count={"clarify_build_question": _GRAPH_RETRY_CAP_PER_NODE + 1},
        )
        assert _route_after_build_question(s) == "__end__"


# ═══════════════════════════════════════════════════════════════════
# 5. _route_after_graph_generate
# ═══════════════════════════════════════════════════════════════════


class TestRouteAfterGraphGenerate:
    """制图后：无错→ok / 可重试+count<2→retry_graph / 可重试+count>=2→abort / 不可重试→abort。"""

    def test_no_error_ok(self):
        s = _state(error_code=None, error_msg=None)
        assert _route_after_graph_generate(s) == "ok"

    def test_retryable_count_zero_retry_graph(self):
        s = _state(
            error_code="LLM.UPSTREAM",
            retryable=True,
            error_msg="[graph_generate:LLM.UPSTREAM] 5xx",
            retry_count={"graph_generate": 0},
        )
        assert _route_after_graph_generate(s) == "retry_graph"

    def test_retryable_count_one_retry_graph(self):
        s = _state(
            error_code="LLM.RATE_LIMIT",
            retryable=True,
            error_msg="[graph_generate:LLM.RATE_LIMIT] 429",
            retry_count={"graph_generate": 1},
        )
        assert _route_after_graph_generate(s) == "retry_graph"

    def test_retryable_count_two_abort(self):
        """count >= 2 → abort（不再重试制图）。"""
        s = _state(
            error_code="LLM.UPSTREAM",
            retryable=True,
            error_msg="[graph_generate:LLM.UPSTREAM] 5xx",
            retry_count={"graph_generate": 2},
        )
        assert _route_after_graph_generate(s) == "abort"

    def test_not_retryable_abort(self):
        s = _state(
            error_code="LLM.REFUSED",
            retryable=False,
            error_msg="[graph_generate:LLM.REFUSED] quota exceeded",
            retry_count={"graph_generate": 0},
        )
        assert _route_after_graph_generate(s) == "abort"

    def test_not_retryable_context_overflow_abort(self):
        s = _state(
            error_code="LLM.CONTEXT_OVERFLOW",
            retryable=False,
            error_msg="[graph_generate:LLM.CONTEXT_OVERFLOW] max tokens",
            retry_count={"graph_generate": 0},
        )
        assert _route_after_graph_generate(s) == "abort"


# ═══════════════════════════════════════════════════════════════════
# 6. _route_after_code_search
# ═══════════════════════════════════════════════════════════════════


class TestRouteAfterCodeSearch:
    """5 条分支：有结果→has_results / 无错空结果→no_results / 可重试→retry / 超限→abort / 不可重试→abort。"""

    def test_has_results(self):
        s = _state(
            error_code=None,
            code_context=[{"file_path": "src/main.py", "code_snippet": "..."}],
        )
        assert _route_after_code_search(s) == "has_results"

    def test_no_error_no_results(self):
        s = _state(error_code=None, code_context=[])
        assert _route_after_code_search(s) == "no_results"

    def test_retryable_error_retry(self):
        s = _state(
            error_code="HTTP.UPSTREAM",
            retryable=True,
            retry_count={"code_search": 1},
            code_context=[],
        )
        assert _route_after_code_search(s) == "retry"

    def test_retryable_over_cap_abort(self):
        s = _state(
            error_code="HTTP.NETWORK",
            retryable=True,
            retry_count={"code_search": _GRAPH_RETRY_CAP_PER_NODE + 1},
            code_context=[],
        )
        assert _route_after_code_search(s) == "abort"

    def test_not_retryable_error_abort(self):
        """不可重试错（如 CLI.NOT_FOUND）→ abort。"""
        s = _state(
            error_code="CLI.NOT_FOUND",
            retryable=False,
            retry_count={"code_search": 0},
            code_context=[],
        )
        assert _route_after_code_search(s) == "abort"

    def test_not_retryable_auth_error_abort(self):
        s = _state(
            error_code="HTTP.AUTH",
            retryable=False,
            retry_count={"code_search": 0},
            code_context=[],
        )
        assert _route_after_code_search(s) == "abort"


# ═══════════════════════════════════════════════════════════════════
# 7. _route_after_code_gen
# ═══════════════════════════════════════════════════════════════════


class TestRouteAfterCodeGen:
    """6 条分支：无错+lint_ok / 无错+lint_in_error+retry / 无错+lint+force_test / 有错+retryable→retry / 有错+超限→abort / 有错+不可重试→abort。"""

    def test_no_error_lint_ok(self):
        s = _state(
            error_code=None,
            error_msg=None,
            code_changes=[{"file_path": "a.py", "lint_passed": True}],
        )
        assert _route_after_code_gen(s) == "lint_ok"

    def test_no_error_lint_in_msg_retry(self):
        """last_error 含 'lint' 但 last_error_code=None → 业务路由返回 retry。"""
        s = _state(
            error_code=None,
            error_msg="[code_gen] lint warning: unused import",
            retry_count={"code_gen": 0},
            code_changes=[{"file_path": "a.py", "lint_passed": False}],
        )
        assert _route_after_code_gen(s) == "retry"

    def test_no_error_lint_in_msg_force_test(self):
        """lint 在 error_msg 里 + retry_count >= 2 → force_test。"""
        s = _state(
            error_code=None,
            error_msg="[code_gen] lint failed",
            retry_count={"code_gen": 2},
            code_changes=[{"file_path": "a.py", "lint_passed": False}],
        )
        assert _route_after_code_gen(s) == "force_test"

    def test_error_code_retryable_within_cap_retry(self):
        """有 error_code + retryable + count <= 3 → retry。"""
        s = _state(
            error_code="HTTP.LINT_FAILED",
            retryable=True,
            error_msg="[code_gen:HTTP.LINT_FAILED] lint 不通过",
            retry_count={"code_gen": 1},
            code_changes=[],
        )
        assert _route_after_code_gen(s) == "retry"

    def test_error_code_retryable_over_cap_abort(self):
        s = _state(
            error_code="HTTP.LINT_FAILED",
            retryable=True,
            error_msg="[code_gen:HTTP.LINT_FAILED] lint 不通过",
            retry_count={"code_gen": _GRAPH_RETRY_CAP_PER_NODE + 1},
            code_changes=[],
        )
        assert _route_after_code_gen(s) == "abort"

    def test_error_code_not_retryable_abort(self):
        s = _state(
            error_code="LLM.REFUSED",
            retryable=False,
            error_msg="[code_gen:LLM.REFUSED] quota exceeded",
            retry_count={"code_gen": 0},
            code_changes=[],
        )
        assert _route_after_code_gen(s) == "abort"

    def test_error_code_context_overflow_abort(self):
        s = _state(
            error_code="LLM.CONTEXT_OVERFLOW",
            retryable=False,
            retry_count={"code_gen": 0},
            code_changes=[],
        )
        assert _route_after_code_gen(s) == "abort"


# ═══════════════════════════════════════════════════════════════════
# 8. _route_after_test_gen
# ═══════════════════════════════════════════════════════════════════


class TestRouteAfterTestGen:
    """无错 → run（交给 test_run 执行判定）/ 有错+可重试 → retry / 有错+不可重试或超限 → abort。"""

    def test_no_error_runs_test_run_node(self):
        s = _state(
            error_code=None,
            test_report={"run": {"passed": 5, "failed": 0}},
        )
        assert _route_after_test_gen(s) == "run"

    def test_no_error_fail_report_still_runs(self):
        """设计报告里的数字不再决定路由，真实执行在 test_run。"""
        s = _state(
            error_code=None,
            test_report={"run": {"passed": 3, "failed": 2}},
        )
        assert _route_after_test_gen(s) == "run"

    def test_no_error_no_report_still_runs(self):
        s = _state(error_code=None, test_report=None)
        assert _route_after_test_gen(s) == "run"

    def test_error_retryable_retry(self):
        s = _state(
            error_code="HTTP.UPSTREAM",
            retryable=True,
            retry_count={"test_gen": 1},
            test_report={"run": {"passed": 0, "failed": 0}},
        )
        assert _route_after_test_gen(s) == "retry"

    def test_error_retryable_over_cap_abort(self):
        s = _state(
            error_code="HTTP.UPSTREAM",
            retryable=True,
            retry_count={"test_gen": _GRAPH_RETRY_CAP_PER_NODE + 1},
            test_report=None,
        )
        assert _route_after_test_gen(s) == "abort"

    def test_error_not_retryable_abort(self):
        s = _state(
            error_code="HTTP.AUTH",
            retryable=False,
            retry_count={"test_gen": 0},
            test_report=None,
        )
        assert _route_after_test_gen(s) == "abort"

    def test_error_cli_exit_retryable_retry(self):
        """CLI.EXIT_ERROR 是可重试的（测试未通过也走这条）。"""
        s = _state(
            error_code="CLI.EXIT_ERROR",
            retryable=True,
            retry_count={"test_gen": 1},
            test_report={"run": {"passed": 2, "failed": 3}},
        )
        assert _route_after_test_gen(s) == "retry"


# ═══════════════════════════════════════════════════════════════════
# 9. dead_letter_drain_node
# ═══════════════════════════════════════════════════════════════════


class TestDeadLetterDrainNode:
    """死信落盘节点：按 error_code 去重写 JSONL + 截断队列。"""

    def test_empty_dead_letters(self):
        s = _state(dead_letters=[])
        out = dead_letter_drain_node(s)
        assert out["dead_letters"] == []
        # _state 没设 current_stage → None → dead_letter_drain_node 回退到 "end"
        assert out["current_stage"] == "end"

    def test_writes_unique_error_codes(self, tmp_path, monkeypatch):
        """同一 error_code 只写一次 JSONL。"""
        # monkeypatch orchestrator 模块里已导入的 dead_letter_record 引用
        from devflow import orchestrator as orc

        def _fake_record(err, *, state_snapshot=None, root_dir="./data/dead_letter"):
            path = tmp_path / "test.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a") as f:
                f.write(json.dumps({"code": err.code, "msg": err.message}) + "\n")
            return path

        monkeypatch.setattr(orc, "dead_letter_record", _fake_record)

        dead_letters = [
            {
                "error_code": "LLM.REFUSED",
                "error_message": "quota exceeded",
                "retryable": False,
                "snapshot": {},
                "extra": {},
                "cause_repr": None,
                "stage": "clarify",
            },
            {
                "error_code": "LLM.REFUSED",  # 重复 code，应该跳过
                "error_message": "quota exceeded again",
                "retryable": False,
                "snapshot": {},
                "extra": {},
                "cause_repr": None,
                "stage": "clarify",
            },
            {
                "error_code": "HTTP.AUTH",
                "error_message": "401 unauthorized",
                "retryable": False,
                "snapshot": {},
                "extra": {},
                "cause_repr": None,
                "stage": "code_gen",
            },
        ]
        s = _state(dead_letters=dead_letters, current_stage="code")
        out = dead_letter_drain_node(s)

        # 返回的 dead_letters 保持原样（截断逻辑只在 > 200 时触发）
        assert len(out["dead_letters"]) == 3
        assert out["current_stage"] == "code"

        # 落盘文件只有 2 行（去重后 LLM.REFUSED + HTTP.AUTH）
        jsonl_path = tmp_path / "test.jsonl"
        assert jsonl_path.exists()
        lines = jsonl_path.read_text().strip().split("\n")
        assert len(lines) == 2
        codes = [json.loads(line)["code"] for line in lines]
        assert "LLM.REFUSED" in codes
        assert "HTTP.AUTH" in codes

    def test_truncates_over_200(self, tmp_path, monkeypatch):
        """dead_letters 超过 200 条时截断到尾部 200。"""
        from devflow import orchestrator as orc

        monkeypatch.setattr(
            orc,
            "dead_letter_record",
            lambda *a, **kw: tmp_path / "dummy.jsonl",
        )

        dead_letters = [
            {
                "error_code": f"ERR.{i}",
                "error_message": f"err {i}",
                "retryable": False,
                "snapshot": {},
                "extra": {},
                "cause_repr": None,
                "stage": "test",
            }
            for i in range(250)
        ]
        s = _state(dead_letters=dead_letters)
        out = dead_letter_drain_node(s)
        assert len(out["dead_letters"]) == 200
        # 保留的是尾部 200 条（index 50..249）
        assert out["dead_letters"][0]["error_code"] == "ERR.50"
        assert out["dead_letters"][-1]["error_code"] == "ERR.249"

    def test_oserror_swallowed(self, tmp_path, monkeypatch):
        """落盘 OSError 不影响节点返回（避免失败套失败）。"""
        from devflow import orchestrator as orc

        def _raise_oserror(*a, **kw):
            raise OSError("disk full")

        monkeypatch.setattr(orc, "dead_letter_record", _raise_oserror)

        dead_letters = [
            {
                "error_code": "LLM.REFUSED",
                "error_message": "quota",
                "retryable": False,
                "snapshot": {},
                "extra": {},
                "cause_repr": None,
                "stage": "clarify",
            }
        ]
        s = _state(dead_letters=dead_letters, current_stage="clarify")
        # 不应该抛异常
        out = dead_letter_drain_node(s)
        assert out["dead_letters"] == dead_letters
        assert out["current_stage"] == "clarify"


# ═══════════════════════════════════════════════════════════════════
# 10. initial_state 完整性
# ═══════════════════════════════════════════════════════════════════


class TestInitialState:
    """确保 initial_state 包含所有 SPEC 5 新增字段。"""

    def test_has_all_error_fields(self):
        s = initial_state()
        assert "last_error" in s
        assert s["last_error"] is None
        assert "last_error_code" in s
        assert s["last_error_code"] is None
        assert "last_error_retryable" in s
        assert s["last_error_retryable"] is False
        assert "retry_count" in s
        assert s["retry_count"] == {}
        assert "dead_letters" in s
        assert s["dead_letters"] == []

    def test_has_business_fields(self):
        s = initial_state()
        assert s["current_stage"] == "clarify"
        assert s["messages"] == []
        assert s["code_context"] == []
        assert s["logic_graph"] is None
        assert s["code_changes"] == []
        assert s["test_report"] is None
        assert "opencode_sessions" in s
