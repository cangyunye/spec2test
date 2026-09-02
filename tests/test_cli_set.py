"""CLI --set 参数解析测试（评审稿 §10 TC-C12 核心落地版）。

覆盖：
  - _parse_assignment: k=v / JSON literal（数组/对象/bool/数字）
  - apply_assignments: 覆盖已有字段 + 清空（--set target_modules=[]）
  - io_constraints.<sub> 点路径支持
运行: pytest -v tests/test_cli_set.py
"""
from __future__ import annotations

import pytest

from devflow.cli import _parse_assignment, apply_assignments
from devflow.schemas import empty_requirement


class TestParseAssignment:
    def test_plain_string(self):
        key, val = _parse_assignment("req_type=bug_fix")
        assert key == "req_type"
        assert val == "bug_fix"

    def test_json_array(self):
        key, val = _parse_assignment('target_modules=["src/a.py","src/b.py"]')
        assert key == "target_modules"
        assert val == ["src/a.py", "src/b.py"]

    def test_json_object(self):
        key, val = _parse_assignment('io_constraints={"input":"x","output":"y"}')
        assert key == "io_constraints"
        assert val == {"input": "x", "output": "y"}

    def test_json_bool_and_number(self):
        assert _parse_assignment("existing_code_accessible=true") == (
            "existing_code_accessible", True,
        )
        assert _parse_assignment("latency=100") == ("latency", 100)

    def test_empty_array_for_clear(self):
        assert _parse_assignment("target_modules=[]") == ("target_modules", [])

    def test_missing_equals_raises(self):
        with pytest.raises(ValueError, match="key=value"):
            _parse_assignment("target_modules")

    def test_empty_key_raises(self):
        with pytest.raises(ValueError, match="key 不能为空"):
            _parse_assignment("=abc")


class TestApplyAssignments:
    def test_overwrites_existing_field(self):
        req = empty_requirement()
        out = apply_assignments(req, ["req_type=bug_fix", "project_root=/tmp/app"])
        assert out["req_type"] == "bug_fix"
        assert out["project_root"] == "/tmp/app"

    def test_clear_with_empty_array(self):
        req = empty_requirement()
        req["target_modules"] = ["auth", "api"]
        out = apply_assignments(req, ["target_modules=[]"])
        assert out["target_modules"] == []

    def test_dot_path_for_io_constraints(self):
        req = empty_requirement()
        out = apply_assignments(req, ["io_constraints.input=POST /login"])
        assert out["io_constraints"]["input"] == "POST /login"
        assert out["io_constraints"]["output"] == ""  # 未动

    def test_does_not_mutate_input(self):
        req = empty_requirement()
        before = dict(req)
        apply_assignments(req, ["req_type=bug_fix"])
        assert req == before  # 深拷贝语义
