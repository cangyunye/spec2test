"""门禁挂起时消息通道防护测试。

会话停在 interrupt 门禁（如需求确认）上时，POST /messages 必须拒绝（409）：
此时从消息通道再发输入会从 START 重跑一轮、脱离挂起点的恢复上下文。

Mock LLM 驱动（conftest 强制）：mock 兜底直接给出完整演示需求，
首轮消息即跑到需求确认门禁并挂起，可确定性构造门禁挂起态。
运行: pytest -v tests/test_gate_message_guard.py
"""
from __future__ import annotations

import time
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

import web.server as ws
from web.server import app


@pytest.fixture()
def client() -> Iterator[TestClient]:
    ws._buses.clear()
    ws._tid_locks.clear()
    with TestClient(app) as c:  # with 触发 startup（tmp runs 目录为空，无续跑）
        yield c
    ws._buses.clear()
    ws._tid_locks.clear()


def _create_session(client: TestClient) -> str:
    tid = f"w-{uuid.uuid4().hex[:8]}"
    r = client.post("/api/sessions", json={"thread_id": tid, "set_fields": []})
    assert r.status_code == 200
    return tid


def _wait_idle(client: TestClient, tid: str, timeout: float = 30) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not client.get(f"/api/sessions/{tid}").json()["running"]:
            return
        time.sleep(0.05)
    raise AssertionError("会话运行超过 30s 仍未空闲")


def test_message_rejected_at_requirement_review_gate(client):
    """端到端：mock 完整需求 → 首轮停在需求确认门禁；挂起中发「继续」→ 409。"""
    tid = _create_session(client)
    r = client.post(f"/api/sessions/{tid}/messages", json={"text": "做个桌面计算器"})
    assert r.status_code == 200 and r.json()["accepted"] is True
    _wait_idle(client, tid)

    # mock 需求字段齐全 → validate 通过 → 停在 requirement_review 门禁等用户确认
    graph = ws.build_graph_with_providers()
    assert ws._gate_pending(graph, tid) is True

    r = client.post(f"/api/sessions/{tid}/messages", json={"text": "继续"})
    assert r.status_code == 409
    assert "门禁" in r.json()["detail"]


def test_gate_pending_false_for_missing_thread(client):
    """_gate_pending：不存在的会话返回 False（不拦正常消息）。"""
    graph = ws.build_graph_with_providers()
    assert ws._gate_pending(graph, "w-nonexistent") is False
