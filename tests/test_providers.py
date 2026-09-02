"""devflow.providers 各实现的单元测试。

策略：
  - mock provider：端到端跑一遍，断言 TypedDict 返回值字段齐全
  - codegraph provider：通过 monkeypatch 替换 asyncio.create_subprocess_exec，
    断言实际调用命令行参数；也断言字段别名的归一化、scope_files 过滤、fallback 触发
  - archify provider：不跑真实 Node CLI（CI 里没装），只断言字段映射 IR 结构、
    以及 force_mermaid_fallback 路径
  - opencode provider：monkeypatch 掉 devflow.providers.opencode._post，
    断言 last_request body 符合 SPEC 2.4 契约
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from devflow.providers import (
    ArchifyProvider,
    CodeGraphProvider,
    MockCodeEdit,
    MockCodeGraphRender,
    MockCodeSearch,
    MockTestGen,
    OpenCodeEditProvider,
    OpenCodeSearchProvider,
    OpenCodeTestProvider,
    build_code_search,
    build_graph_render,
    build_code_edit,
    build_test_gen,
    get_providers,
)
from devflow.providers.archify import logic_graph_to_archify_ir
from devflow.providers.codegraph import CodeGraphNotInstalledError, _normalize_hit


# ═══════════════════════════════════════════════════════════════════
# 工厂：坏名字 → ValueError
# ═══════════════════════════════════════════════════════════════════
def test_factory_rejects_bad_names():
    with pytest.raises(ValueError, match="CODE_SEARCH_PROVIDER"):
        build_code_search("bogus")
    with pytest.raises(ValueError, match="CODE_GRAPH_RENDER_PROVIDER"):
        build_graph_render("bogus")
    with pytest.raises(ValueError, match="CODE_EDIT_PROVIDER"):
        build_code_edit("bogus")
    with pytest.raises(ValueError, match="TEST_GEN_PROVIDER"):
        build_test_gen("bogus")


def test_get_providers_defaults_are_mock_based(monkeypatch):
    # .env 里没设时走默认值：mock/mermaid/mock/mock
    for key in [
        "CODE_SEARCH_PROVIDER",
        "CODE_GRAPH_RENDER_PROVIDER",
        "CODE_EDIT_PROVIDER",
        "TEST_GEN_PROVIDER",
    ]:
        monkeypatch.delenv(key, raising=False)
    p = get_providers()
    assert isinstance(p.code_search, MockCodeSearch)
    assert isinstance(p.graph_render, MockCodeGraphRender)
    assert isinstance(p.code_edit, MockCodeEdit)
    assert isinstance(p.test_gen, MockTestGen)


# ═══════════════════════════════════════════════════════════════════
# Mock Providers
# ═══════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_mock_search_full_shape():
    p = MockCodeSearch()
    out = await p.search(
        "/tmp/prj",
        "jwt auth",
        query_type="semantic",
        target_symbols=["Auth.login"],
        max_results=5,
    )
    assert out["total"] >= 1
    first = out["results"][0]
    assert first["file_path"].endswith(".py")
    assert first["symbol_name"] is not None
    assert 1 <= first["line_start"] <= first["line_end"]
    assert "def " in first["code_snippet"]
    assert 0.0 <= first["relevance_score"] <= 1.0
    assert isinstance(first["callers"], list) and isinstance(first["callees"], list)
    # session_id 复用
    out2 = await p.search("/tmp/prj", "jwt", session_id=out["session_id"])
    assert out2["session_id"] == out["session_id"]


@pytest.mark.asyncio
async def test_mock_render_returns_mermaid_and_html():
    p = MockCodeGraphRender()
    logic_graph = {"graph_id": "g-1", "mermaid_source": "graph TD\\n  A-->B"}
    m = await p.render(logic_graph, preferred_format="mermaid")
    assert m["format"] == "mermaid" and m["mermaid_text"] == "graph TD\\n  A-->B"
    h = await p.render(logic_graph, preferred_format="html")
    assert h["format"] == "html"
    assert isinstance(h["html_bytes"], bytes) and b"<html" in h["html_bytes"].lower()


@pytest.mark.asyncio
async def test_mock_edit_with_lint_trigger():
    p = MockCodeEdit()
    out = await p.generate(
        "/tmp/prj",
        "add 2fa to login",
        related_files=[{"file_path": "src/auth.py"}],
        acceptance=["pass lint check"],  # 触发 mock 的一个 lint warning
    )
    assert len(out["changes"]) == 1 and out["changes"][0]["file_path"] == "src/auth.py"
    assert out["lint_passed"] is False
    assert out["lint"][0]["linter"] == "mock"


@pytest.mark.asyncio
async def test_mock_testgen_uses_logic_graph_edges():
    p = MockTestGen()
    graph = {
        "graph_id": "g-1",
        "nodes": [],
        "edges": [
            {"edge_id": "e-1", "from_node": "n-1", "to_node": "n-2", "edge_type": "call", "is_modified": True},
            {"edge_id": "e-2", "from_node": "n-2", "to_node": "n-3", "edge_type": "call", "is_modified": True},
        ],
        "mermaid_source": "",
    }
    rep = await p.generate(
        "/tmp/prj",
        ["Auth.login", "Auth.logout"],
        logic_graph=graph,
    )
    assert rep["target_symbols"] == ["Auth.login", "Auth.logout"]
    assert rep["run"]["coverage_pct"] == 80
    # 第一个 case 覆盖第一条边
    assert rep["test_cases"][0]["covered_edges"] == ["e-1"]


# ═══════════════════════════════════════════════════════════════════
# CodeGraph Provider
# ═══════════════════════════════════════════════════════════════════
def test_codegraph_field_aliases_normalize():
    """字段别名处理：CodeGraph 不同版本字段名漂移，_normalize_hit 应该兜住。"""
    a = _normalize_hit(
        {
            "source": "x/y.py",
            "identifier": "Foo.bar",
            "start": 7,
            "end": 12,
            "snippet": "def bar(self): pass",
            "score": 0.6,
            "called_by": ["a.py:caller"],
            "calls": ["b.py:callee"],
        }
    )
    assert a["file_path"] == "x/y.py"
    assert a["symbol_name"] == "Foo.bar"
    assert a["line_start"] == 7 and a["line_end"] == 12
    assert "def bar" in a["code_snippet"]
    assert a["relevance_score"] == pytest.approx(0.6)
    assert a["callers"] == ["a.py:caller"]
    assert a["callees"] == ["b.py:callee"]


@pytest.mark.asyncio
async def test_codegraph_cli_args_for_symbol_query(monkeypatch, tmp_path: Path):
    """symbol 查询应该走 node/callers/callees 三条命令，带 --json。"""
    calls: list[tuple[str, ...]] = []

    class FakeProc:
        def __init__(self, rc=0, stdout=b"[]", stderr=b"") -> None:
            self.returncode = rc
            self._so = stdout
            self._se = stderr

        async def communicate(self):
            return self._so, self._se

        async def kill(self):  # pragma: no cover
            pass

    async def fake_subproc_exec(*args, **kwargs):
        calls.append(tuple(args[1:]))  # 跳过第一个参数（self.bin_path）
        # search/node/callers/callees 各自返回空数组，不影响断言 args
        return FakeProc(0, b"[]", b"")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subproc_exec)
    # bin_path 指向 /nonexistent 但因为我们 monkey patch 掉 create_subprocess_exec，
    # 实际不会执行；但 shutil.which 在 _ensure_env 会看 bin_path 是否存在，
    # 所以给一个肯定存在的路径
    bin_real = Path("/bin/sh")
    p = CodeGraphProvider(bin_path=str(bin_real))
    await p.search(
        str(tmp_path),
        "unused",
        query_type="symbol",
        target_symbols=["Auth.login"],
    )
    # 调用顺序：node --json Auth.login, callers --json Auth.login, callees --json Auth.login
    assert len(calls) == 3
    assert calls[0] == ("node", "--json", "Auth.login")
    assert calls[1] == ("callers", "--json", "Auth.login")
    assert calls[2] == ("callees", "--json", "Auth.login")


@pytest.mark.asyncio
async def test_codegraph_cli_args_for_search_query(monkeypatch, tmp_path: Path):
    """语义查询先走 search，explore 仅在 search 返回空时才触发。"""
    calls: list[tuple[str, ...]] = []

    class FakeProc:
        def __init__(self, rc, stdout, stderr=b"") -> None:
            self.returncode = rc
            self._so = stdout
            self._se = stderr

        async def communicate(self):
            return self._so, self._se

        async def kill(self):  # pragma: no cover
            pass

    async def fake_subproc_exec(*args, **kwargs):
        subcmd, flag, query = args[1], args[2], args[3]
        calls.append((subcmd, flag, query))
        if subcmd == "search":
            # 不空 → explore 不触发
            body = [
                {
                    "file": "src/auth/routes.py",
                    "symbol": "login_handler",
                    "start_line": 10,
                    "end_line": 30,
                    "snippet": "def login_handler(): ...",
                    "score": 0.9,
                    "callers": [],
                    "callees": [],
                }
            ]
            import json as _json

            return FakeProc(0, _json.dumps(body).encode("utf-8"), b"")
        if subcmd == "explore":  # pragma: no cover
            return FakeProc(0, b"[]", b"")
        return FakeProc(0, b"[]", b"")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subproc_exec)
    p = CodeGraphProvider(bin_path="/bin/sh")
    out = await p.search(
        str(tmp_path),
        "jwt 登录入口",
        query_type="semantic",
        scope_files=["src/auth/*"],
        max_results=5,
    )
    assert [c[0] for c in calls] == ["search"]  # explore 没调
    assert out["total"] == 1
    assert out["results"][0]["file_path"] == "src/auth/routes.py"
    assert out["results"][0]["symbol_name"] == "login_handler"


@pytest.mark.asyncio
async def test_codegraph_uninstalled_triggers_fallback(monkeypatch, tmp_path: Path):
    """bin_path 不存在 → 抛 CodeGraphNotInstalledError；有 fallback 就走 fallback。"""
    monkeypatch.setattr("devflow.providers.codegraph.shutil.which", lambda x: None)
    # fallback 是 Mock，应该能正常返回
    fallback = MockCodeSearch()
    p = CodeGraphProvider(bin_path="/does/not/exist/codegraph", fallback=fallback)
    out = await p.search(str(tmp_path), "anything")
    assert out["total"] >= 1
    # 没 fallback → 抛错
    p2 = CodeGraphProvider(bin_path="/does/not/exist/codegraph")
    with pytest.raises(CodeGraphNotInstalledError):
        await p2.search(str(tmp_path), "anything")


# ═══════════════════════════════════════════════════════════════════
# Archify Provider：只测字段映射和 fallback 路径
# ═══════════════════════════════════════════════════════════════════
def test_logic_graph_to_archify_ir_mapping():
    graph = {
        "graph_id": "graph-tiny",
        "nodes": [
            {
                "node_id": "n-in",
                "label": "HTTP /login",
                "node_type": "io",
                "code_ref": None,
                "is_modified": False,
            },
            {
                "node_id": "n-login-new",
                "label": "AuthService.login",
                "node_type": "function",
                "code_ref": {"file_path": "src/auth/svc.py", "symbol": "AuthService.login"},
                "is_modified": True,
            },
            {
                "node_id": "n-ext",
                "label": "SMS Gateway",
                "node_type": "external",
                "code_ref": None,
                "is_modified": False,
            },
        ],
        "edges": [
            {
                "edge_id": "e-1",
                "from_node": "n-in",
                "to_node": "n-login-new",
                "edge_type": "call",
                "condition": None,
                "is_modified": True,
            },
            {
                "edge_id": "e-2",
                "from_node": "n-login-new",
                "to_node": "n-ext",
                "edge_type": "condition",
                "condition": "2FA enabled",
                "is_modified": False,
            },
        ],
        "mermaid_source": "graph TD\\n  n-in-->n-login-new",
    }
    ir = logic_graph_to_archify_ir(graph)
    # 标题与结构
    assert ir["title"] == "graph-tiny" and ir["version"] == "archify-1.0"
    roles = {n["id"]: n["role"] for n in ir["diagram"]["nodes"]}
    assert roles == {"n-in": "frontend", "n-login-new": "backend", "n-ext": "external"}
    # 新节点 diff=added，且带背景色
    new_n = next(n for n in ir["diagram"]["nodes"] if n["id"] == "n-login-new")
    assert new_n["diff"] == "added" and new_n.get("background")
    assert new_n["source"]["ref"] == "src/auth/svc.py:AuthService.login"
    # 边：condition 类型 → label 写条件；e-1 是 modified
    e1 = next(e for e in ir["diagram"]["edges"] if e["id"] == "e-1")
    assert e1["diff"] == "modified"  # id 里不含 "new"/"add" → modified
    e2 = next(e for e in ir["diagram"]["edges"] if e["id"] == "e-2")
    assert e2["label"] == "2FA enabled" and e2["kind"] == "condition"


@pytest.mark.asyncio
async def test_archify_force_mermaid_fallback():
    p = ArchifyProvider(force_mermaid_fallback=True)
    out = await p.render({"mermaid_source": "graph TD\\n  A-->B"}, preferred_format="html")
    assert out["format"] == "mermaid"
    assert "mermaid-fallback" not in out["render_backend"]  # force 路径直接 +mermaid
    assert out["mermaid_text"] == "graph TD\\n  A-->B"


# ═══════════════════════════════════════════════════════════════════
# OpenCode Provider：断言 HTTP body 严格符合 SPEC 2.4 契约
# ═══════════════════════════════════════════════════════════════════
@pytest.fixture
def stub_post(monkeypatch):
    """替换 _post，记录 last 调用并返回 canned response。"""

    state = {"last": None, "response": {}}

    async def fake(base_url, token, path, payload, timeout_sec=120):
        state["last"] = {"base_url": base_url, "token": token, "path": path, "payload": payload}
        return state["response"]

    monkeypatch.setattr("devflow.providers.opencode._post", fake)
    return state


@pytest.mark.asyncio
async def test_opencode_search_body_spec241(stub_post):
    stub_post["response"] = {
        "session_id": "sess-1",
        "total": 1,
        "results": [
            {
                "file_path": "a.py",
                "symbol_name": "A",
                "line_start": 1,
                "line_end": 2,
                "code_snippet": "x=1",
                "relevance_score": 0.9,
                "callers": ["b.py:caller"],
                "callees": ["c.py:callee"],
            }
        ],
    }
    p = OpenCodeSearchProvider(base_url="http://oc", api_token="tk")
    out = await p.search(
        "/tmp/prj",
        "jwt login",
        query_type="call_chain",
        target_symbols=["Auth.login"],
        scope_files=["src/auth/*"],
        max_results=7,
        session_id="old-sess",
    )
    last = stub_post["last"]
    assert last["path"] == "/api/v1/code/search"
    assert last["token"] == "tk"
    body = last["payload"]
    assert body["project_root"] == "/tmp/prj"
    assert body["session_id"] == "old-sess"
    assert body["query"]["type"] == "call_chain"
    assert body["query"]["text"] == "jwt login"
    assert body["query"]["target_symbols"] == ["Auth.login"]
    assert body["query"]["scope_files"] == ["src/auth/*"]
    assert body["max_results"] == 7
    assert body["include_context"] is True
    assert body["request_id"].startswith("devflow-")
    # 返回值能正确解码
    assert out["session_id"] == "sess-1" and out["results"][0]["symbol_name"] == "A"


@pytest.mark.asyncio
async def test_opencode_edit_body_spec242(stub_post):
    stub_post["response"] = {
        "session_id": "sess-code-1",
        "changes": [
            {
                "file_path": "src/a.py",
                "action": "update",
                "diff_unified": "--- a/src/a.py",
                "content_after": "# done",
            }
        ],
        "lint": {"passed": False, "issues": [{"file_path": "src/a.py", "line": 3, "level": "error", "message": "bad", "linter": "flake8"}]},
    }
    p = OpenCodeEditProvider(base_url="http://oc", api_token="")
    out = await p.generate(
        "/tmp/prj",
        "add 2fa",
        logic_graph_node_id="n-login-new",
        related_files=[{"file_path": "src/auth.py"}],
        acceptance=["AC1"],
        run_lint=True,
    )
    last = stub_post["last"]
    assert last["path"] == "/api/v1/code/generate"
    body = last["payload"]
    assert body["instruction"] == "add 2fa"
    assert body["context"]["logic_graph_node_id"] == "n-login-new"
    assert body["context"]["acceptance_criteria"] == ["AC1"]
    assert body["run_lint"] is True and body["max_retry_fix"] == 1
    # 返回值解码
    assert out["lint_passed"] is False
    assert out["lint"][0]["level"] == "error"


@pytest.mark.asyncio
async def test_opencode_test_body_spec243(stub_post):
    stub_post["response"] = {
        "session_id": "sess-test-1",
        "test_cases": [],
        "run": {"passed": 0, "failed": 1, "skipped": 0, "coverage_pct": None, "logs": "boom"},
    }
    p = OpenCodeTestProvider(base_url="http://oc", api_token="")
    rep = await p.generate(
        "/tmp/prj",
        ["Auth.login"],
        coverage_target=90,
        modified_branches_only=False,
        logic_graph={"graph_id": "gg-1"},
    )
    last = stub_post["last"]
    assert last["path"] == "/api/v1/tests/generate"
    body = last["payload"]
    assert body["target"]["files_or_symbols"] == ["Auth.login"]
    assert body["target"]["modified_branches_only"] is False
    assert body["target"]["logic_graph_ref"] == "gg-1"
    assert body["framework"] == "pytest" and body["coverage_target"] == 90
    assert rep["run"]["failed"] == 1 and rep["run"]["coverage_pct"] is None
