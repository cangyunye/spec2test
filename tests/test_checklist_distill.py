"""沉淀流程 API 测试：distill 预览（LLM monkeypatch）→ commit 落盘 → 树可见。"""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest

from devflow.checklist.models import DistillOutput
from devflow.orchestrator import build_graph_with_providers, initial_state


def _mk_session(tmp_root: str) -> str:
    """造一个带 test_report 的会话（checkpoint 直写，不跑节点）。"""
    graph = build_graph_with_providers()
    tid = f"w-{uuid.uuid4().hex[:8]}"
    state = initial_state()
    state["requirement"]["project_context"] = "订单支付系统"
    state["requirement"]["project_root"] = tmp_root
    state["test_report"] = {
        "session_id": "s-1",
        "target_symbols": ["refund"],
        "run": {"passed": 2, "failed": 0, "skipped": 0, "coverage_pct": None, "logs": ""},
        "test_cases": [
            {
                "case_id": "TC-001", "tier": "functional", "priority": "P0",
                "case_type": "正向", "title": "全额退款成功",
                "target": "refund", "precondition": "已支付订单",
                "steps": "发起退款 → 确认", "expected": "状态为已退款",
                "rationale": "主流程",
            },
            {
                "case_id": "TC-002", "tier": "functional", "priority": "P1",
                "case_type": "反向", "title": "超额退款被拒绝",
                "target": "refund", "precondition": "已支付订单",
                "steps": "退款金额超过实付", "expected": "拒绝并提示",
                "rationale": "资损防护",
            },
        ],
    }
    graph.update_state({"configurable": {"thread_id": tid}}, state)
    return tid


def _fake_distill(monkeypatch, tmp_path: Path):
    """替换 LLM 归纳为确定性输出，并校验用例勾选确实生效。"""
    seen: dict = {}

    async def fake(cases, business, existing_scenario, existing_checklist):
        seen["case_ids"] = [c.get("case_id") for c in cases]
        seen["business"] = business
        seen["merge"] = bool(existing_checklist)
        return DistillOutput.model_validate({
            "scenario": {
                "name": business.get("name") or "支付业务",
                "description": business.get("description") or "需求涉及支付时路由到此",
                "keywords": ["支付", "退款"],
                "usage": "支付退款流程测试",
            },
            "sections": [
                {"category": "正向", "items": [{"priority": "P0", "text": "全额退款成功且状态流转"}]},
                {"category": "反向", "items": [{"priority": "P0", "text": "超额退款被拒绝"}]},
            ],
            "merge_notes": "合并 2 条",
        })

    monkeypatch.setattr("devflow.checklist.distill.distill_from_cases", fake)
    return seen


def test_distill_preview_create_then_commit(monkeypatch, tmp_path):
    monkeypatch.setenv("DEVFLOW_CHECKLIST_ROOT", str(tmp_path / "lib"))
    seen = _fake_distill(monkeypatch, tmp_path)
    tid = _mk_session(str(tmp_path / "proj"))

    from web.server import DistillCommit, DistillRequest, commit_checklist, distill_checklist

    prev = asyncio.run(distill_checklist(
        tid, DistillRequest(business={"rel_dir": "payment", "name": "支付业务"}, case_ids=["TC-001"])
    ))
    assert prev["mode"] == "create"
    assert prev["rel_dir"] == "payment"
    assert prev["case_count"] == 1  # 只勾选了 TC-001
    assert seen["case_ids"] == ["TC-001"]
    assert "## 正向" in prev["checklist_md"]
    assert prev["checklist_md"].startswith("---")  # 带溯源 frontmatter

    res = commit_checklist(tid, DistillCommit(
        rel_dir=prev["rel_dir"], scenario_md=prev["scenario_md"], checklist_md=prev["checklist_md"],
    ))
    lib = tmp_path / "lib"
    assert (lib / "payment" / "scenario.md").is_file()
    assert (lib / "payment" / "checklist.md").is_file()
    # 库树可见
    from devflow.checklist.library import checklist_tree
    tree = checklist_tree(lib)
    assert tree[0]["rel_dir"] == "payment"
    assert tree[0]["item_count"] == 2


def test_distill_merge_mode_reads_existing(monkeypatch, tmp_path):
    monkeypatch.setenv("DEVFLOW_CHECKLIST_ROOT", str(tmp_path / "lib"))
    from devflow.checklist.scaffold import init_library

    init_library(tmp_path / "lib", with_example=True)
    seen = _fake_distill(monkeypatch, tmp_path)
    tid = _mk_session(str(tmp_path / "proj"))

    from web.server import DistillRequest, distill_checklist

    prev = asyncio.run(distill_checklist(
        tid, DistillRequest(business={"rel_dir": "payment/refund"}, case_ids=[])
    ))
    assert prev["mode"] == "merge"       # 已有 checklist.md
    assert seen["merge"] is True         # 旧清单原文已交给 LLM
    assert seen["case_ids"] == ["TC-001", "TC-002"]  # 空勾选 = 全部


def test_distill_rejects_bad_rel_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("DEVFLOW_CHECKLIST_ROOT", str(tmp_path / "lib"))
    tid = _mk_session(str(tmp_path / "proj"))
    from fastapi import HTTPException

    from web.server import DistillRequest, distill_checklist

    with pytest.raises(HTTPException) as ei:
        asyncio.run(distill_checklist(tid, DistillRequest(business={"rel_dir": "../escape"})))
    assert ei.value.status_code == 400
