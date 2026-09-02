"""P2 check-providers 后端自检测试。

覆盖：
  - probe_codegraph：二进制/索引探测
  - probe_archify：node/npx 探测
  - probe_opencode：URL 连通性探测（不抛异常）
  - check_providers_all：汇总报告，字段齐全
运行: pytest -v tests/test_check_providers.py
"""
from __future__ import annotations

import pytest

from devflow.providers.check import (
    check_providers_all,
    probe_archify,
    probe_codegraph,
    probe_opencode,
)


class TestProbeCodegraph:
    def test_missing_bin_reports_not_ok(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "devflow.providers.check.shutil.which", lambda x: None
        )
        r = probe_codegraph(str(tmp_path))
        assert r["ok"] is False
        assert r["name"] == "codegraph"
        assert "not found" in r["detail"].lower() or "未找到" in r["detail"]

    def test_bin_ok_missing_index_warns(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "devflow.providers.check.shutil.which", lambda x: "/bin/sh"
        )
        r = probe_codegraph(str(tmp_path))
        assert r["bin_ok"] is True
        assert r["index_ok"] is False
        assert r["ok"] is False  # 索引缺失算不 ok（提示先 init）

    def test_full_ok(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "devflow.providers.check.shutil.which", lambda x: "/bin/sh"
        )
        (tmp_path / ".codegraph").mkdir()
        r = probe_codegraph(str(tmp_path))
        assert r["ok"] is True
        assert r["index_ok"] is True


class TestProbeArchify:
    def test_node_and_npx_present(self, monkeypatch):
        monkeypatch.setattr(
            "devflow.providers.check.shutil.which", lambda x: f"/usr/bin/{x}"
        )
        r = probe_archify()
        assert r["name"] == "archify"
        assert r["ok"] is True
        assert r["node_ok"] is True
        assert r["npx_ok"] is True

    def test_missing_node_reports_not_ok(self, monkeypatch):
        monkeypatch.setattr(
            "devflow.providers.check.shutil.which",
            lambda x: None if x == "node" else "/usr/bin/npx",
        )
        r = probe_archify()
        assert r["ok"] is False
        assert r["node_ok"] is False


class TestProbeOpencode:
    @pytest.mark.asyncio
    async def test_unconfigured_reports_not_ok(self, monkeypatch):
        monkeypatch.setattr(
            "devflow.providers.check.settings.OPENCODE_BASE_URL", ""
        )
        r = await probe_opencode()
        assert r["ok"] is False
        assert "未配置" in r["detail"] or "not configured" in r["detail"].lower()


class TestCheckProvidersAll:
    def test_returns_full_report(self, monkeypatch):
        async def fake_probe_opencode():
            return {"name": "opencode", "ok": False, "detail": "z"}

        monkeypatch.setattr(
            "devflow.providers.check.probe_codegraph",
            lambda proot: {"name": "codegraph", "ok": False, "detail": "x"},
        )
        monkeypatch.setattr(
            "devflow.providers.check.probe_archify",
            lambda: {"name": "archify", "ok": True, "detail": "y"},
        )
        monkeypatch.setattr(
            "devflow.providers.check.probe_opencode", fake_probe_opencode
        )
        reports = check_providers_all("/tmp/prj")
        assert len(reports) == 3
        names = [r["name"] for r in reports]
        assert names == ["codegraph", "archify", "opencode"]
        # 每个报告都有统一字段
        for r in reports:
            assert set(["name", "ok", "detail"]).issubset(r.keys())
