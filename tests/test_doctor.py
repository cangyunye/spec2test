"""首次启动自检与引导（devflow.doctor）测试。

覆盖：
  - github_latest_release：成功 / 超时 / 非 200 → None（超时降级第一环）
  - parse_multi_selection：多选编号解析（越界 / 去重 / 全角逗号 / 非法输入）
  - placeholder_providers：占位 Key 检测（JSON 多 provider + 旧版单 provider）
  - env_updates_from_probes / apply_env_updates：按已装服务配置 .env，
    不覆盖用户自定义值，缺 .env 时从 .env.example 创建
  - _download_file：成功 / 超时清理半成品 / 404
  - install_codegraph：API 超时 → manual；完整下载+SHA256 校验 → installed；
    校验不符 → manual
  - first_run_notice / collect_report / missing_install_items / run_setup 冒烟
运行: pytest -v tests/test_doctor.py
"""
from __future__ import annotations

import hashlib
import io
import os
import tarfile
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from rich.console import Console

from devflow import doctor
from devflow.doctor import (
    _download_file,
    apply_env_updates,
    collect_report,
    env_updates_from_probes,
    first_run_notice,
    github_latest_release,
    install_codegraph,
    missing_install_items,
    parse_multi_selection,
    placeholder_providers,
    run_setup,
)


def _silent_console() -> Console:
    return Console(file=io.StringIO(), width=200)


def _probe(name: str, ok: bool, **extra) -> dict:
    return {"name": name, "ok": ok, "detail": "", **extra}


# ═══════════════════════════════════════════════════════════════════
# GitHub 最新版本检测
# ═══════════════════════════════════════════════════════════════════


class TestGithubLatestRelease:
    def test_success(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert "api.github.com" in str(request.url)
            return httpx.Response(200, json={"tag_name": "v1.6.0"})

        rel = github_latest_release("foo/bar", transport=httpx.MockTransport(handler))
        assert rel == {"tag": "v1.6.0", "releases_url": "https://github.com/foo/bar/releases"}

    def test_timeout_returns_none(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("boom", request=request)

        assert github_latest_release("foo/bar", transport=httpx.MockTransport(handler)) is None

    def test_non_200_returns_none(self):
        handler = lambda request: httpx.Response(403)  # noqa: E731 - 限流场景
        assert github_latest_release("foo/bar", transport=httpx.MockTransport(handler)) is None

    def test_missing_tag_returns_none(self):
        handler = lambda request: httpx.Response(200, json={})  # noqa: E731
        assert github_latest_release("foo/bar", transport=httpx.MockTransport(handler)) is None


# ═══════════════════════════════════════════════════════════════════
# 多选输入解析
# ═══════════════════════════════════════════════════════════════════


class TestParseMultiSelection:
    def test_basic(self):
        assert parse_multi_selection("1,3", 3) == [0, 2]

    def test_empty_means_skip(self):
        assert parse_multi_selection("", 3) == []
        assert parse_multi_selection("   ", 3) == []

    def test_fullwidth_comma_and_spaces(self):
        assert parse_multi_selection("1， 2", 3) == [0, 1]

    def test_out_of_range_and_garbage_ignored(self):
        assert parse_multi_selection("9,abc,2", 3) == [1]
        assert parse_multi_selection("0,-1", 3) == []

    def test_dedupe(self):
        assert parse_multi_selection("2,2,2", 3) == [1]


# ═══════════════════════════════════════════════════════════════════
# 占位 Key 检测
# ═══════════════════════════════════════════════════════════════════


class TestPlaceholderProviders:
    def test_json_mixed_providers(self):
        env = {"LLM_PROVIDERS_JSON": """[
            {"name": "real", "api_key": "sk-real-key", "base_url": "u", "model": "m"},
            {"name": "fake", "api_key": "sk-your-key", "base_url": "u", "model": "m"}
        ]"""}
        assert placeholder_providers(env) == ["fake"]

    def test_legacy_placeholder(self):
        assert placeholder_providers({"LLM_API_KEY": "sk-your-deepseek-key"}) == ["primary(LLM_API_KEY)"]
        assert placeholder_providers({"LLM_API_KEY": "sk-dummy-key"}) == ["primary(LLM_API_KEY)"]

    def test_legacy_real_key(self):
        assert placeholder_providers({"LLM_API_KEY": "sk-real"}) == []

    def test_invalid_json_reported(self):
        assert placeholder_providers({"LLM_PROVIDERS_JSON": "{not-json"}) == [
            "LLM_PROVIDERS_JSON(JSON解析失败)"
        ]

    def test_missing_env_means_unconfigured(self):
        assert placeholder_providers({}) == ["primary(LLM_API_KEY)"]


# ═══════════════════════════════════════════════════════════════════
# .env 配置建议与写入
# ═══════════════════════════════════════════════════════════════════


def _probes_all_ok():
    return [
        _probe("codegraph", True, bin_ok=True, index_ok=False),
        _probe("archify", True, node_ok=True, npx_ok=True),
        _probe("opencode", True),
    ]


class TestEnvUpdatesFromProbes:
    def test_opencode_and_archify_proposed_on_defaults(self):
        updates = env_updates_from_probes(_probes_all_ok(), {})
        keys = {u["key"]: u["new"] for u in updates}
        assert keys["CODE_EDIT_PROVIDER"] == "opencode"
        assert keys["TEST_GEN_PROVIDER"] == "opencode"
        assert keys["CODE_GRAPH_RENDER_PROVIDER"] == "archify"

    def test_codegraph_index_ready_wins_search(self):
        probes = [
            _probe("codegraph", True, bin_ok=True, index_ok=True),
            _probe("archify", False),
            _probe("opencode", True),
        ]
        keys = {u["key"]: u["new"] for u in env_updates_from_probes(probes, {})}
        assert keys["CODE_SEARCH_PROVIDER"] == "codegraph"

    def test_bin_without_index_falls_to_opencode_search(self):
        probes = [
            _probe("codegraph", False, bin_ok=True, index_ok=False),
            _probe("archify", False),
            _probe("opencode", True),
        ]
        keys = {u["key"]: u["new"] for u in env_updates_from_probes(probes, {})}
        assert keys["CODE_SEARCH_PROVIDER"] == "opencode"

    def test_user_customized_value_not_touched(self):
        current = {"CODE_EDIT_PROVIDER": "my-own-backend", "CODE_GRAPH_RENDER_PROVIDER": "mermaid"}
        updates = env_updates_from_probes(_probes_all_ok(), current)
        keys = {u["key"] for u in updates}
        assert "CODE_EDIT_PROVIDER" not in keys
        assert "CODE_GRAPH_RENDER_PROVIDER" in keys  # mermaid 是示例默认值，允许改

    def test_nothing_proposed_when_all_disabled(self):
        probes = [
            _probe("codegraph", False, bin_ok=False, index_ok=False),
            _probe("archify", False),
            _probe("opencode", False),
        ]
        assert env_updates_from_probes(probes, {}) == []


class TestApplyEnvUpdates:
    def _updates(self, *pairs):
        return [
            {"key": k, "new": v, "cur": "(缺省)", "reason": "test"}
            for k, v in pairs
        ]

    def test_creates_env_from_example(self, tmp_path):
        example = tmp_path / ".env.example"
        example.write_text("CODE_EDIT_PROVIDER=mock\n", encoding="utf-8")
        env = tmp_path / ".env"
        applied, kept = apply_env_updates(
            self._updates(("CODE_EDIT_PROVIDER", "opencode")), env, example
        )
        assert any("已创建" in line for line in applied)
        assert kept == []
        assert "CODE_EDIT_PROVIDER=opencode" in env.read_text(encoding="utf-8")

    def test_replaces_example_default(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("CODE_EDIT_PROVIDER=mock\nTEST_GEN_PROVIDER=mock\n", encoding="utf-8")
        applied, _ = apply_env_updates(
            self._updates(("CODE_EDIT_PROVIDER", "opencode")), env, tmp_path / "none"
        )
        assert len(applied) == 1
        text = env.read_text(encoding="utf-8")
        assert "CODE_EDIT_PROVIDER=opencode" in text
        assert "TEST_GEN_PROVIDER=mock" in text  # 未提及的键不动

    def test_keeps_user_custom_value(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("CODE_EDIT_PROVIDER=my-own-backend\n", encoding="utf-8")
        applied, kept = apply_env_updates(
            self._updates(("CODE_EDIT_PROVIDER", "opencode")), env, tmp_path / "none"
        )
        assert applied == []
        assert len(kept) == 1
        assert "CODE_EDIT_PROVIDER=my-own-backend" in env.read_text(encoding="utf-8")

    def test_appends_missing_key(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("OTHER=1\n", encoding="utf-8")
        applied, _ = apply_env_updates(
            self._updates(("TEST_GEN_PROVIDER", "opencode")), env, tmp_path / "none"
        )
        assert len(applied) == 1
        assert "TEST_GEN_PROVIDER=opencode" in env.read_text(encoding="utf-8")

    def test_noop_when_already_target(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("CODE_EDIT_PROVIDER=opencode\n", encoding="utf-8")
        applied, kept = apply_env_updates(
            self._updates(("CODE_EDIT_PROVIDER", "opencode")), env, tmp_path / "none"
        )
        assert applied == [] and kept == []


# ═══════════════════════════════════════════════════════════════════
# 下载与自动安装（超时降级）
# ═══════════════════════════════════════════════════════════════════


class TestDownloadFile:
    def test_success(self, tmp_path):
        handler = lambda request: httpx.Response(200, content=b"hello-codegraph")  # noqa: E731
        dest = tmp_path / "f.bin"
        assert _download_file("https://x/f", dest, transport=httpx.MockTransport(handler))
        assert dest.read_bytes() == b"hello-codegraph"

    def test_deadline_cleans_partial(self, tmp_path):
        handler = lambda request: httpx.Response(200, content=b"x" * 1024)  # noqa: E731
        dest = tmp_path / "f.bin"
        assert not _download_file(
            "https://x/f", dest, transport=httpx.MockTransport(handler), deadline_sec=0
        )
        assert not dest.exists()  # 半成品已清理

    def test_404_returns_false(self, tmp_path):
        handler = lambda request: httpx.Response(404)  # noqa: E731
        assert not _download_file(
            "https://x/f", tmp_path / "f.bin", transport=httpx.MockTransport(handler)
        )


def _make_tar_gz(content: bytes, member: str = "codegraph") -> bytes:
    buf = BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo(member)
        info.size = len(content)
        info.mode = 0o755
        tf.addfile(info, BytesIO(content))
    return buf.getvalue()


class TestInstallCodegraph:
    def test_api_timeout_degrades_to_manual(self, monkeypatch):
        monkeypatch.setattr(doctor, "github_latest_release", lambda repo, **kw: None)
        assert install_codegraph(_silent_console()) == "manual"

    def test_unsupported_platform_degrades_to_manual(self, monkeypatch):
        monkeypatch.setattr(doctor, "codegraph_asset_name", lambda: None)
        assert install_codegraph(_silent_console()) == "manual"

    def test_full_success(self, tmp_path, monkeypatch):
        asset = doctor.codegraph_asset_name()
        if asset is None:
            pytest.skip("当前平台不支持自动安装资产映射")
        payload = _make_tar_gz(b"#!/bin/sh\necho codegraph-test\n")
        sha = hashlib.sha256(payload).hexdigest()

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url.endswith("/releases/latest"):
                return httpx.Response(200, json={"tag_name": "v9.9.9"})
            if url.endswith(asset):
                return httpx.Response(200, content=payload)
            if url.endswith("SHA256SUMS"):
                return httpx.Response(200, text=f"{sha}  {asset}\n")
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        install_dir = tmp_path / "bin"
        result = install_codegraph(
            _silent_console(), transport=transport, api_transport=transport,
            install_dirs=[install_dir],
        )
        assert result == "installed"
        bin_path = install_dir / "codegraph"
        assert bin_path.exists()
        assert os.access(bin_path, os.X_OK)

    def test_sha_mismatch_degrades_to_manual(self, tmp_path):
        asset = doctor.codegraph_asset_name()
        if asset is None:
            pytest.skip("当前平台不支持自动安装资产映射")
        payload = _make_tar_gz(b"#!/bin/sh\necho x\n")

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url.endswith("/releases/latest"):
                return httpx.Response(200, json={"tag_name": "v9.9.9"})
            if url.endswith(asset):
                return httpx.Response(200, content=payload)
            if url.endswith("SHA256SUMS"):
                return httpx.Response(200, text=f"{'0' * 64}  {asset}\n")
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        install_dir = tmp_path / "bin"
        result = install_codegraph(
            _silent_console(), transport=transport, api_transport=transport,
            install_dirs=[install_dir],
        )
        assert result == "manual"
        assert not (install_dir / "codegraph").exists()

    def test_download_timeout_degrades_to_manual(self, tmp_path):
        asset = doctor.codegraph_asset_name()
        if asset is None:
            pytest.skip("当前平台不支持自动安装资产映射")
        # 下载 deadline=0 → 必然超时；API 用独立 MockTransport 正常返回
        api = httpx.MockTransport(
            lambda request: httpx.Response(200, json={"tag_name": "v9.9.9"})
        )
        dl = httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"x" * 2048)
        )
        result = install_codegraph(
            _silent_console(), transport=dl, api_transport=api,
            deadline_sec=0, install_dirs=[tmp_path / "bin"],
        )
        assert result == "manual"


# ═══════════════════════════════════════════════════════════════════
# 报告收集 / 安装项 / 首启提示 / 向导冒烟
# ═══════════════════════════════════════════════════════════════════


class TestMissingInstallItems:
    def test_all_missing(self):
        items = missing_install_items([
            _probe("codegraph", False, bin_ok=False, index_ok=False),
            _probe("archify", False),
            _probe("opencode", False),
        ])
        assert [i.key for i in items] == ["codegraph", "opencode", "node"]
        assert [i.auto for i in items] == [True, False, False]
        # 发布页都指向 GitHub 官方
        assert all(i.releases_url.startswith("https://github.com/") for i in items)

    def test_bin_ok_but_index_missing_is_not_install_item(self):
        items = missing_install_items([
            _probe("codegraph", False, bin_ok=True, index_ok=False),
            _probe("archify", True),
            _probe("opencode", True),
        ])
        assert items == []  # 缺索引属于 codegraph init 提示，不是「安装二进制」


class TestFirstRunNotice:
    def test_missing_env_returns_guidance(self, tmp_path):
        notice = first_run_notice(env_path=tmp_path / ".env")
        assert notice is not None
        assert "devflow.cli setup" in notice
        assert "Mock" in notice

    def test_existing_env_returns_none(self, tmp_path):
        (tmp_path / ".env").write_text("A=1\n", encoding="utf-8")
        assert first_run_notice(env_path=tmp_path / ".env") is None


class TestCollectReport:
    def test_report_shape(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "devflow.doctor.check_providers_all",
            lambda root: [_probe("codegraph", False, bin_ok=False, index_ok=False),
                          _probe("archify", True), _probe("opencode", True)],
        )
        env = tmp_path / ".env"
        env.write_text("LLM_API_KEY=sk-your-deepseek-key\n", encoding="utf-8")
        r = collect_report(str(tmp_path), env_path=env)
        assert r["env_exists"] is True
        assert r["placeholder_providers"] == ["primary(LLM_API_KEY)"]
        assert [p["name"] for p in r["providers"]] == ["codegraph", "archify", "opencode"]

    def test_missing_env_reported(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "devflow.doctor.check_providers_all", lambda root: []
        )
        r = collect_report(str(tmp_path), env_path=tmp_path / ".env")
        assert r["env_exists"] is False
        assert r["env_vars"] == {}


class TestRunSetupSmoke:
    def test_check_only_prints_report_and_guidance(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "devflow.doctor.check_providers_all",
            lambda root: [_probe("codegraph", False, bin_ok=False, index_ok=False),
                          _probe("archify", True), _probe("opencode", True)],
        )
        out = io.StringIO()
        console = Console(file=out, width=200)
        # run_setup 走 collect_report 默认 .env（相对 CWD），chdir 到 tmp 隔离
        monkeypatch.chdir(tmp_path)
        run_setup(console, project_root=str(tmp_path), check_only=True)
        text = out.getvalue()
        assert "最后一步" in text
        assert "check-providers" in text
        assert "check-llm" in text
        assert "uvicorn web.server:app" in text

    def test_interactive_skip_everything(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "devflow.doctor.check_providers_all",
            lambda root: [_probe("codegraph", False, bin_ok=False, index_ok=False),
                          _probe("archify", True), _probe("opencode", True)],
        )
        monkeypatch.chdir(tmp_path)
        answers = iter(["", ""])  # 多选回车跳过安装；env 配置回车应用（空 updates 时仍创建 .env）
        out = io.StringIO()
        run_setup(Console(file=out, width=200), project_root=str(tmp_path),
                  input_fn=lambda *a, **kw: next(answers))
        text = out.getvalue()
        assert "最后一步" in text
