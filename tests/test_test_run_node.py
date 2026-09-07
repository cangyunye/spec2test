"""执行闭环节点测试：apply_code / test_run 节点 + 路由函数。

不依赖 LLM：直接构造 state 调 async_version；测试执行用真实 tmp 项目 + 真实 pytest 子进程。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from devflow.config import settings
from devflow.nodes.test_run import (
    make_apply_code_node,
    make_test_run_node,
    route_after_code_apply,
    route_after_test_run,
)


# ═══════════════════════════════════════════════════════════════════
# apply_code 节点
# ═══════════════════════════════════════════════════════════════════

def _req(root: str | None) -> dict:
    return {"requirement": {"project_root": root}}


class TestApplyCodeNode:
    @pytest.mark.asyncio
    async def test_applies_diff_with_backup(self, tmp_path):
        f = tmp_path / "calc.py"
        f.write_text("v = 1\n", encoding="utf-8")
        node = make_apply_code_node()
        state = {
            **_req(str(tmp_path)),
            "code_changes": [
                {
                    "file_path": "calc.py",
                    "action": "update",
                    "diff": "--- a/calc.py\n+++ b/calc.py\n@@ -1,1 +1,1 @@\n-v = 1\n+v = 2\n",
                },
                {
                    "file_path": "newmod.py",
                    "action": "create",
                    "content_after": "NEW = 1\n",
                },
            ],
        }
        out = await node.async_version(state)  # type: ignore[arg-type]
        ca = out["code_apply"]
        assert ca["applied"] is True
        assert len(ca["files"]) == 2
        assert f.read_text(encoding="utf-8") == "v = 2\n"
        assert (tmp_path / "newmod.py").read_text(encoding="utf-8") == "NEW = 1\n"
        assert ca["backup_dir"] and (Path(ca["backup_dir"]) / "manifest.json").is_file()
        assert out["current_stage"] == "test"

    @pytest.mark.asyncio
    async def test_content_after_ignored_for_update(self, tmp_path):
        """content_after 仅对新建文件生效：update 走 diff，防止 mock 内容覆盖真实源码。"""
        f = tmp_path / "real.py"
        f.write_text("keep = 1\n", encoding="utf-8")
        node = make_apply_code_node()
        state = {
            **_req(str(tmp_path)),
            "code_changes": [
                {"file_path": "real.py", "action": "update", "content_after": "clobbered = True\n"},
            ],
        }
        out = await node.async_version(state)  # type: ignore[arg-type]
        assert out["code_apply"]["applied"] is False
        assert f.read_text(encoding="utf-8") == "keep = 1\n"
        assert out["last_error_code"] == "EXEC.APPLY_FAILED"
        assert out["last_error_retryable"] is True

    @pytest.mark.asyncio
    async def test_diff_mismatch_is_retryable(self, tmp_path):
        (tmp_path / "x.py").write_text("real = 1\n", encoding="utf-8")
        node = make_apply_code_node()
        state = {
            **_req(str(tmp_path)),
            "code_changes": [
                {"file_path": "x.py", "action": "update",
                 "diff": "--- a/x.py\n+++ b/x.py\n@@ -1,1 +1,1 @@\n-wrong\n+right\n"},
            ],
        }
        out = await node.async_version(state)  # type: ignore[arg-type]
        assert out["last_error_code"] == "EXEC.APPLY_FAILED"
        assert out["last_error_retryable"] is True
        assert out["code_apply"]["applied"] is False
        assert "EXEC.APPLY_FAILED" in out["code_apply"]["reason"]

    @pytest.mark.asyncio
    async def test_skip_when_disabled(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "APPLY_CODE_ENABLED", False)
        node = make_apply_code_node()
        state = {
            **_req(str(tmp_path)),
            "code_changes": [{"file_path": "a.py", "action": "update", "diff": "--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,1 @@\n-a\n+b\n"}],
        }
        out = await node.async_version(state)  # type: ignore[arg-type]
        assert out["code_apply"]["applied"] is False
        assert "APPLY_CODE_ENABLED" in out["code_apply"]["reason"]
        assert out["current_stage"] == "test"
        assert out.get("last_error_code") is None  # 跳过不是错误

    @pytest.mark.asyncio
    async def test_skip_when_root_missing(self, tmp_path):
        node = make_apply_code_node()
        state = {
            **_req(str(tmp_path / "nope")),
            "code_changes": [{"file_path": "a.py", "action": "update", "diff": "x"}],
        }
        out = await node.async_version(state)  # type: ignore[arg-type]
        assert out["code_apply"]["applied"] is False
        assert out.get("last_error_code") is None

    @pytest.mark.asyncio
    async def test_empty_changes_passthrough(self):
        node = make_apply_code_node()
        out = await node.async_version({**_req("/w"), "code_changes": []})  # type: ignore[arg-type]
        assert out["code_apply"]["applied"] is False
        assert out["current_stage"] == "test"


class TestRouteAfterCodeApply:
    def test_retry_then_continue(self):
        from devflow.orchestrator import _route_after_code_apply

        retryable = {
            "last_error_code": "EXEC.APPLY_FAILED",
            "last_error_retryable": True,
            "retry_count": {"apply_code": 0},
        }
        # 业务路由只看 retryable；orchestrator wrapper 负责 cap=1
        assert route_after_code_apply(retryable) == "retry"
        assert _route_after_code_apply(retryable) == "retry"
        # 回炉一次后不再回炉，降级继续
        exhausted = {**retryable, "retry_count": {"apply_code": 2}}
        assert _route_after_code_apply(exhausted) == "continue"
        non_retry = {**retryable, "last_error_retryable": False}
        assert _route_after_code_apply(non_retry) == "continue"
        assert _route_after_code_apply({}) == "continue"


# ═══════════════════════════════════════════════════════════════════
# test_run 节点
# ═══════════════════════════════════════════════════════════════════

def _project_with_tests(tmp_path: Path, body: str) -> Path:
    (tmp_path / "tests").mkdir(exist_ok=True)
    (tmp_path / "tests" / "test_x.py").write_text(body, encoding="utf-8")
    return tmp_path


def _run_state(root: Path, **kw) -> dict:
    base = {
        "requirement": {"project_root": str(root)},
        "code_apply": {"applied": True, "files": [], "backup_dir": None},
        "code_changes": [{"file_path": "calc.py", "action": "update", "diff": ""}],
        "test_report": {"session_id": None, "test_cases": [], "run": {"passed": 3, "failed": 0, "skipped": 0}, "target_symbols": []},
        "retry_count": {},
    }
    base.update(kw)
    return base


class TestTestRunNode:
    @pytest.mark.asyncio
    async def test_passing_execution(self, tmp_path):
        _project_with_tests(tmp_path, "def test_ok():\n    assert True\n")
        node = make_test_run_node()
        out = await node.async_version(_run_state(tmp_path))  # type: ignore[arg-type]
        run = out["test_report"]["run"]
        assert run["executed"] is True
        assert run["failed"] == 0 and run["total"] >= 1
        assert run["passed"] >= 1
        assert out["code_changes"][0]["test_passed"] is True
        assert out["current_stage"] == "review"
        assert out["test_failure"] is None

    @pytest.mark.asyncio
    async def test_failing_execution_sets_summary(self, tmp_path):
        _project_with_tests(tmp_path, "def test_bad():\n    assert 1 == 2, '值不等'\n")
        node = make_test_run_node()
        out = await node.async_version(_run_state(tmp_path))  # type: ignore[arg-type]
        run = out["test_report"]["run"]
        assert run["executed"] is True and run["failed"] >= 1
        assert run["failures"]
        assert out["code_changes"][0]["test_passed"] is False
        assert out["current_stage"] == "test"
        assert "test_bad" in out["test_failure"]
        assert "值不等" in out["test_failure"]
        assert out["retry_count"]["test_run"] == 1
        # 设计态数字被真实执行覆盖
        assert run["passed"] == 0

    @pytest.mark.asyncio
    async def test_skip_when_not_applied(self, tmp_path):
        node = make_test_run_node()
        out = await node.async_version(
            _run_state(tmp_path, code_apply={"applied": False, "reason": "落盘关闭"})
        )  # type: ignore[arg-type]
        run = out["test_report"]["run"]
        assert run["executed"] is False
        assert "落盘关闭" in run["skip_reason"]
        assert out["current_stage"] == "review"

    @pytest.mark.asyncio
    async def test_skip_when_disabled(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "TEST_RUN_ENABLED", False)
        node = make_test_run_node()
        out = await node.async_version(_run_state(tmp_path))  # type: ignore[arg-type]
        assert out["test_report"]["run"]["executed"] is False
        assert out["current_stage"] == "review"

    @pytest.mark.asyncio
    async def test_no_tests_collected_is_not_pass(self, tmp_path):
        """没有收集到用例：executed=True 但不声称 test_passed。"""
        node = make_test_run_node()
        out = await node.async_version(_run_state(tmp_path))  # type: ignore[arg-type]
        run = out["test_report"]["run"]
        assert run["executed"] is True
        assert run["total"] == 0
        assert out["code_changes"][0]["test_passed"] is None
        assert out["current_stage"] == "review"


class TestRouteAfterTestRun:
    def test_matrix(self):
        ok = {"test_report": {"run": {"executed": True, "failed": 0, "errors": 0, "passed": 3}}}
        assert route_after_test_run(ok) == "test_ok"

        fail = {
            "test_report": {"run": {"executed": True, "failed": 2, "errors": 0, "passed": 1}},
            "retry_count": {"test_run": 1},
        }
        assert route_after_test_run(fail) == "test_fail"

        exhausted = {
            "test_report": {"run": {"executed": True, "failed": 1, "errors": 0, "passed": 0}},
            "retry_count": {"test_run": settings.TEST_RUN_MAX_FIX_ROUNDS + 1},
        }
        assert route_after_test_run(exhausted) == "review_failed"

        skipped = {"test_report": {"run": {"executed": False, "skip_reason": "x"}}}
        assert route_after_test_run(skipped) == "skip"
        assert route_after_test_run({}) == "skip"
