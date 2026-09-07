"""code_apply 单元测试：unified diff 解析、预检、落盘、备份回滚。"""
from __future__ import annotations

from pathlib import Path

import pytest

from devflow.code_apply import (
    DiffApplyError,
    apply_diff,
    apply_patches,
    parse_unified_diff,
    patch_from_content,
    rollback,
)


# ═══════════════════════════════════════════════════════════════════
# 解析
# ═══════════════════════════════════════════════════════════════════

class TestParse:
    def test_simple_modify(self):
        diff = (
            "--- a/src/calc.py\n"
            "+++ b/src/calc.py\n"
            "@@ -1,3 +1,4 @@\n"
            " def add(a, b):\n"
            "-    return a + b\n"
            "+    total = a + b\n"
            "+    return total\n"
            " \n"
        )
        patches = parse_unified_diff(diff)
        assert len(patches) == 1
        p = patches[0]
        assert p.path == "src/calc.py"
        assert not p.is_new and not p.is_delete
        assert len(p.hunks) == 1
        assert p.hunks[0].old_start == 1
        assert p.hunks[0].old_lines == ["def add(a, b):", "    return a + b", ""]
        assert p.hunks[0].new_lines == [
            "def add(a, b):", "    total = a + b", "    return total", "",
        ]

    def test_new_file(self):
        diff = (
            "--- /dev/null\n"
            "+++ b/tests/test_new.py\n"
            "@@ -0,0 +1,2 @@\n"
            "+def test_ok():\n"
            "+    assert True\n"
        )
        p = parse_unified_diff(diff)[0]
        assert p.is_new and p.path == "tests/test_new.py"

    def test_delete_file(self):
        diff = (
            "--- a/legacy.py\n"
            "+++ /dev/null\n"
            "@@ -1,2 +0,0 @@\n"
            "-old = True\n"
            "-\n"
        )
        p = parse_unified_diff(diff)[0]
        assert p.is_delete and p.path == "legacy.py"

    def test_multi_file(self):
        diff = (
            "diff --git a/a.py b/a.py\n"
            "index 111..222 100644\n"
            "--- a/a.py\n"
            "+++ b/a.py\n"
            "@@ -1,1 +1,1 @@\n"
            "-x = 1\n"
            "+x = 2\n"
            "diff --git a/b.py b/b.py\n"
            "--- a/b.py\n"
            "+++ b/b.py\n"
            "@@ -1,1 +1,2 @@\n"
            " y = 1\n"
            "+z = 2\n"
        )
        patches = parse_unified_diff(diff)
        assert [p.path for p in patches] == ["a.py", "b.py"]

    def test_no_prefix_paths(self):
        diff = (
            "--- src/main.py\n"
            "+++ src/main.py\n"
            "@@ -1,1 +1,1 @@\n"
            "-a = 1\n"
            "+a = 2\n"
        )
        assert parse_unified_diff(diff)[0].path == "src/main.py"

    def test_empty_diff_raises(self):
        with pytest.raises(DiffApplyError):
            parse_unified_diff("")
        with pytest.raises(DiffApplyError):
            parse_unified_diff("这不是 diff")

    def test_unsafe_path_rejected(self):
        for bad in ("/etc/passwd", "../../etc/passwd"):
            diff = f"--- {bad}\n+++ {bad}\n@@ -1,1 +1,1 @@\n-a\n+b\n"
            with pytest.raises(DiffApplyError):
                parse_unified_diff(diff)


# ═══════════════════════════════════════════════════════════════════
# 应用
# ═══════════════════════════════════════════════════════════════════

class TestApply:
    def test_modify_existing(self, tmp_path):
        f = tmp_path / "calc.py"
        f.write_text("def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n", encoding="utf-8")
        diff = (
            "--- a/calc.py\n"
            "+++ b/calc.py\n"
            "@@ -1,2 +1,3 @@\n"
            " def add(a, b):\n"
            "-    return a + b\n"
            "+    if a == 0:\n"
            "+        return b\n"
            "+    return a + b\n"
        )
        rep = apply_diff(tmp_path, diff, backup=False)
        assert rep["applied"] and rep["files"][0]["ok"]
        assert rep["files"][0]["action"] == "update"
        text = f.read_text(encoding="utf-8")
        assert text.startswith("def add(a, b):\n    if a == 0:\n        return b\n    return a + b\n")
        assert "def sub" in text  # 后文不动

    def test_fuzzy_line_drift(self, tmp_path):
        """@@ 行号漂移时按上下文就近匹配。"""
        f = tmp_path / "m.py"
        f.write_text("x = 1\nx = 2\nx = 3\nold()\n", encoding="utf-8")
        diff = (
            "--- a/m.py\n"
            "+++ b/m.py\n"
            "@@ -99,1 +99,1 @@\n"  # 错误行号
            "-old()\n"
            "+new()\n"
        )
        apply_diff(tmp_path, diff, backup=False)
        assert f.read_text(encoding="utf-8") == "x = 1\nx = 2\nx = 3\nnew()\n"

    def test_create_and_delete(self, tmp_path):
        old = tmp_path / "legacy.py"
        old.write_text("gone = True\n", encoding="utf-8")
        diff = (
            "--- /dev/null\n"
            "+++ b/pkg/new.py\n"
            "@@ -0,0 +1,1 @@\n"
            "+VALUE = 42\n"
            "--- a/legacy.py\n"
            "+++ /dev/null\n"
            "@@ -1,1 +0,0 @@\n"
            "-gone = True\n"
        )
        rep = apply_diff(tmp_path, diff, backup=False)
        assert [f["action"] for f in rep["files"]] == ["create", "delete"]
        assert (tmp_path / "pkg/new.py").read_text(encoding="utf-8") == "VALUE = 42\n"
        assert not old.exists()

    def test_no_eof_newline_preserved(self, tmp_path):
        f = tmp_path / "n.py"
        f.write_text("a = 1", encoding="utf-8")  # 无行尾换行
        diff = (
            "--- a/n.py\n"
            "+++ b/n.py\n"
            "@@ -1 +1 @@\n"
            "-a = 1\n"
            "+a = 2\n"
            "\\ No newline at end of file\n"
        )
        apply_diff(tmp_path, diff, backup=False)
        assert f.read_text(encoding="utf-8") == "a = 2"

    def test_content_after_full_write(self, tmp_path):
        (tmp_path / "f.py").write_text("old\n", encoding="utf-8")
        patches = [patch_from_content("f.py", "brand new content\n")]
        rep = apply_patches(tmp_path, patches, backup=False)
        assert rep["applied"]
        assert (tmp_path / "f.py").read_text(encoding="utf-8") == "brand new content\n"

    def test_context_mismatch_all_or_nothing(self, tmp_path):
        """预检失败 → 一个文件都不能被改动。"""
        (tmp_path / "good.py").write_text("keep = 1\n", encoding="utf-8")
        (tmp_path / "bad.py").write_text("real = 1\n", encoding="utf-8")
        diff = (
            "--- a/good.py\n"
            "+++ b/good.py\n"
            "@@ -1,1 +1,1 @@\n"
            "-keep = 1\n"
            "-keep = 2\n"
            "--- a/bad.py\n"
            "+++ b/bad.py\n"
            "@@ -1,1 +1,1 @@\n"
            "-wrong = 999\n"
            "+fixed = 1\n"
        )
        with pytest.raises(DiffApplyError) as ei:
            apply_diff(tmp_path, diff, backup=False)
        assert "bad.py" in str(ei.value)
        assert (tmp_path / "good.py").read_text(encoding="utf-8") == "keep = 1\n"
        assert (tmp_path / "bad.py").read_text(encoding="utf-8") == "real = 1\n"

    def test_missing_target_file(self, tmp_path):
        diff = (
            "--- a/ghost.py\n"
            "+++ b/ghost.py\n"
            "@@ -1,1 +1,1 @@\n"
            "-x\n"
            "+y\n"
        )
        with pytest.raises(DiffApplyError, match="目标文件不存在"):
            apply_diff(tmp_path, diff, backup=False)


# ═══════════════════════════════════════════════════════════════════
# 备份 + 回滚
# ═══════════════════════════════════════════════════════════════════

class TestBackupRollback:
    def test_backup_and_rollback(self, tmp_path):
        f = tmp_path / "calc.py"
        f.write_text("v = 1\n", encoding="utf-8")
        diff = (
            "--- a/calc.py\n"
            "+++ b/calc.py\n"
            "@@ -1,1 +1,1 @@\n"
            "-v = 1\n"
            "+v = 2\n"
        )
        rep = apply_diff(tmp_path, diff)
        assert f.read_text(encoding="utf-8") == "v = 2\n"
        bdir = rep["backup_dir"]
        assert bdir and Path(bdir).is_dir()
        assert (Path(bdir) / "manifest.json").is_file()
        assert (Path(bdir) / "calc.py").read_text(encoding="utf-8") == "v = 1\n"

        rollback(tmp_path, bdir)
        assert f.read_text(encoding="utf-8") == "v = 1\n"

    def test_rollback_removes_created_file(self, tmp_path):
        diff = (
            "--- /dev/null\n"
            "+++ b/created.py\n"
            "@@ -0,0 +1,1 @@\n"
            "+fresh = True\n"
        )
        rep = apply_diff(tmp_path, diff)
        assert (tmp_path / "created.py").is_file()
        rollback(tmp_path, rep["backup_dir"])
        assert not (tmp_path / "created.py").exists()
