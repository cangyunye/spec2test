"""Web 会话列表标题优先级（纯函数测试，不依赖 DB / LLM）。

优先级：state.session_title（澄清阶段 LLM 起名）→ project_context 截断 → 首条用户消息。
运行: pytest -v tests/test_web_sessions.py
"""
from __future__ import annotations

from web.server import _session_title


def test_prefers_session_title_from_state():
    assert _session_title({"session_title": "计算器科学计算"}) == "计算器科学计算"


def test_falls_back_to_project_context_truncation():
    vals = {"session_title": "", "requirement": {"project_context": "A" * 50}}
    assert _session_title(vals) == "A" * 30


def test_falls_back_to_context_when_title_blank():
    vals = {"session_title": "   ", "requirement": {"project_context": "订单导出 Excel"}}
    assert _session_title(vals) == "订单导出 Excel"


def test_empty_when_nothing_available():
    assert _session_title({}) == ""
    assert _session_title({"requirement": {}}) == ""


def test_library_node_api_found_and_404(tmp_path):
    from fastapi.testclient import TestClient

    from devflow.checklist.library import write_checklist
    from web.server import app

    write_checklist(
        tmp_path,
        "payment",
        "---\nname: 支付\n---\n## 使用场景\n支付",
        "## 正向\n- [P0] 支付成功",
    )
    c = TestClient(app)
    r = c.get("/api/library/node", params={"rel_dir": "payment", "library_root": str(tmp_path)})
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "支付"
    assert body["sections"][0]["items"][0]["priority"] == "P0"
    missing = c.get("/api/library/node", params={"rel_dir": "nope", "library_root": str(tmp_path)})
    assert missing.status_code == 404
