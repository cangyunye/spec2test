"""D5 真实 LLM 集成测试（默认跳过，需设置 LLM_LIVE_TESTS=1 + 有效 Key）。

运行方式：
  LLM_LIVE_TESTS=1 LLM_API_KEY=sk-xxx python -m pytest tests/test_live_llm.py -v

覆盖：
  - check-llm 连通性自检返回 ok
  - graph_generate 用真实模型产出合法 LogicGraph（过 validate_logic_graph）
  - clarify_extract 用真实模型抽取需求不空心（回归：网关 function_calling 回空壳）
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


# 内置代表性需求文本（未提供 LLM_LIVE_SPEC_FILE 时使用）：四个断言字段均有可抽取内容
_LIVE_SPEC_TEXT = """\
需求：给现有 FastAPI 用户登录模块增加短信验证码二步验证（2FA）。

项目背景：后端为 FastAPI + Redis 会话，登录接口已上线，前端是 React 单页应用。

接口变更：POST /login 请求体在原有 username、password 外新增可选 sms_code；
成功返回 200 与 {token, uid}，验证码错误返回 401 与 {code, msg}，验证码有效期 5 分钟。

边界与异常场景：密码连续错误 5 次锁定账号 15 分钟；短信验证码重复使用应被拒绝；
验证码过期需返回明确错误码；Redis 不可用时降级为单因子登录并记录告警。

验收标准：开启 2FA 的用户登录必须校验 sms_code；验证码正确且未过期时登录成功并返回 token；
验证码错误或过期返回 401 与可读提示且不写会话；关闭 2FA 的用户登录流程保持不变。
"""


class TestLiveRequirementExtract:
    """真实模型对完整需求文本抽取 RequirementExtract 不空心（回归：网关
    function_calling 只回空参数 tool_call，导致抽取永远为空、反复追问）。

    需求文本默认读 tests/fixtures/test_spec.txt（精确版样例）；
    可用 LLM_LIVE_SPEC_FILE 指定任意文本覆盖（如模糊版 test_spec2.txt）：
      LLM_LIVE_TESTS=1 LLM_LIVE_SPEC_FILE=tests/fixtures/test_spec2.txt \
        python -m pytest tests/test_live_llm.py -k extract -v
    """

    @pytest.mark.asyncio
    async def test_extract_not_hollow_from_full_spec(self):
        import json
        from pathlib import Path

        from langchain_core.messages import HumanMessage

        from devflow.nodes.clarify import clarify_extract_async

        spec_file = os.getenv("LLM_LIVE_SPEC_FILE")
        if not spec_file:
            default_fixture = Path(__file__).parent / "fixtures" / "test_spec.txt"
            spec_file = str(default_fixture) if default_fixture.exists() else None
        text = Path(spec_file).read_text(encoding="utf-8") if spec_file else _LIVE_SPEC_TEXT
        state = {
            "messages": [HumanMessage(content=text)],
            "requirement": empty_requirement(),
            "retry_count": {},
        }
        out = await clarify_extract_async(state)  # type: ignore[arg-type]
        assert out.get("last_error") is None, f"需求抽取失败: {out.get('last_error')}"
        req = out["requirement"]
        missing = [
            f for f in ("project_context", "io_constraints", "edge_cases", "acceptance_criteria")
            if not req.get(f)
        ]
        assert not missing, f"抽取结果空心（function_calling 未真正填字段），缺 {missing}: {req}"
        print(f"\n[LIVE] requirement={json.dumps(req, ensure_ascii=False)[:400]}")
