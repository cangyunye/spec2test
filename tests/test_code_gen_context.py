"""P4 code_gen 节点上下文增强测试。

覆盖：
  - related_files 从 code_context 携带真实 code_snippet（有长度上限）
  - 长 snippet 被截断（保护 token 预算）
  - 无 snippet 字段时降级为只传路径
运行: pytest -v tests/test_code_gen_context.py
"""
from __future__ import annotations

from typing import Any

import pytest

from devflow.nodes.provider_nodes import _build_related_files, make_code_gen_node
from devflow.providers import (
    MockCodeEdit,
    MockCodeGraphRender,
    MockCodeSearch,
    MockTestGen,
    Providers,
)


def test_related_files_carry_snippet_with_cap():
    code_ctx = [
        {
            "file_path": "src/auth.py",
            "symbol_name": "Auth.login",
            "code_snippet": "def login(self):\n    return 'ok'\n",
            "line_start": 1,
            "line_end": 3,
        },
        {
            "file_path": "src/redis.py",
            "symbol_name": "get_conn",
            "code_snippet": "def get_conn():\n    pass\n",
        },
    ]
    files = _build_related_files(code_ctx, max_snippet_chars=100)
    assert len(files) == 2
    assert files[0]["file_path"] == "src/auth.py"
    assert files[0]["symbol"] == "Auth.login"
    assert "def login" in files[0]["code_snippet"]


def test_snippet_capped_at_limit():
    code_ctx = [
        {
            "file_path": "a.py",
            "symbol_name": "f",
            "code_snippet": "x" * 10000,  # 超长
        }
    ]
    files = _build_related_files(code_ctx, max_snippet_chars=200)
    assert len(files[0]["code_snippet"]) <= 200 + 3  # 截断符占位


def test_missing_snippet_degrades_to_path_only():
    code_ctx = [{"file_path": "b.py", "symbol_name": "g"}]
    files = _build_related_files(code_ctx)
    assert files[0]["file_path"] == "b.py"
    assert files[0]["symbol"] == "g"
    assert "code_snippet" not in files[0]


@pytest.mark.asyncio
async def test_code_gen_node_passes_snippet_to_provider(monkeypatch):
    """code_gen 节点把 code_context 的 snippet 传给 Provider（捕获 generate 参数）。"""
    captured: dict[str, Any] = {}

    class _CaptureEdit(MockCodeEdit):
        async def generate(self, project_root, instruction, **kw):
            captured["related_files"] = kw.get("related_files")
            captured["instruction"] = instruction
            return await super().generate(project_root, instruction, **kw)

    node = make_code_gen_node(
        Providers(
            code_search=MockCodeSearch(),
            graph_render=MockCodeGraphRender(),
            code_edit=_CaptureEdit(),
            test_gen=MockTestGen(),
        )
    )
    state = {
        "requirement": {"project_root": "/tmp/prj", "acceptance_criteria": ["AC1"]},
        "logic_graph": {
            "nodes": [
                {"node_id": "n-1", "label": "Auth.login", "is_modified": True},
            ],
            "edges": [],
        },
        "code_context": [
            {"file_path": "src/auth.py", "symbol_name": "Auth.login", "code_snippet": "def login(): ..."}
        ],
        "opencode_sessions": {"code_gen": None},
        "retry_count": {},
        "dead_letters": [],
        "current_stage": "code",
    }
    out = await node.async_version(state)
    assert out["last_error"] is None, out.get("last_error")
    assert captured["related_files"][0]["file_path"] == "src/auth.py"
    assert "def login" in captured["related_files"][0]["code_snippet"]
