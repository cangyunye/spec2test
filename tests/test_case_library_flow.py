"""采纳用例库端到端契约：采纳即登记 → 跨会话管理/删除 → 独立沉淀为清单。

覆盖链路：
  1. Web /review/adopt 采纳 → 用例库落盘（含 adopted_at 溯源）
  2. /api/case-library 列表 / 单条删除 / 整来源注销 / CSV·JSON 导入
  3. /api/library/distill + /api/library/commit 无会话独立沉淀
  4. CLI _parse_case_selection（approve <采纳清单>）+ checklist distill / rm / cases rm
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

from devflow import case_library as cl
from devflow.checklist.models import DistillOutput
from devflow.cli import _parse_case_selection
from devflow.orchestrator import build_graph_with_providers, initial_state

_CASES = [
    {"case_id": "TC-001", "tier": "functional", "priority": "P0", "case_type": "正向",
     "title": "全额退款成功", "target": "refund", "precondition": "已支付订单",
     "steps": "发起退款", "expected": "已退款", "rationale": "主流程"},
    {"case_id": "TC-002", "tier": "functional", "priority": "P1", "case_type": "反向",
     "title": "超额退款被拒", "target": "refund", "precondition": "已支付订单",
     "steps": "超额退款", "expected": "拒绝", "rationale": "资损防护"},
]


# ═══════════════════════════════════════════════════════════════════
# 1. Web 采纳 → 登记（复用 test_review_adopt 的会话构造）
# ═══════════════════════════════════════════════════════════════════


def _mk_session() -> str:
    graph = build_graph_with_providers()
    tid = f"w-{uuid.uuid4().hex[:8]}"
    state = initial_state()
    state["requirement"]["project_context"] = "订单退款系统"
    state["test_report"] = {"session_id": "s-1", "test_cases": _CASES, "run": {}}
    graph.update_state({"configurable": {"thread_id": tid}}, state)
    return tid


def _park_at_review(tid: str) -> None:
    graph = build_graph_with_providers()
    config = {"configurable": {"thread_id": tid}}
    graph.update_state(config, {"current_stage": "test"}, as_node="test_run")
    list(graph.stream(None, config, stream_mode="updates"))


async def _adopt_and_settle(tid: str, case_ids: list[str]) -> dict:
    from fastapi import HTTPException

    from web.server import AdoptBody, _bus, adopt_review

    try:
        resp = await adopt_review(tid, AdoptBody(case_ids=case_ids))
    except HTTPException as e:
        return {"http_error": e.status_code, "detail": e.detail}
    bus = _bus(tid)
    for _ in range(300):
        if bus.task and bus.task.done():
            break
        await asyncio.sleep(0.05)
    return resp


def test_adopt_registers_into_case_library(monkeypatch, tmp_path):
    monkeypatch.setenv("DEVFLOW_CASE_LIBRARY_ROOT", str(tmp_path / "cl"))
    tid = _mk_session()
    _park_at_review(tid)

    resp = asyncio.run(_adopt_and_settle(tid, ["TC-001"]))
    assert resp.get("accepted") is True
    assert resp["case_library_registered"] == 1

    recs = cl.load_thread(tid)
    assert [r["case_id"] for r in recs] == ["TC-001"]
    assert recs[0]["thread_id"] == tid
    assert recs[0]["adopted_at"]
    assert recs[0]["title"] == "全额退款成功"  # 完整结构化字段随登记持久化


def test_adopt_reject_keeps_library_untouched(monkeypatch, tmp_path):
    """未采纳（无 interrupt → 409）不登记：登记只跟采纳动作绑定。"""
    monkeypatch.setenv("DEVFLOW_CASE_LIBRARY_ROOT", str(tmp_path / "cl"))
    tid = _mk_session()
    resp = asyncio.run(_adopt_and_settle(tid, ["TC-001"]))
    assert resp["http_error"] == 409
    assert cl.list_cases() == []


# ═══════════════════════════════════════════════════════════════════
# 2. /api/case-library 管理端点
# ═══════════════════════════════════════════════════════════════════


@pytest.fixture()
def client(monkeypatch, tmp_path) -> Iterator[TestClient]:
    monkeypatch.setenv("DEVFLOW_CASE_LIBRARY_ROOT", str(tmp_path / "cl"))
    from web import server as ws

    ws._buses.clear()
    ws._tid_locks.clear()
    with TestClient(app := ws.app) as c:  # with 触发 startup（tmp runs 目录为空，无续跑）
        yield c
    ws._buses.clear()
    ws._tid_locks.clear()


_CSV = (
    "\ufeff标识,层级,优先级,类型,标题,所属模块,前置条件,步骤,预期结果,数据要求,设计依据,来源\r\n"
    "TC-001,功能,P0,正向,全额退款成功,refund,已支付订单,发起退款,已退款,,主流程,AI\r\n"
    "TC-002,功能,P1,反向,超额退款被拒,refund,已支付订单,超额退款,拒绝,,资损,人工\r\n"
)


def test_case_library_endpoints_roundtrip(client, tmp_path):
    csv_path = tmp_path / "casecraft-tests-w1.csv"
    csv_path.write_text(_CSV, encoding="utf-8")

    # 导入 → 列表可见
    with open(csv_path, "rb") as fh:
        r = client.post("/api/case-library/import",
                        files={"file": ("casecraft-tests-w1.csv", fh, "text/csv")})
    assert r.status_code == 200, r.text
    assert r.json()["registered"] == 2

    data = client.get("/api/case-library").json()
    assert data["threads"][0]["count"] == 2
    assert [c["case_id"] for c in data["cases"]] == ["TC-001", "TC-002"]

    # 关键词过滤
    hit = client.get("/api/case-library", params={"keyword": "超额"}).json()
    assert [c["case_id"] for c in hit["cases"]] == ["TC-002"]

    # 单条删除 → 404 再删
    tid = data["threads"][0]["thread_id"]
    assert client.delete(f"/api/case-library/cases/{tid}/TC-001").status_code == 200
    assert client.delete(f"/api/case-library/cases/{tid}/TC-001").status_code == 404

    # 整来源注销 → 库空
    assert client.delete(f"/api/case-library/threads/{tid}").status_code == 200
    assert client.get("/api/case-library").json()["cases"] == []
    assert client.delete(f"/api/case-library/threads/{tid}").status_code == 404


def test_case_library_import_rejects_bad_input(client, tmp_path):
    r = client.post("/api/case-library/import",
                    files={"file": ("x.md", b"not cases", "text/markdown")})
    assert r.status_code == 415
    r = client.post("/api/case-library/import",
                    files={"file": ("bad.json", b"{oops", "application/json")})
    assert r.status_code == 400


def test_case_library_import_json_report_shape(client):
    payload = json.dumps({"test_cases": [
        {"case_id": "TC-009", "priority": "P1", "title": "JSON 用例",
         "steps": "s", "expected": "e"},
    ]}, ensure_ascii=False).encode("utf-8")
    r = client.post("/api/case-library/import",
                    files={"file": ("report.json", payload, "application/json")})
    assert r.status_code == 200, r.text
    assert r.json()["thread_id"] == "imported-report"
    ids = [c["case_id"] for c in client.get("/api/case-library").json()["cases"]]
    assert ids == ["TC-009"]


# ═══════════════════════════════════════════════════════════════════
# 3. 无会话独立沉淀：/api/library/distill + /api/library/commit
# ═══════════════════════════════════════════════════════════════════


def _fake_distill(monkeypatch) -> None:
    async def fake(cases, business, existing_scenario, existing_checklist):
        return DistillOutput.model_validate({
            "scenario": {
                "name": business.get("name") or "支付业务",
                "description": business.get("description") or "需求涉及支付时路由到此",
                "keywords": ["支付", "退款"], "usage": "支付退款流程测试",
            },
            "sections": [
                {"category": "正向", "items": [{"priority": "P0", "text": "全额退款成功且状态流转"}]},
                {"category": "反向", "items": [{"priority": "P0", "text": "超额退款被拒绝"}]},
            ],
            "merge_notes": "",
        })

    monkeypatch.setattr("devflow.checklist.distill.distill_from_cases", fake)


def test_library_distill_and_commit_without_session(monkeypatch, tmp_path):
    monkeypatch.setenv("DEVFLOW_CASE_LIBRARY_ROOT", str(tmp_path / "cl"))
    monkeypatch.setenv("DEVFLOW_CHECKLIST_ROOT", str(tmp_path / "lib"))
    _fake_distill(monkeypatch)
    cl.register_cases("w-src", _CASES, ["TC-001", "TC-002"])

    from web.server import LibraryCommitBody, LibraryDistillBody, library_commit, library_distill

    prev = asyncio.run(library_distill(LibraryDistillBody(
        business={"rel_dir": "payment", "name": "支付业务"},
        selections=[{"thread_id": "w-src", "case_id": "TC-001"}],
    )))
    assert prev["mode"] == "create"
    assert prev["case_count"] == 1  # 勾选子集生效
    assert "## 正向" in prev["checklist_md"]

    out = library_commit(LibraryCommitBody(
        rel_dir=prev["rel_dir"], scenario_md=prev["scenario_md"], checklist_md=prev["checklist_md"],
    ))
    assert (tmp_path / "lib" / "payment" / "checklist.md").is_file()
    assert Path(out["written"][0]).is_file()


def test_library_distill_validates_selection(monkeypatch, tmp_path):
    monkeypatch.setenv("DEVFLOW_CASE_LIBRARY_ROOT", str(tmp_path / "cl"))
    monkeypatch.setenv("DEVFLOW_CHECKLIST_ROOT", str(tmp_path / "lib"))
    _fake_distill(monkeypatch)
    cl.register_cases("w-src", _CASES, ["TC-001"])

    from fastapi import HTTPException

    from web.server import LibraryDistillBody, library_distill

    with pytest.raises(HTTPException) as ei:
        asyncio.run(library_distill(LibraryDistillBody(
            business={"rel_dir": "payment"}, selections=[])))
    assert ei.value.status_code == 400

    with pytest.raises(HTTPException) as ei:
        asyncio.run(library_distill(LibraryDistillBody(
            business={"rel_dir": "payment"},
            selections=[{"thread_id": "w-src", "case_id": "TC-404"}])))
    assert ei.value.status_code == 400

    with pytest.raises(HTTPException) as ei:
        asyncio.run(library_distill(LibraryDistillBody(
            business={"rel_dir": "../escape"},
            selections=[{"thread_id": "w-src", "case_id": "TC-001"}])))
    assert ei.value.status_code == 400


# ═══════════════════════════════════════════════════════════════════
# 4. CLI：approve 采纳清单解析 + checklist distill/rm + cases rm
# ═══════════════════════════════════════════════════════════════════


class TestParseCaseSelection:
    CS = _CASES

    def test_all(self):
        assert _parse_case_selection("all", self.CS) == ["TC-001", "TC-002"]

    def test_indices_and_ranges(self):
        assert _parse_case_selection("1", self.CS) == ["TC-001"]
        assert _parse_case_selection("1-2", self.CS) == ["TC-001", "TC-002"]
        assert _parse_case_selection("2, 1", self.CS) == ["TC-002", "TC-001"]  # 保序去重

    def test_case_ids_case_insensitive(self):
        assert _parse_case_selection("tc-002", self.CS) == ["TC-002"]

    def test_unknown_token_returns_none(self):
        assert _parse_case_selection("9", self.CS) is None
        assert _parse_case_selection("TC-009", self.CS) is None
        assert _parse_case_selection("1-", self.CS) is None
        assert _parse_case_selection("all", []) is None

    def test_dedup_keep_order(self):
        assert _parse_case_selection("1,1,2", self.CS) == ["TC-001", "TC-002"]


def test_cli_checklist_distill_from_library(monkeypatch, tmp_path):
    monkeypatch.setenv("DEVFLOW_CASE_LIBRARY_ROOT", str(tmp_path / "cl"))
    monkeypatch.setenv("DEVFLOW_CHECKLIST_ROOT", str(tmp_path / "lib"))
    _fake_distill(monkeypatch)
    cl.register_cases("w-src", _CASES, ["TC-001", "TC-002"])

    from devflow.cli import checklist_distill

    checklist_distill(
        business="payment", name="支付业务", description="", ids="TC-001",
        thread="", keyword="", root=None, project_root="", yes=True,
    )
    md = (tmp_path / "lib" / "payment" / "checklist.md").read_text(encoding="utf-8")
    assert "sources" in md and "cli:cases" in md  # 溯源标签落 frontmatter

    # merge：第二次沉淀同业务，sources 累积且库仍可读
    checklist_distill(
        business="payment", ids="TC-002", thread="w-src", keyword="",
        root=None, project_root="", name="", description="", yes=True,
    )
    from devflow.checklist.library import checklist_tree

    tree = checklist_tree(tmp_path / "lib")
    assert tree[0]["rel_dir"] == "payment"


def test_cli_checklist_rm_and_cases_rm(monkeypatch, tmp_path):
    monkeypatch.setenv("DEVFLOW_CASE_LIBRARY_ROOT", str(tmp_path / "cl"))
    monkeypatch.setenv("DEVFLOW_CHECKLIST_ROOT", str(tmp_path / "lib"))
    from devflow.cli import cases_rm, checklist_rm

    cl.register_cases("w-a", _CASES, ["TC-001", "TC-002"])
    lib_file = tmp_path / "lib" / "payment" / "checklist.md"
    lib_file.parent.mkdir(parents=True)
    lib_file.write_text("---\nname: x\n---\n", encoding="utf-8")

    cases_rm(case_ids=["TC-001"], thread="w-a", yes=True)
    assert [c["case_id"] for c in cl.load_thread("w-a")] == ["TC-002"]

    cases_rm(case_ids=[], thread="w-a", yes=True)  # 整来源注销
    assert cl.load_thread("w-a") == []

    checklist_rm(business="payment", root=None, yes=True)
    assert not lib_file.exists()

    import typer

    with pytest.raises(typer.Exit):
        checklist_rm(business="../escape", root=None, yes=True)  # 非法路径拒绝
