"""评审期人工补录用例：编号接续 / 重做保护 / 接口守卫 / 采纳与驳回闭环。

覆盖四层：
  1. 纯函数：next_case_id 编号接续、reappend_manual_cases 重追加去重
  2. CSV：cases_to_csv 来源列（人工/AI）
  3. HTTP 守卫：非评审时机 409、必填校验 400、normalize mock 降级
  4. 全链路 e2e：预置完整需求（跳过 mock 澄清循环，同 test_stage3_e2e 手法）
     直接驱动图到终审门禁 → HTTP 补录（update_state 落库后门禁仍挂起）
     → 采纳含人工用例通过；另一会话驳回重做 → 人工用例自动重追加

运行: pytest -v tests/test_manual_cases.py
"""
from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from langgraph.types import Command

import web.server as ws
from devflow.auto_run import cases_to_csv
from devflow.cli import _config
from devflow.feature_split import next_case_id, reappend_manual_cases
from devflow.orchestrator import build_graph_with_providers, initial_state
from devflow.schemas import empty_requirement
from web.server import app


# ═══════════════════════════════════════════════════════════════════
# 1. 纯函数：编号接续 + 重追加
# ═══════════════════════════════════════════════════════════════════


def test_next_case_id_continues_from_max():
    cases = [{"case_id": "TC-001"}, {"case_id": "TC-007"}, {"case_id": "weird"}]
    assert next_case_id(cases) == "TC-008"
    assert next_case_id([]) == "TC-001"
    assert next_case_id([{"case_id": None}]) == "TC-001"


def test_reappend_manual_cases_renumbers_and_dedupes():
    report = {"test_cases": [{"case_id": "TC-001", "title": "a"}, {"case_id": "TC-002", "title": "b"}]}
    manual = [
        {"case_id": "TC-003", "title": "人工一", "steps": "1. x", "expected": "y", "origin": "manual"},
        # AI 重做后恰好也出了同名场景：同 id 已在报告中的不重复追加
        {"case_id": "TC-001", "title": "a", "steps": "1. x", "expected": "y"},
        {"case_id": "TC-009", "title": "", "steps": "1. x", "expected": "y"},  # 无标题的脏数据跳过
    ]
    n = reappend_manual_cases(report, manual)
    ids = [c["case_id"] for c in report["test_cases"]]
    assert n == 1
    assert ids == ["TC-001", "TC-002", "TC-003"]
    assert report["test_cases"][-1]["title"] == "人工一"
    assert report["test_cases"][-1]["origin"] == "manual"


def test_reappend_manual_cases_empty_noop():
    report = {"test_cases": [{"case_id": "TC-001", "title": "a"}]}
    assert reappend_manual_cases(report, []) == 0


# ═══════════════════════════════════════════════════════════════════
# 2. CSV 来源列
# ═══════════════════════════════════════════════════════════════════


def test_cases_to_csv_has_origin_column():
    rows = cases_to_csv([
        {"case_id": "TC-001", "tier": "functional", "priority": "P0", "title": "a", "origin": "manual"},
        {"case_id": "TC-002", "tier": "functional", "priority": "P1", "title": "b"},
    ]).split("\r\n")
    assert "来源" in rows[0]
    assert rows[0].endswith('"来源"')
    assert '"人工"' in rows[1]
    assert '"AI"' in rows[2]


# ═══════════════════════════════════════════════════════════════════
# 3+4. HTTP 接口（mock provider 隔离 .env 的 pi 配置）
# ═══════════════════════════════════════════════════════════════════


@pytest.fixture()
def client(monkeypatch) -> Iterator[TestClient]:
    # .env 可能把三个能力指向真实 pi CLI：测试强制 mock，杜绝外部调用
    for key in ("CODE_SEARCH_PROVIDER", "CODE_EDIT_PROVIDER", "TEST_GEN_PROVIDER"):
        monkeypatch.setenv(key, "mock")
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


def _no_code_requirement() -> dict[str, Any]:
    """完整且仅需求模式的需求（has_project_code=False：跳过代码检索/生成/执行）。"""
    req = empty_requirement()
    req.update({
        "req_type": "new_feature",
        "project_root": "",
        "project_context": "一个待办清单 Web 应用，支持任务增删改查与完成标记",
        "target_modules": [],
        "existing_code_accessible": False,
        "io_constraints": {"input": "任务文本输入", "output": "任务列表与状态反馈"},
        "edge_cases": ["空任务名", "超长任务名"],
        "acceptance_criteria": ["任务可新增并展示", "完成任务有明确状态标识"],
    })
    return req


# 门禁 → resume 决策（requirement_review 预置需求字段来源明确，多半直接放行不弹）
_GATE_RESUMES: dict[str, Command] = {
    "requirement_review": Command(resume={"decision": "confirm"}),
    "graph_type_select": Command(resume="flowchart"),
    "graph_review": Command(resume="approve"),
    "checklist_route_gate": Command(resume={"decision": "skip"}),
    "feature_gate": Command(resume={"decision": "skip"}),
}


def _drive_until(graph, config, target_node: str, max_rounds: int = 25):
    """推进图直到停在 target_node；门禁显式 resume，非门禁暂停点（错误恢复）
    用 stream(None) 续跑。返回到达时的 snapshot。"""
    last = None
    for _ in range(max_rounds):
        last = graph.get_state(config)
        nxt = [str(n) for n in (last.next or [])]
        if target_node in nxt:
            return last
        if not nxt:
            raise AssertionError(
                f"流程提前结束 stage={last.values.get('current_stage')} "
                f"last_error={last.values.get('last_error')}")
        cmd = next((r for node, r in _GATE_RESUMES.items() if node in nxt), None)
        list(graph.stream(cmd, config, stream_mode="updates"))  # None=续跑错误恢复点
    raise AssertionError(f"超过 {max_rounds} 轮仍未停在 {target_node}（next={last.next}）")


def _park_at_review(client: TestClient, tid: str):
    """建好的会话预置仅需求需求 → 直接驱动到终审门禁。返回 (graph, config)。"""
    graph = build_graph_with_providers()
    config = _config(thread_id=tid)
    graph.update_state(config, {"requirement": _no_code_requirement()})
    _drive_until(graph, config, "review")
    return graph, config


_CASE = {"title": "无凭证用户访问订单页被重定向到登录",
         "steps": "1. 退出登录\n2. 直接访问 /orders\n3. 观察跳转",
         "expected": "重定向到登录页并提示先登录",
         "target": "订单", "precondition": "系统已部署",
         "normalize": True}


def test_add_guard_rejected_at_non_review_gate(client):
    """非评审时机（停在图种类选择门禁）补录 → 409。"""
    tid = _create_session(client)
    graph = build_graph_with_providers()
    config = _config(thread_id=tid)
    graph.update_state(config, {"requirement": _no_code_requirement()})
    _drive_until(graph, config, "graph_type_select")
    r = client.post(f"/api/sessions/{tid}/cases", json=_CASE)
    assert r.status_code == 409
    assert "评审" in r.json()["detail"]


def test_add_case_missing_required_fields(client):
    """必填校验（标题/步骤/预期）→ 400，先于时机守卫暴露表单问题。"""
    tid = _create_session(client)
    r = client.post(f"/api/sessions/{tid}/cases",
                    json={"title": "只有标题", "normalize": False})
    assert r.status_code == 400
    assert "必填" in r.json()["detail"]


def test_normalize_mock_fallback_degrades(client):
    """mock 兜底的「规范化」是编造数据，必须降级为未规范化而非污染用户输入。"""
    tid = _create_session(client)
    r = client.post(f"/api/sessions/{tid}/cases/normalize",
                    json={"text": "登录密码输错五次后账号锁定 30 分钟"})
    assert r.status_code == 200
    body = r.json()
    assert body["normalized"] is False
    assert body["reason"]

    r2 = client.post(f"/api/sessions/{tid}/cases/normalize", json={"text": "  "})
    assert r2.status_code == 400


def test_add_adopt_delete_full_flow(client):
    """全链路：驱动到终审 → 补录两条（编号接续）→ 门禁仍挂起 → 删除一条 →
    采纳剩余人工用例随终审通过落 adopted_cases，流程到 done。"""
    tid = _create_session(client)
    graph, config = _park_at_review(client, tid)

    r1 = client.post(f"/api/sessions/{tid}/cases", json=_CASE)
    assert r1.status_code == 200, r1.text
    body1 = r1.json()
    assert body1["accepted"] is True
    assert body1["normalized"] is False  # mock 兜底：降级原文入库，不阻塞
    id1 = body1["case"]["case_id"]
    assert id1.startswith("TC-")
    assert body1["case"]["origin"] == "manual"
    assert body1["report"]["test_cases"][-1]["case_id"] == id1

    # update_state 落库后，挂起的终审门禁必须原样保留（仍可 Command(resume) 恢复）
    assert ws._gate_pending(graph, tid) is True
    vals = graph.get_state(config).values or {}
    assert len(vals.get("manual_cases") or []) == 1

    # 第二条：编号接续（max+1，与既有用例数量无关）
    r2 = client.post(f"/api/sessions/{tid}/cases", json={**_CASE, "title": "编辑待办保存后统计数实时更新",
                                                         "normalize": False})
    id2 = r2.json()["case"]["case_id"]
    assert int(id2.split("-")[1]) == int(id1.split("-")[1]) + 1

    # 删除第一条（人工可删）；AI 用例不可删；未知 id 404
    ai_id = next(c["case_id"] for c in r2.json()["report"]["test_cases"]
                 if c["case_id"] not in (id1, id2))
    assert client.delete(f"/api/sessions/{tid}/cases/{ai_id}").status_code == 400
    assert client.delete(f"/api/sessions/{tid}/cases/TC-999").status_code == 404
    rd = client.delete(f"/api/sessions/{tid}/cases/{id1}")
    assert rd.status_code == 200
    ids_after = [c["case_id"] for c in rd.json()["report"]["test_cases"]]
    assert id1 not in ids_after and id2 in ids_after
    vals = graph.get_state(config).values or {}
    assert [c["case_id"] for c in vals.get("manual_cases") or []] == [id2]

    # 采纳剩余用例（含人工）随终审通过 → adopted_cases 含人工用例，流程到 done
    all_ids = [c["case_id"] for c in vals["test_report"]["test_cases"] if c.get("case_id")]
    ra = client.post(f"/api/sessions/{tid}/review/adopt", json={"case_ids": all_ids})
    assert ra.status_code == 200, ra.text
    _wait_idle(client, tid)
    vals = graph.get_state(config).values or {}
    assert vals.get("current_stage") == "done"
    assert id2 in (vals.get("adopted_cases") or [])


def test_reject_regen_reappends_manual_cases(client):
    """驳回重做：AI 重新出用例全局重编后，人工用例自动追加到新用例末尾。"""
    tid = _create_session(client)
    graph, config = _park_at_review(client, tid)

    r = client.post(f"/api/sessions/{tid}/cases", json=_CASE)
    manual_title = r.json()["case"]["title"]

    # 驳回（仅需求模式 → 回测试设计重出用例）→ 再次到达终审门禁
    rr = client.post(f"/api/sessions/{tid}/gates",
                     json={"decision": "reject", "comment": "边界场景不足"})
    assert rr.status_code == 200, rr.text
    _wait_idle(client, tid)  # 服务端 run 结束后再直接驱动，避免并发写 checkpoint
    _drive_until(graph, config, "review")

    vals = graph.get_state(config).values or {}
    cases = vals["test_report"]["test_cases"]
    manual = [c for c in cases if c.get("origin") == "manual"]
    assert len(manual) == 1, f"人工用例应恰好被重追加一次: {[c.get('title') for c in cases]}"
    assert manual[0]["title"] == manual_title
    # 编号接在全部新用例之后
    ai_nums = [int(c["case_id"].split("-")[1]) for c in cases if c.get("origin") != "manual"]
    assert int(manual[0]["case_id"].split("-")[1]) == max(ai_nums) + 1
    # 镜像同步保留（下次重做仍可恢复）
    assert len(vals.get("manual_cases") or []) == 1

    # 收尾：通过终审，不留挂起会话
    all_ids = [c["case_id"] for c in cases if c.get("case_id")]
    assert client.post(f"/api/sessions/{tid}/review/adopt", json={"case_ids": all_ids}).status_code == 200


def test_export_md_contains_origin(client):
    """导出 MD 的用例表带「来源」列并标记人工用例。"""
    tid = _create_session(client)
    _park_at_review(client, tid)
    r = client.post(f"/api/sessions/{tid}/cases", json={**_CASE, "normalize": False})
    manual_id = r.json()["case"]["case_id"]
    resp = client.get(f"/api/sessions/{tid}/export?format=md")
    assert resp.status_code == 200
    text = resp.text
    assert "来源" in text and "人工" in text
    assert manual_id in text
