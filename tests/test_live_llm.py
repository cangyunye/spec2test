"""D5 真实 LLM 集成测试（默认跳过，需设置 LLM_LIVE_TESTS=1 + 有效 Key）。

运行方式：
  LLM_LIVE_TESTS=1 LLM_API_KEY=sk-xxx python -m pytest tests/test_live_llm.py -v

覆盖：
  - check-llm 连通性自检返回 ok
  - graph_generate 用真实模型产出合法 LogicGraph（过 validate_logic_graph）
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from devflow.llm_client import check_llm_all
from devflow.nodes.graph_gen import graph_generate_async
from devflow.schemas import empty_requirement, validate_logic_graph

pytestmark = pytest.mark.skipif(
    os.getenv("LLM_LIVE_TESTS") != "1",
    reason="需要真实 LLM API Key（LLM_LIVE_TESTS=1）",
)


def _full_requirement() -> dict:
    req = empty_requirement()
    req.update({
        "req_type": "new_feature",
        "project_root": "/workspace",
        "project_context": "FastAPI 用户登录模块，带 2FA 二步验证，Redis 存会话",
        "target_modules": ["app/auth/login.py"],
        "existing_code_accessible": True,
        "io_constraints": {
            "input": "POST /login {username, password, sms_code?}",
            "output": "200 {token, uid} / 401 {code, msg}",
        },
        "edge_cases": ["密码错误 5 次锁定", "sms_code 错误 401", "token 过期"],
        "acceptance_criteria": [
            "登录成功返回 JWT token",
            "密码错误返回 401 与错误码",
            "开启 2FA 时校验 sms_code",
        ],
    })
    return req


class TestLiveCheckLlm:
    @pytest.mark.asyncio
    async def test_check_llm_reports_ok(self):
        reports = await check_llm_all()
        assert reports, "未配置任何 LLM provider（LLM_PROVIDERS_JSON / LLM_BASE_URL）"
        ok = [r for r in reports if r["ok"]]
        assert ok, f"所有 provider 不可用: {reports}"


class TestLiveGraphGenerate:
    @pytest.mark.asyncio
    async def test_real_llm_produces_valid_logic_graph(self):
        state = {"requirement": _full_requirement(), "code_context": [], "retry_count": {}}
        out = await graph_generate_async(state)  # type: ignore[arg-type]
        assert out.get("last_error") is None, f"真实 LLM 制图失败: {out.get('last_error')}"
        graph = out.get("logic_graph")
        assert graph is not None
        assert validate_logic_graph(graph) == [], validate_logic_graph(graph)
        assert len(graph["nodes"]) >= 3
        assert len(graph["edges"]) >= 2
        print(f"\n[LIVE] graph_id={graph['graph_id']} nodes={len(graph['nodes'])} edges={len(graph['edges'])}")
