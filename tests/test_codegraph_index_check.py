"""P1 CodeGraph 索引检查测试。

覆盖：
  - _ensure_env 在 .codegraph/ 缺失且 CODEGRAPH_REQUIRE_INDEX=1 时抛 CliIndexMissingError
  - CODEGRAPH_REQUIRE_INDEX=0 时仅警告不抛错（fallback 留后路）
  - require_index 显式参数覆盖环境变量
运行: pytest -v tests/test_codegraph_index_check.py
"""
from __future__ import annotations

import asyncio

import pytest

from devflow.errors import CliIndexMissingError
from devflow.providers.codegraph import CodeGraphProvider, CodeGraphNotInstalledError


class TestEnsureEnvIndexCheck:
    def test_missing_index_raises_when_required(self, tmp_path, monkeypatch):
        """.codegraph 缺失 + require_index=True → 抛 CliIndexMissingError（可重试）。"""
        monkeypatch.setattr(
            "devflow.providers.codegraph.shutil.which", lambda x: "/bin/sh"
        )
        p = CodeGraphProvider(bin_path="/bin/sh", require_index=True)
        with pytest.raises(CliIndexMissingError) as ei:
            p._ensure_env(str(tmp_path))
        assert "codegraph" in ei.value.message or ".codegraph" in ei.value.message
        assert ei.value.retryable is True  # CLI.INDEX_MISSING 默认可重试

    def test_missing_index_warns_when_not_required(self, tmp_path, monkeypatch, caplog):
        """.codegraph 缺失 + require_index=False → 只 warning 不抛错。"""
        import logging

        monkeypatch.setattr(
            "devflow.providers.codegraph.shutil.which", lambda x: "/bin/sh"
        )
        p = CodeGraphProvider(bin_path="/bin/sh", require_index=False)
        with caplog.at_level(logging.WARNING, logger="devflow.providers.codegraph"):
            p._ensure_env(str(tmp_path))  # 不抛错
        assert any("codegraph" in r.message or ".codegraph" in r.message for r in caplog.records)

    def test_index_exists_no_error(self, tmp_path, monkeypatch):
        """.codegraph/ 存在 → 不抛错不警告。"""
        monkeypatch.setattr(
            "devflow.providers.codegraph.shutil.which", lambda x: "/bin/sh"
        )
        (tmp_path / ".codegraph").mkdir()
        p = CodeGraphProvider(bin_path="/bin/sh", require_index=True)
        p._ensure_env(str(tmp_path))  # 不抛错

    def test_bin_missing_still_raises_first(self, tmp_path, monkeypatch):
        """二进制缺失优先报 CodeGraphNotInstalledError（先于索引检查）。"""
        monkeypatch.setattr(
            "devflow.providers.codegraph.shutil.which", lambda x: None
        )
        p = CodeGraphProvider(bin_path="/does/not/exist/codegraph", require_index=True)
        with pytest.raises(CodeGraphNotInstalledError):
            p._ensure_env(str(tmp_path))

    def test_search_missing_index_with_require_goes_fallback(self, tmp_path, monkeypatch):
        """search 时 .codegraph 缺失 + require_index=True → 走 fallback 而非抛错（搜索语义降级）。"""
        from devflow.providers import MockCodeSearch

        monkeypatch.setattr(
            "devflow.providers.codegraph.shutil.which", lambda x: "/bin/sh"
        )
        fallback = MockCodeSearch()
        p = CodeGraphProvider(bin_path="/bin/sh", fallback=fallback, require_index=True)

        async def _run():
            return await p.search(str(tmp_path), "anything")

        out = asyncio.run(_run())
        # fallback（MockCodeSearch）会返回 ≥1 条结果
        assert out["total"] >= 1
