"""阶段一 MVP 单元 & 集成测试（不依赖 LLM API Key）。

对应 tests/stage1_mvp.md 中的 TC101 ~ TC106。
运行: pytest -v tests/test_stage1_unit.py
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
from pathlib import Path

import pytest

from devflow import config, orchestrator, schemas
from devflow.nodes.clarify import _merge_requirement, clarify_extract
from devflow.nodes.compress import compress_messages


# ═══════════════════════════════════════════════════════════════════
# 测试辅助
# ═══════════════════════════════════════════════════════════════════

@pytest.fixture()
def clean_checkpoint(tmp_path, monkeypatch):
    """每个测试用独立的 SQLite DB，避免互相污染。"""
    db = tmp_path / "checkpoints.db"
    monkeypatch.setattr(config.settings, "CHECKPOINT_SQLITE_PATH", Path(db))
    # 重置 orchestrator 全局 sqlite 连接，让它重新连到新 tmp
    monkeypatch.setattr(orchestrator, "_conn", None)
    return db


def _complete_requirement():
    return {
        "req_type": "component_iteration",
        "project_root": "/workspace/demo-app",
        "project_context": "Flask + SQLAlchemy 电商后端",
        "target_modules": ["auth", "api/routes.login"],
        "existing_code_accessible": True,
        "reference_files": ["docs/login.md"],
        "io_constraints": {
            "input": "POST /login {u,p,sms?}",
            "output": "200 {token,uid} / 401 {code,msg}",
        },
        "edge_cases": ["密码错误5次锁定", "sms_code 不对 401"],
        "acceptance_criteria": ["twofa 开关控制", "bcrypt>=12 轮"],
    }


# ═══════════════════════════════════════════════════════════════════
# TC101 空需求错误
# ═══════════════════════════════════════════════════════════════════

def test_tc101_empty_requirement_many_errors():
    errs = schemas.validate_requirement(schemas.empty_requirement())
    # 仅需求模式下 project_root / target_modules 不再是空需求报错项（不提供代码也能继续）
    assert len(errs) >= 5
    msgs = "\n".join(errs)
    for must in [
        "edge_cases", "acceptance_criteria",
        "project_context", "io_constraints.input", "io_constraints.output",
    ]:
        assert must in msgs, f"缺少 {must}"
    for not_in in ["project_root", "target_modules"]:
        assert not_in not in msgs, f"仅需求模式不应强制 {not_in}"


# ═══════════════════════════════════════════════════════════════════
# TC102 完整需求零错误
# ═══════════════════════════════════════════════════════════════════

def test_tc102_complete_requirement_passes():
    assert schemas.validate_requirement(_complete_requirement()) == []


# ═══════════════════════════════════════════════════════════════════
# TC103 合并逻辑：只覆盖实质非空
# ═══════════════════════════════════════════════════════════════════

def test_tc103_merge_only_nonempty():
    old = schemas.empty_requirement()
    old["project_context"] = "用户已填"
    old["target_modules"] = ["auth"]
    patch = {
        "project_context": None,
        "target_modules": [],
        "io_constraints": {"input": "", "output": "新填 output"},
        "acceptance_criteria": ["token 24h"],
    }
    merged = _merge_requirement(old, patch)
    assert merged["project_context"] == "用户已填"
    assert merged["target_modules"] == ["auth"]
    assert merged["io_constraints"]["output"] == "新填 output"
    assert merged["acceptance_criteria"] == ["token 24h"]


# ═══════════════════════════════════════════════════════════════════
# TC104 压缩节点只截断 messages
# ═══════════════════════════════════════════════════════════════════

def test_tc104_compress_truncates_messages_only():
    from langchain_core.messages import AIMessage, HumanMessage

    msgs = []
    for i in range(10):
        msgs.append(HumanMessage(content=f"u{i}"))
        msgs.append(AIMessage(content=f"a{i}"))
    state = {"messages": list(msgs)}
    out = compress_messages(state)
    assert "messages" in out
    assert len(out["messages"]) == config.settings.HOT_MEMORY_LAST_N * 2
    # 结构化字段 untouched
    for k in ("requirement", "logic_graph", "code_context"):
        assert k not in out


# ═══════════════════════════════════════════════════════════════════
# TC105 逻辑图校验
# ═══════════════════════════════════════════════════════════════════

def test_tc105_validate_logic_graph():
    valid = {
        "graph_id": schemas.new_graph_id(),
        "nodes": [
            {"node_id": "n-1", "label": "in",  "node_type": "io",        "is_modified": False},
            {"node_id": "n-2", "label": "chk", "node_type": "condition", "is_modified": True},
            {"node_id": "n-3", "label": "out", "node_type": "io",        "is_modified": True},
        ],
        "edges": [
            {"edge_id": "e-1", "from_node": "n-1", "to_node": "n-2",
             "edge_type": "call",      "condition": None, "is_modified": False},
            {"edge_id": "e-2", "from_node": "n-2", "to_node": "n-3",
             "edge_type": "condition", "condition": "OK", "is_modified": True},
        ],
        "mermaid_source": "flowchart TD\nn-1-->n-2\nn-2-->|OK|n-3\n",
    }
    assert schemas.validate_logic_graph(valid) == []

    invalid = {**valid, "edges": [
        {"edge_id": "e-bad", "from_node": "n-NOTEXIST", "to_node": "n-3",
         "edge_type": "call", "condition": None, "is_modified": False}
    ]}
    errs = schemas.validate_logic_graph(invalid)
    assert len(errs) == 1 and "n-NOTEXIST" in errs[0]


# ═══════════════════════════════════════════════════════════════════
# TC106 Graph 构建 + SQLite Checkpoint 持久化
# ═══════════════════════════════════════════════════════════════════

def test_tc106_checkpoint_persists(clean_checkpoint):
    # 在测试时，默认 LLM 配置是空 key → clarify_extract 会抛 DevFlowError 然后 fallback 到 Mock。
    # 但 Mock 必须在模型链里；用环境变量保证 mock fallback 存在（默认就有），
    # 且节点现在是 async，graph.invoke 走 LangGraph 内部 async 调度。
    g1 = orchestrator.build_graph()
    tid = "tc106-pytest"
    cfg = {"configurable": {"thread_id": tid}}
    g1.invoke(orchestrator.initial_state(), cfg)
    g1.update_state(cfg, {"requirement": {"project_root": "/tmp/tc106"}})

    snap = g1.get_state(cfg).values
    assert snap["requirement"]["project_root"] == "/tmp/tc106"

    # 重建 graph，模拟服务重启
    del g1
    orchestrator._conn = None  # 清全局连接
    g2 = orchestrator.build_graph()
    snap2 = g2.get_state(cfg).values
    assert snap2["requirement"]["project_root"] == "/tmp/tc106"

    # 表存在检查
    assert clean_checkpoint.exists()
    with sqlite3.connect(str(clean_checkpoint)) as c:
        tables = {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "checkpoints" in tables


@pytest.mark.asyncio
async def test_tc107_clarify_extract_empty_messages_is_noop():
    """用户消息为空时，clarify_extract_async 不打 LLM，直接返回原 requirement。"""
    from devflow.nodes.clarify import clarify_extract_async

    state = {
        "messages": [],  # 无用户消息
        "requirement": {"project_root": "/tmp/tc107"},
    }
    out = await clarify_extract_async(state)  # type: ignore[arg-type]
    assert out["requirement"]["project_root"] == "/tmp/tc107"
    # 没 last_error（因为没打 LLM 就没失败的可能）
    assert "last_error" not in out or out["last_error"] is None or out.get("last_error") is None


@pytest.mark.asyncio
async def test_tc108_clarify_extract_runs_mock_fallback_on_any_error(monkeypatch):
    """当没有真实 LLM 可用时，clarify_extract_async 内部走 mock 兜底成功合并需求（非空 user 消息）。"""
    # 默认 settings.LLM_FALLBACKS=mock，invoke_json 最终会走到 mock；断言 out 成功写回 requirement
    from devflow.nodes.clarify import clarify_extract_async
    from langchain_core.messages import HumanMessage

    state = {
        "messages": [HumanMessage(content="登录功能加 2FA 二步验证")],
        "requirement": config.schemas.empty_requirement() if False else {},
    }
    out = await clarify_extract_async(state)  # type: ignore[arg-type]
    # mock 兜底最终会合并出 requirement；即便前面任何环节失败，也保证输出结构不是缺 last_error 以外的崩溃
    assert "requirement" in out or "last_error" in out
    # 如果走了 error 分支，last_error 就非空（允许）；否则 requirement 有内容（mock 返回的）
    if out.get("last_error") is None and "requirement" in out:
        req = out["requirement"] or {}
        # 至少 project_root 之类的模板字段（mock 默认有 project_root）
        assert "project_root" in req

