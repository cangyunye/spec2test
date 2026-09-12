"""Web 断线恢复与步骤回退端点测试。

覆盖：POST 推进立即返回 + RunBus 缓冲回放（游标补齐）、运行状态可见化
（running/last_seq）、busy 409、history 锚点、revert 回退改需求并自动续跑、
运行中禁止删除会话。

Mock LLM 驱动（conftest 强制），不依赖网络。
运行: pytest -v tests/test_web_recovery.py
"""
from __future__ import annotations

import json
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
    with TestClient(app) as c:  # with 触发 startup（重启续跑扫描；tmp runs 目录为空）
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


def _collect_events(client: TestClient, tid: str, after: int) -> list[dict]:
    """消费 GET /events 直到服务端关闭流（回放完整或 run 结束）。含 stream_meta 首帧。"""
    events: list[dict] = []
    with client.stream("GET", f"/api/sessions/{tid}/events?after={after}") as resp:
        assert resp.status_code == 200
        for line in resp.iter_lines():
            if not line.startswith("data:"):
                continue
            events.append(json.loads(line[len("data:"):].strip()))
    return events


def _run_events(client: TestClient, tid: str, after: int = 0) -> list[dict]:
    """仅运行事件（剔除 stream_meta 首帧）。"""
    return [e for e in _collect_events(client, tid, after) if e["type"] != "stream_meta"]


# ═══════════════════════════════════════════════════════════════════
# 运行状态可见化 + busy
# ═══════════════════════════════════════════════════════════════════


def test_snapshot_and_list_expose_running_and_last_seq(client):
    tid = _create_session(client)
    snap = client.get(f"/api/sessions/{tid}").json()
    assert snap["running"] is False and "last_seq" in snap
    entry = next(e for e in client.get("/api/sessions").json() if e["thread_id"] == tid)
    assert "running" in entry and "last_seq" in entry


def test_post_message_conflicts_when_session_running(client):
    tid = _create_session(client)
    ws._bus(tid).running = True  # 模拟已有 run 在推进
    r = client.post(f"/api/sessions/{tid}/messages", json={"text": "hi"})
    assert r.status_code == 409


def test_delete_conflicts_when_session_running(client):
    tid = _create_session(client)
    ws._bus(tid).running = True  # 模拟已有 run 在推进
    r = client.delete(f"/api/sessions/{tid}")
    assert r.status_code == 409


# ═══════════════════════════════════════════════════════════════════
# RunBus：立即返回 + 缓冲回放 + 游标
# ═══════════════════════════════════════════════════════════════════


def test_message_returns_immediately_and_events_replayable(client):
    """POST 立即返回 accepted；run 结束后事件仍可整轮回放（模拟浏览器关闭再打开）。"""
    tid = _create_session(client)
    r = client.post(f"/api/sessions/{tid}/messages", json={
        "text": "开发一个桌面计算器，支持四则运算与除零报错提示"})
    assert r.status_code == 200, r.text
    assert r.json()["accepted"] is True
    _wait_idle(client, tid)

    events = _run_events(client, tid, after=0)
    types = [e["type"] for e in events]
    assert "node_done" in types, f"回放应包含节点完成事件: {types}"
    assert types[-1] == "stream_end"
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs), "seq 应严格递增"


def test_events_cursor_skips_already_seen(client):
    """after=游标 → 只回放其后的事件（断线重连零重复）。"""
    tid = _create_session(client)
    client.post(f"/api/sessions/{tid}/messages", json={
        "text": "开发一个桌面计算器，支持四则运算与除零报错提示"})
    _wait_idle(client, tid)
    events = _run_events(client, tid, after=0)
    assert len(events) >= 3
    cursor = events[0]["seq"]
    tail = _run_events(client, tid, after=cursor)
    assert [e["seq"] for e in tail] == [e["seq"] for e in events[1:]]


# ═══════════════════════════════════════════════════════════════════
# history / revert
# ═══════════════════════════════════════════════════════════════════


def _drive_to_review_gate(client: TestClient, tid: str) -> None:
    r = client.post(f"/api/sessions/{tid}/messages", json={
        "text": "开发一个桌面计算器，支持四则运算与除零报错提示"})
    assert r.status_code == 200
    _wait_idle(client, tid)
    snap = client.get(f"/api/sessions/{tid}").json()
    assert "requirement_review" in snap["next"], f"应停在需求确认门禁: {snap['next']}"


def test_history_lists_node_anchors(client):
    tid = _create_session(client)
    _drive_to_review_gate(client, tid)
    steps = client.get(f"/api/sessions/{tid}/history").json()["steps"]
    nodes = [s["node"] for s in steps]
    assert "clarify_extract" in nodes and "compress_messages" in nodes
    from devflow.events import NODE_LABELS

    for s in steps:
        assert s["checkpoint_id"] and s["label"]
        assert s["node"] in NODE_LABELS, f"锚点必须是真实节点，不能是 input/update 检查点: {s}"
        assert isinstance(s["message_ids"], list)


def test_snapshot_messages_carry_ids(client):
    """快照消息带 id：前端把每条气泡锚到回退步骤的凭据。"""
    tid = _create_session(client)
    _drive_to_review_gate(client, tid)
    msgs = client.get(f"/api/sessions/{tid}").json()["values"]["messages"]
    assert msgs, "应有对话消息"
    assert all(m.get("id") for m in msgs), f"每条消息都应带 id: {msgs}"


def test_revert_with_field_edits_reruns_downstream(client):
    """回退到抽取完成时点 + 就地改需求：自动续跑后新需求生效并再次到达门禁。"""
    tid = _create_session(client)
    _drive_to_review_gate(client, tid)
    steps = client.get(f"/api/sessions/{tid}/history").json()["steps"]
    anchor = next(s for s in steps if s["node"] == "clarify_extract")

    r = client.post(f"/api/sessions/{tid}/revert", json={
        "checkpoint_id": anchor["checkpoint_id"],
        "fields": {"project_context": "回退时改的项目背景"},
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["run"]["accepted"] is True
    assert body["node"] == "clarify_extract"

    _wait_idle(client, tid)
    snap = client.get(f"/api/sessions/{tid}").json()
    assert snap["values"]["requirement"]["project_context"] == "回退时改的项目背景"
    assert "requirement_review" in snap["next"], "续跑应重新到达需求确认门禁"


def test_revert_unknown_checkpoint_is_400(client):
    tid = _create_session(client)
    r = client.post(f"/api/sessions/{tid}/revert", json={"checkpoint_id": "nonexistent"})
    assert r.status_code == 400
