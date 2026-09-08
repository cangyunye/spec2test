"""Provider 节点单元测试：验证 make_*_node 工厂产出的 async 节点能正确读写 GlobalState。

不跑真实 LangGraph graph 编译（那需要 LLM），只断言单节点 input/output 契约。
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from devflow.nodes.provider_nodes import (
    make_code_gen_node,
    make_code_search_node,
    make_graph_render_node,
    make_test_gen_node,
    route_after_code_gen,
    route_after_code_search,
    route_after_test_gen,
)
from devflow.providers import (
    MockCodeEdit,
    MockCodeGraphRender,
    MockCodeSearch,
    MockTestGen,
    Providers,
)


# ═══════════════════════════════════════════════════════════════════
# 辅助
# ═══════════════════════════════════════════════════════════════════
def _mock_providers() -> Providers:
    return Providers(
        code_search=MockCodeSearch(),
        graph_render=MockCodeGraphRender(),
        code_edit=MockCodeEdit(),
        test_gen=MockTestGen(),
    )


def _sample_requirement() -> dict[str, Any]:
    return {
        "req_type": "component_iteration",
        "project_root": "/tmp/demo-project",
        "project_context": "FastAPI 后端，JWT 鉴权",
        "target_modules": ["src/auth/*"],
        "existing_code_accessible": True,
        "io_constraints": {"input": "POST /login", "output": "JWT token"},
        "edge_cases": ["2FA 用户首次登录"],
        "acceptance_criteria": ["登录成功返回 token", "2FA 用户返回 SMS_CODE_MISSING"],
    }


def _sample_logic_graph() -> dict[str, Any]:
    return {
        "graph_id": "graph-test",
        "nodes": [
            {"node_id": "n-in", "label": "HTTP /login", "node_type": "io",
             "code_ref": None, "is_modified": False},
            {"node_id": "n-login-new", "label": "AuthService.login",
             "node_type": "function",
             "code_ref": {"file_path": "src/auth/svc.py", "symbol": "AuthService.login"},
             "is_modified": True},
        ],
        "edges": [
            {"edge_id": "e-1", "from_node": "n-in", "to_node": "n-login-new",
             "edge_type": "call", "condition": None, "is_modified": True},
        ],
        "mermaid_source": "graph TD\n  n-in-->n-login-new",
    }


# ═══════════════════════════════════════════════════════════════════
# 1. code_search_node
# ═══════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_code_search_node_writes_code_context():
    node = make_code_search_node(_mock_providers())
    state = {
        "requirement": _sample_requirement(),
        "opencode_sessions": {"search": None, "code_gen": None, "test_gen": None},
    }
    out = await node.async_version(state)  # type: ignore[arg-type]
    assert "code_context" in out
    assert len(out["code_context"]) >= 1
    first = out["code_context"][0]
    assert "file_path" in first and "code_snippet" in first
    # session_id 被写回
    assert out["opencode_sessions"]["search"] is not None
    assert out["current_stage"] == "graph"
    assert out["last_error"] is None


@pytest.mark.asyncio
async def test_code_search_node_empty_project_root():
    node = make_code_search_node(_mock_providers())
    state = {"requirement": {}, "opencode_sessions": {}}
    out = await node.async_version(state)  # type: ignore[arg-type]
    assert "project_root" in (out["last_error"] or "")
    assert out["current_stage"] == "search"


# ═══════════════════════════════════════════════════════════════════
# 2. graph_render_node
# ═══════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_graph_render_node_updates_mermaid():
    node = make_graph_render_node(_mock_providers())
    state = {"logic_graph": _sample_logic_graph()}
    out = await node.async_version(state)  # type: ignore[arg-type]
    updated = out["logic_graph"]
    assert updated["mermaid_source"]  # 非空
    assert updated["_render_backend"]  # 记录了实际后端名
    assert out["current_stage"] == "code"


@pytest.mark.asyncio
async def test_graph_render_node_no_logic_graph():
    node = make_graph_render_node(_mock_providers())
    out = await node.async_version({"logic_graph": None})  # type: ignore[arg-type]
    assert "logic_graph" in (out["last_error"] or "")


# ═══════════════════════════════════════════════════════════════════
# 3. code_gen_node
# ═══════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_code_gen_node_produces_changes():
    node = make_code_gen_node(_mock_providers())
    state = {
        "requirement": _sample_requirement(),
        "logic_graph": _sample_logic_graph(),
        "code_context": [
            {"file_path": "src/auth/svc.py", "symbol_name": "AuthService.login"},
        ],
        "opencode_sessions": {"search": None, "code_gen": None, "test_gen": None},
    }
    out = await node.async_version(state)  # type: ignore[arg-type]
    assert len(out["code_changes"]) >= 1
    ch = out["code_changes"][0]
    assert ch["file_path"] and ch["diff"]
    assert "lint_passed" in ch
    assert out["opencode_sessions"]["code_gen"] is not None
    # mock 默认不触发 lint warning → lint_passed=True → 进入 test
    assert out["current_stage"] == "test"


@pytest.mark.asyncio
async def test_code_gen_node_no_modified_nodes():
    node = make_code_gen_node(_mock_providers())
    lg = _sample_logic_graph()
    for n in lg["nodes"]:
        n["is_modified"] = False
    state = {"requirement": _sample_requirement(), "logic_graph": lg}
    out = await node.async_version(state)  # type: ignore[arg-type]
    assert "is_modified" in (out["last_error"] or "")


# ═══════════════════════════════════════════════════════════════════
# 4. test_gen_node
# ═══════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_test_gen_node_writes_report():
    node = make_test_gen_node(_mock_providers())
    state = {
        "requirement": _sample_requirement(),
        "logic_graph": _sample_logic_graph(),
        "code_changes": [{"file_path": "src/auth/svc.py", "action": "update", "diff": "..."}],
        "opencode_sessions": {"search": None, "code_gen": None, "test_gen": None},
    }
    out = await node.async_version(state)  # type: ignore[arg-type]
    assert out["test_report"] is not None
    assert "test_cases" in out["test_report"]
    assert "run" in out["test_report"]
    # test_gen 只产出设计报告；真实 test_passed 由下游 test_run 节点回填
    assert "code_changes" not in out
    assert out["current_stage"] == "test"


@pytest.mark.asyncio
async def test_test_gen_node_empty_changes():
    """代码模式（提供了项目代码）下 code_changes 为空 → 报错回炉。"""
    node = make_test_gen_node(_mock_providers())
    state = {"requirement": {"project_root": "/workspace"}, "code_changes": []}
    out = await node.async_version(state)  # type: ignore[arg-type]
    assert "code_changes" in (out["last_error"] or "")


@pytest.mark.asyncio
async def test_test_gen_node_requirement_only_needs_no_changes():
    """仅需求模式（未提供代码）：code_changes 为空是常态，直接用需求+逻辑图设计用例。"""
    node = make_test_gen_node(_mock_providers())
    state = {
        "requirement": {
            "project_context": "计算器",
            "target_modules": ["calc.py"],
            "io_constraints": {"input": "x", "output": "y"},
            "edge_cases": ["除零"],
            "acceptance_criteria": ["结果正确"],
        },
        "logic_graph": {},
        "code_changes": [],
        "opencode_sessions": {},
    }
    out = await node.async_version(state)  # type: ignore[arg-type]
    assert out.get("last_error_code") is None
    assert out["test_report"]["test_cases"]


# ═══════════════════════════════════════════════════════════════════
# 5. 路由函数
# ═══════════════════════════════════════════════════════════════════
def test_route_after_code_search():
    assert route_after_code_search({"code_context": [{"file_path": "a.py"}]}) == "has_results"
    assert route_after_code_search({"code_context": []}) == "no_results"
    assert route_after_code_search({}) == "no_results"


def test_route_after_code_gen():
    # lint 不通过 + 未超重试次数
    s1 = {"last_error": "lint 不通过", "retry_count": {"code_gen": 0}}
    assert route_after_code_gen(s1) == "retry"
    # lint 不通过 + 超重试
    s2 = {"last_error": "lint 不通过", "retry_count": {"code_gen": 2}}
    assert route_after_code_gen(s2) == "force_test"
    # lint 通过
    s3 = {"last_error": None, "retry_count": {}}
    assert route_after_code_gen(s3) == "lint_ok"


def test_route_after_test_gen():
    # test_gen 一律交给下游 test_run 做真实执行判定
    assert route_after_test_gen({"test_report": {"run": {"passed": 3, "failed": 0}}}) == "run"
    assert route_after_test_gen({"test_report": {"run": {"passed": 0, "failed": 1}}}) == "run"
    assert route_after_test_gen({}) == "run"


# ═══════════════════════════════════════════════════════════════════
# 6. 节点工厂注入隔离：两个不同 providers 实例不串
# ═══════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_factory_isolation():
    p1 = _mock_providers()
    p2 = _mock_providers()
    n1 = make_code_search_node(p1)
    n2 = make_code_search_node(p2)
    state = {
        "requirement": _sample_requirement(),
        "opencode_sessions": {"search": None},
    }
    o1 = await n1.async_version(state)  # type: ignore[arg-type]
    o2 = await n2.async_version(state)  # type: ignore[arg-type]
    # 两轮调用拿到不同 session_id（各自独立 Mock 实例）
    assert o1["opencode_sessions"]["search"] != o2["opencode_sessions"]["search"]
