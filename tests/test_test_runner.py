"""test_runner 单元测试：真实子进程跑 pytest + junit 解析。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from devflow.test_runner import run_pytest


def _make_project(tmp_path: Path, test_body: str) -> Path:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_it.py").write_text(test_body, encoding="utf-8")
    return tmp_path


class TestRunPytest:
    @pytest.mark.asyncio
    async def test_all_pass(self, tmp_path):
        _make_project(
            tmp_path,
            "def test_ok():\n    assert True\n",
        )
        rep = await run_pytest(tmp_path, timeout_sec=120)
        assert rep["executed"] is True
        assert rep["failed"] == 0 and rep["errors"] == 0
        assert rep["total"] >= 1
        assert rep["error"] is None
        assert "passed" in rep["logs"]

    @pytest.mark.asyncio
    async def test_failures_captured(self, tmp_path):
        _make_project(
            tmp_path,
            "def test_bad():\n    assert 1 == 2, '数值不相等'\n",
        )
        rep = await run_pytest(tmp_path, timeout_sec=120)
        assert rep["executed"] is True
        assert rep["failed"] >= 1
        assert rep["failures"], "失败明细不应为空"
        assert "test_bad" in rep["failures"][0]["id"]
        assert "数值不相等" in rep["failures"][0]["message"]

    @pytest.mark.asyncio
    async def test_no_tests_collected(self, tmp_path):
        # 目录里没有测试文件：executed=True 但 total=0
        rep = await run_pytest(tmp_path, timeout_sec=120)
        assert rep["executed"] is True
        assert rep["total"] == 0
        assert "未收集到任何测试用例" in rep["logs"]

    @pytest.mark.asyncio
    async def test_project_root_missing(self, tmp_path):
        rep = await run_pytest(tmp_path / "nope")
        assert rep["executed"] is False
        assert "不存在" in (rep["error"] or "")

    @pytest.mark.asyncio
    async def test_pytest_missing_env(self, tmp_path, monkeypatch):
        """python_exe 指向不存在的解释器 → executed=False 带原因。"""
        _make_project(tmp_path, "def test_ok():\n    assert True\n")
        rep = await run_pytest(
            tmp_path,
            python_exe=str(tmp_path / "no-such-python"),
            timeout_sec=60,
        )
        assert rep["executed"] is False
        assert rep["error"]

    @pytest.mark.asyncio
    async def test_timeout(self, tmp_path):
        _make_project(
            tmp_path,
            "import time\n\ndef test_hang():\n    time.sleep(30)\n",
        )
        rep = await run_pytest(tmp_path, timeout_sec=3)
        assert rep["executed"] is False
        assert "超时" in (rep["error"] or "")

    @pytest.mark.asyncio
    async def test_paths_scope(self, tmp_path):
        """paths 只跑指定目录。"""
        _make_project(tmp_path, "def test_a():\n    assert True\n")
        other = tmp_path / "other"
        other.mkdir()
        (other / "test_b.py").write_text("def test_b():\n    assert False\n", encoding="utf-8")
        rep = await run_pytest(tmp_path, paths=["tests"], timeout_sec=120)
        assert rep["executed"] is True
        assert rep["failed"] == 0
