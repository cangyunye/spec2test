"""Pi Coding Agent Provider 骨架测试。

覆盖：工厂装配 / CLI 参数拼装 / JSON 抠取 / 检索解析 / 仅需求模式委托 /
probe_pi 探测 / setup 向导的安装项与 .env 建议。
"""
import asyncio
import json
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

from devflow.doctor import env_updates_from_probes, install_pi, missing_install_items
from devflow.providers import build_code_edit, build_code_search, build_test_gen
from devflow.providers.check import probe_pi
from devflow.providers.pi import (
    PiEditProvider,
    PiNotInstalledError,
    PiSearchProvider,
    PiTestProvider,
    _extract_json_payload,
)


# ═══════════════════════════════════════════════════════════════════
# 工厂装配
# ═══════════════════════════════════════════════════════════════════
def test_factory_pi_branches():
    assert isinstance(build_code_search("pi"), PiSearchProvider)
    assert isinstance(build_code_edit("pi"), PiEditProvider)
    assert isinstance(build_test_gen("pi"), PiTestProvider)


def test_factory_rejects_pi_typo():
    with pytest.raises(ValueError, match="CODE_EDIT_PROVIDER"):
        build_code_edit("pi-agent")


# ═══════════════════════════════════════════════════════════════════
# CLI 参数拼装与子进程
# ═══════════════════════════════════════════════════════════════════
def test_build_args_flags_order():
    p = PiSearchProvider(
        bin_path="pi", provider="openai", model="openai/gpt-4o", extra_args=["--no-approve"]
    )
    args = p._build_args("帮我找登录逻辑")
    # provider/model 前置，--no-session 固定，prompt 永远是最后一个位置参数
    assert args == [
        "pi",
        "--provider", "openai",
        "--model", "openai/gpt-4o",
        "--no-session",
        "--no-approve",
        "--print", "帮我找登录逻辑",
    ]


def test_build_args_defaults_no_model():
    p = PiSearchProvider(bin_path="pi", provider="", model="", extra_args=[])
    args = p._build_args("q")
    assert args == ["pi", "--no-session", "--print", "q"]


@pytest.mark.asyncio
async def test_run_pi_not_installed_fails_fast(monkeypatch, tmp_path: Path):
    """create_subprocess_exec 抛 FileNotFoundError → PiNotInstalledError（不可重试）。"""

    async def _raise(*args, **kwargs):
        raise FileNotFoundError("pi")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _raise)
    p = PiEditProvider(bin_path="pi-does-not-exist")
    with pytest.raises(PiNotInstalledError) as ei:
        await p._run_pi(str(tmp_path), "x")
    assert ei.value.retryable is False


@pytest.mark.asyncio
async def test_run_pi_returns_stdout(monkeypatch, tmp_path: Path):
    captured: dict = {}

    class FakeProc:
        returncode = 0

        def __init__(self) -> None:
            self._so = b'{"summary": "ok"}'

        async def communicate(self):
            return self._so, b""

        async def kill(self):  # pragma: no cover
            pass

    async def fake_exec(*args, **kwargs):
        captured["argv"] = list(args)
        captured["cwd"] = kwargs.get("cwd")
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    p = PiEditProvider(bin_path="pi")
    raw = await p._run_pi(str(tmp_path), "do it")
    assert raw == '{"summary": "ok"}'
    assert captured["cwd"] == str(tmp_path)  # pi 无 cwd 参数，靠子进程 cwd


# ═══════════════════════════════════════════════════════════════════
# JSON 抠取（pi 是自然语言输出，必须容错）
# ═══════════════════════════════════════════════════════════════════
def test_extract_json_payload_variants():
    assert _extract_json_payload('[{"a": 1}]') == [{"a": 1}]
    assert _extract_json_payload('前言 ```json\n{"b": 2}\n``` 后记') == {"b": 2}
    assert _extract_json_payload('结果如下：{"c": 3} 完毕') == {"c": 3}
    assert _extract_json_payload("完全不是 JSON") is None
    assert _extract_json_payload("") is None


# ═══════════════════════════════════════════════════════════════════
# 检索：围栏 JSON 解析 + 字段归一
# ═══════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_search_parses_fenced_json(monkeypatch, tmp_path: Path):
    fenced = (
        "定位到登录逻辑：\n```json\n"
        "[{\"file_path\": \"src/auth.py\", \"symbol_name\": \"login\", "
        "\"line_start\": 10, \"line_end\": 20, \"code_snippet\": \"def login(): ...\", "
        "\"relevance_score\": 0.9, \"callers\": [\"api.py:handler\"], \"callees\": []}]"
        "\n```\n以上。"
    )

    async def fake_run(self, project_root, prompt, **kw):
        return fenced

    monkeypatch.setattr(PiSearchProvider, "_run_pi", fake_run)
    out = await PiSearchProvider(bin_path="pi").search(str(tmp_path), "登录逻辑", max_results=5)
    assert out["total"] == 1
    hit = out["results"][0]
    assert hit["file_path"] == "src/auth.py"
    assert hit["symbol_name"] == "login"
    assert hit["line_start"] == 10 and hit["line_end"] == 20
    assert hit["relevance_score"] == pytest.approx(0.9)
    assert hit["callers"] == ["api.py:handler"]


@pytest.mark.asyncio
async def test_search_bad_output_returns_empty(monkeypatch, tmp_path: Path):
    async def fake_run(self, project_root, prompt, **kw):
        return "模型跑题了，没有任何 JSON"

    monkeypatch.setattr(PiSearchProvider, "_run_pi", fake_run)
    out = await PiSearchProvider(bin_path="pi").search(str(tmp_path), "q")
    assert out["total"] == 0 and out["results"] == []


# ═══════════════════════════════════════════════════════════════════
# 测试生成：仅需求模式委托 LlmTestGenProvider
# ═══════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_testgen_empty_project_delegates_to_llm(monkeypatch):
    sentinel = {"session_id": "llm-1", "test_cases": [], "run": {"passed": 0, "failed": 0, "skipped": 0, "coverage_pct": None, "logs": ""}}
    got: dict = {}

    async def fake_generate(self, project_root, target_symbols, **kwargs):
        got["project_root"] = project_root
        got["target_symbols"] = target_symbols
        got.update(kwargs)
        return sentinel

    monkeypatch.setattr("devflow.providers.llm_testgen.LlmTestGenProvider.generate", fake_generate)
    out = await PiTestProvider(bin_path="pi").generate(
        "",
        ["AuthService.login"],
        requirement={"name": "登录"},
        feedback="上轮漏了 2FA",
        checklists=[{"rel_dir": "cl", "name": "支付", "content": "必须覆盖退款"}],
    )
    assert out is sentinel
    assert got["project_root"] == ""
    assert got["feedback"] == "上轮漏了 2FA"
    assert got["checklists"] == [{"rel_dir": "cl", "name": "支付", "content": "必须覆盖退款"}]


@pytest.mark.asyncio
async def test_testgen_parses_report(monkeypatch, tmp_path: Path):
    report = json.dumps({
        "test_cases": [{"test_file": "tests/test_auth.py", "test_symbol": "test_login_ok", "code_snippet": "def test_login_ok(): ...", "covered_edges": ["e1"]}],
        "run": {"passed": 3, "failed": 1, "skipped": 0, "coverage_pct": 82.5, "logs": "pytest output"},
    })

    async def fake_run(self, project_root, prompt, **kw):
        return report

    monkeypatch.setattr(PiTestProvider, "_run_pi", fake_run)
    out = await PiTestProvider(bin_path="pi").generate(str(tmp_path), ["AuthService.login"])
    assert out["run"]["passed"] == 3 and out["run"]["failed"] == 1
    assert out["run"]["coverage_pct"] == pytest.approx(82.5)
    assert out["test_cases"][0]["test_file"] == "tests/test_auth.py"


@pytest.mark.asyncio
async def test_testgen_unparseable_report_raises(monkeypatch, tmp_path: Path):
    async def fake_run(self, project_root, prompt, **kw):
        return "我改了文件但忘了输出 JSON"

    monkeypatch.setattr(PiTestProvider, "_run_pi", fake_run)
    from devflow.errors import CliExitError

    with pytest.raises(CliExitError):
        await PiTestProvider(bin_path="pi").generate(str(tmp_path), ["X.y"])


# ═══════════════════════════════════════════════════════════════════
# 探测 + setup 向导
# ═══════════════════════════════════════════════════════════════════
def test_probe_pi_missing(monkeypatch):
    monkeypatch.setattr("devflow.providers.check.shutil.which", lambda x: None)
    r = probe_pi()
    assert r["name"] == "pi" and r["ok"] is False and r["bin_ok"] is False


def test_probe_pi_found(monkeypatch):
    monkeypatch.setattr("devflow.providers.check.shutil.which", lambda x: "/usr/local/bin/pi")
    r = probe_pi()
    assert r["ok"] is True and r["bin_ok"] is True and "pi" in r["detail"]


def _fake_reports(*, pi_installed: bool, opencode_ok: bool = False) -> list[dict]:
    return [
        {"name": "codegraph", "ok": False, "bin_ok": False, "index_ok": False},
        {"name": "archify", "ok": False},
        {"name": "opencode", "ok": opencode_ok},
        {"name": "pi", "ok": pi_installed, "bin_ok": pi_installed},
    ]


def test_missing_install_items_includes_pi():
    items = missing_install_items(_fake_reports(pi_installed=False))
    keys = [it.key for it in items]
    assert "pi" in keys
    assert any("npm install -g" in it.install_hint for it in items if it.key == "pi")
    # 已安装则不出现
    assert "pi" not in [it.key for it in missing_install_items(_fake_reports(pi_installed=True))]


def test_env_updates_pi_only_when_opencode_absent():
    # OpenCode 未配置 + pi 就绪 → 建议 edit/test 走 pi（检索不给 pi）
    updates = env_updates_from_probes(_fake_reports(pi_installed=True), {})
    by_key = {u["key"]: u["new"] for u in updates}
    assert by_key.get("CODE_EDIT_PROVIDER") == "pi"
    assert by_key.get("TEST_GEN_PROVIDER") == "pi"
    assert "CODE_SEARCH_PROVIDER" not in by_key
    # OpenCode 已可用时不建议 pi，避免两套建议打架
    updates2 = env_updates_from_probes(_fake_reports(pi_installed=True, opencode_ok=True), {})
    by_key2 = {u["key"]: u["new"] for u in updates2}
    assert by_key2.get("CODE_EDIT_PROVIDER") == "opencode"


def test_install_pi_manual_when_npm_missing(monkeypatch):
    monkeypatch.setattr("devflow.doctor.shutil.which", lambda x: None)
    console = Console(file=StringIO(), force_terminal=False)
    assert install_pi(console) == "manual"
