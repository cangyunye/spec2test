"""用户终止流程：POST /stop 协作停止 + POST /resume 断点续跑。

慢图模拟（monkeypatch build_graph_with_providers / _thread_exists）确定性验证
协作中断：停止标志在下一个流事件边界生效，事件以 stopped → stream_end 收尾，
锁释放、run 标记清除、断点留在 checkpoint（快照 resumable=True）。
mock LLM 驱动（conftest 强制）验证真实图下的 409 矩阵：新鲜会话与门禁挂起态
既不可 stop 也不可 resume。
运行: pytest -v tests/test_stop_resume.py
"""
from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from types import SimpleNamespace

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


def _wait_running(tid: str, timeout: float = 10) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if ws._session_running(tid):
            return
        time.sleep(0.02)
    raise AssertionError("run 未在 10s 内启动")


def _task(name: str, intr: bool = False):
    """假挂起任务：interrupts 形状与 PregelTask 一致（空元组 = 无中断）。"""
    return SimpleNamespace(name=name, interrupts=(object(),) if intr else ())


def _event_types(tid: str) -> list[str]:
    return [ev.get("type") for _, ev in ws._bus(tid).buffer]


class _FakeGraph:
    """慢图：stream 逐个吐 update 事件（默认间隔 5ms），供 /stop 在半途命中。

    get_state 返回可配置的假快照；stream 收到的 input 全部记录（resume 断言
    stream(None)）。事件形状走真实 events_from_stream 协议（updates 元组）。
    """

    def __init__(self) -> None:
        self.snap = SimpleNamespace(next=(), tasks=[], values={})
        self.max_events = 400
        self.interval = 0.005
        self.inputs: list = []

    def stream(self, input_msg, config, stream_mode=None):  # noqa: ARG002
        self.inputs.append(input_msg)
        for i in range(self.max_events):
            yield ("updates", {f"slow_{i}": {"n": i}})
            time.sleep(self.interval)

    def get_state(self, config):  # noqa: ARG002
        return self.snap


@pytest.fixture()
def fake_graph(monkeypatch):
    g = _FakeGraph()
    monkeypatch.setattr(ws, "build_graph_with_providers", lambda: g)
    monkeypatch.setattr(ws, "_thread_exists", lambda tid: True)
    return g


# ── stop：参数校验与协作中断 ──────────────────────────────────────


def test_stop_404_and_409(client):
    """不存在的会话 404；存在但没有 run 在推进（新鲜会话）409。"""
    r = client.post("/api/sessions/w-nope/stop")
    assert r.status_code == 404
    r = client.post("/api/sessions/w-nope/resume")
    assert r.status_code == 404  # stop/resume 共用 404 分支

    tid = _create_session(client)
    r = client.post(f"/api/sessions/{tid}/stop")
    assert r.status_code == 409
    assert "没有正在推进" in r.json()["detail"]


def test_stop_midway_cooperative(fake_graph, client):
    """半途终止：标志在下一个流事件边界生效，run 收敛且断点可续。"""
    tid = "w-fake-stop"
    r = client.post(f"/api/sessions/{tid}/messages", json={"text": "慢流程"})
    assert r.status_code == 200 and r.json()["accepted"] is True
    _wait_running(tid)

    r = client.post(f"/api/sessions/{tid}/stop")
    assert r.status_code == 200
    assert r.json() == {"accepted": True, "stopping": True}
    assert ws._bus(tid).stop_requested is True
    _wait_idle(client, tid, timeout=15)

    # 收敛：锁释放、run 标记清除、事件以 stopped → stream_end 收尾
    assert not ws._tid_lock(tid).locked()
    assert not (ws._runs_dir() / f"{tid}.json").exists()
    types = _event_types(tid)
    assert types[-2:] == ["stopped", "stream_end"]
    # 协作中断：远早于 400 个事件就停了
    assert types.count("node_done") < 200

    # 停止后：快照恢复半途态（next 非空 + 无门禁 + 有进展 + 未运行）
    fake_graph.snap = SimpleNamespace(
        next=("code_gen",), tasks=[_task("code_gen")],
        values={"messages": [{"role": "user", "content": "慢流程"}], "current_stage": "code"},
    )
    snap = client.get(f"/api/sessions/{tid}").json()
    assert snap["resumable"] is True
    assert snap["running"] is False


# ── resume：409 矩阵与断点续跑 ────────────────────────────────────


def test_resume_409_matrix(fake_graph, client):
    """门禁挂起 / 无半途（已完成或未开始）/ 推进中，全拒（404 由真图用例覆盖）。"""
    tid = "w-fake-resume"

    # 门禁挂起：走门禁通道，不接受 resume
    fake_graph.snap = SimpleNamespace(
        next=("requirement_review",), tasks=[_task("requirement_review", True)],
        values={"messages": ["m"]},
    )
    r = client.post(f"/api/sessions/{tid}/resume")
    assert r.status_code == 409 and "门禁" in r.json()["detail"]

    # next 为空：已完成
    fake_graph.snap = SimpleNamespace(next=(), tasks=[], values={"messages": ["m"]})
    r = client.post(f"/api/sessions/{tid}/resume")
    assert r.status_code == 409 and "半途" in r.json()["detail"]

    # 无挂起任务 + 无门禁：等同未开始/已完成，不算半途
    fake_graph.snap = SimpleNamespace(
        next=("compress_messages",), tasks=[], values={"messages": []},
    )
    r = client.post(f"/api/sessions/{tid}/resume")
    assert r.status_code == 409 and "半途" in r.json()["detail"]

    # 推进中：409（run 在跑时即使处于半途态也先拒）
    # 注：半途态下消息通道被守卫挡住，先以非半途快照起跑再翻转到半途
    fake_graph.snap = SimpleNamespace(next=(), tasks=[], values={"messages": []})
    r = client.post(f"/api/sessions/{tid}/messages", json={"text": "x"})
    assert r.status_code == 200
    _wait_running(tid)
    fake_graph.snap = SimpleNamespace(
        next=("code_gen",), tasks=[_task("code_gen")],
        values={"messages": ["m"], "current_stage": "code"},
    )
    r = client.post(f"/api/sessions/{tid}/resume")
    assert r.status_code == 409 and "推进中" in r.json()["detail"]
    client.post(f"/api/sessions/{tid}/stop")
    _wait_idle(client, tid, timeout=15)


def test_resume_happy_path(fake_graph, client):
    """半途停止态 resume：stream(None) 续跑到下一停点，终止标志不泄漏。"""
    tid = "w-fake-resume-go"
    fake_graph.snap = SimpleNamespace(
        next=("code_gen",), tasks=[_task("code_gen")],
        values={"messages": ["m"], "current_stage": "code"},
    )
    ws._bus(tid).stop_requested = True  # 模拟上一轮遗留的终止请求
    fake_graph.max_events = 3
    fake_graph.interval = 0

    r = client.post(f"/api/sessions/{tid}/resume")
    assert r.status_code == 200 and r.json()["accepted"] is True
    assert ws._bus(tid).stop_requested is False  # start_run 复位
    _wait_idle(client, tid, timeout=15)

    assert fake_graph.inputs[-1] is None  # resume = stream(None) 恢复挂起节点
    types = _event_types(tid)
    assert "stopped" not in types
    assert types[-1] == "stream_end"


def test_send_message_blocked_midway(fake_graph, client):
    """半途停止态从消息通道发输入会触发 START 重规划，必须 409 引导先继续。"""
    tid = "w-fake-msg"
    fake_graph.snap = SimpleNamespace(
        next=("code_gen",), tasks=[_task("code_gen")],
        values={"messages": ["m"], "current_stage": "code"},
    )
    r = client.post(f"/api/sessions/{tid}/messages", json={"text": "补充"})
    assert r.status_code == 409
    assert "继续" in r.json()["detail"]


# ── 真图（mock LLM）：新鲜会话与门禁挂起态 ────────────────────────


def test_real_flow_fresh_and_gate_states(client):
    """新鲜会话不可 stop/resume；跑到门禁挂起后同样全拒；resumable 恒为 False。"""
    tid = _create_session(client)
    snap = client.get(f"/api/sessions/{tid}").json()
    assert snap["resumable"] is False
    r = client.post(f"/api/sessions/{tid}/resume")
    assert r.status_code == 409  # 未开始
    r = client.post(f"/api/sessions/{tid}/stop")
    assert r.status_code == 409

    # 首条消息 → mock 完整需求 → 停在需求确认门禁
    r = client.post(f"/api/sessions/{tid}/messages", json={"text": "做个桌面计算器"})
    assert r.status_code == 200
    _wait_idle(client, tid)

    graph = ws.build_graph_with_providers()
    assert ws._gate_pending(graph, tid) is True
    snap = client.get(f"/api/sessions/{tid}").json()
    assert snap["resumable"] is False  # 门禁 ≠ 半途
    r = client.post(f"/api/sessions/{tid}/resume")
    assert r.status_code == 409 and "门禁" in r.json()["detail"]
    r = client.post(f"/api/sessions/{tid}/stop")
    assert r.status_code == 409  # 门禁等待期没有 run 在跑
