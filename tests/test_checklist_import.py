"""清单导入/手写入库测试：distill_from_doc 归纳 + import/manual 端点 + 溯源累积。"""
from __future__ import annotations

import asyncio
import io
import uuid

import pytest
from starlette.datastructures import UploadFile

from devflow.checklist.distill import merge_sources
from devflow.checklist.models import DistillOutput
from devflow.orchestrator import build_graph_with_providers, initial_state


def _mk_session(tmp_root: str) -> str:
    graph = build_graph_with_providers()
    tid = f"w-{uuid.uuid4().hex[:8]}"
    state = initial_state()
    state["requirement"]["project_context"] = "订单支付系统"
    state["requirement"]["project_root"] = tmp_root
    graph.update_state({"configurable": {"thread_id": tid}}, state)
    return tid


def _fake_doc_distill(monkeypatch):
    seen: dict = {}

    async def fake(doc_text, business, existing_scenario, existing_checklist):
        seen["doc_len"] = len(doc_text)
        seen["business"] = business
        seen["merge"] = bool(existing_checklist)
        return DistillOutput.model_validate({
            "scenario": {
                "name": business.get("name") or "支付业务",
                "description": business.get("description") or "需求涉及支付时路由到此",
                "keywords": ["支付"],
                "usage": "支付流程测试",
            },
            "sections": [
                {"category": "正向", "items": [{"priority": "P0", "text": "支付成功状态流转"}]},
            ],
            "merge_notes": "合并说明" if existing_checklist else "",
        })

    monkeypatch.setattr("devflow.checklist.distill.distill_from_doc", fake)
    return seen


def _upload(name: str, text: str) -> UploadFile:
    return UploadFile(file=io.BytesIO(text.encode("utf-8")), filename=name)


def test_import_doc_create_then_commit(monkeypatch, tmp_path):
    """上传文档 → create 预览 → 落盘后 frontmatter 带 import: 溯源。"""
    monkeypatch.setenv("DEVFLOW_CHECKLIST_ROOT", str(tmp_path / "lib"))
    seen = _fake_doc_distill(monkeypatch)
    tid = _mk_session(str(tmp_path / "proj"))

    from web.server import DistillCommit, commit_checklist, import_checklist

    prev = asyncio.run(import_checklist(
        tid,
        file=_upload("qa-checklist.md", "## 支付检查点\n- 支付超时订单自动关闭"),
        rel_dir="payment", name="支付业务", description="需求涉及支付时路由到此",
    ))
    assert prev["mode"] == "create"
    assert prev["case_count"] == 0  # 文档导入不产用例计数
    assert seen["merge"] is False
    assert seen["doc_len"] > 0
    assert seen["business"]["rel_dir"] == "payment"
    assert "sources:" in prev["checklist_md"]
    assert "import:qa-checklist.md" in prev["checklist_md"]

    commit_checklist(tid, DistillCommit(
        rel_dir=prev["rel_dir"], scenario_md=prev["scenario_md"], checklist_md=prev["checklist_md"],
    ))
    lib = tmp_path / "lib"
    assert (lib / "payment" / "checklist.md").is_file()


def test_import_merge_preserves_old_sources(monkeypatch, tmp_path):
    """merge 模式：既有 sources 与本次 import: 来源累积，不丢旧溯源。"""
    monkeypatch.setenv("DEVFLOW_CHECKLIST_ROOT", str(tmp_path / "lib"))
    _fake_doc_distill(monkeypatch)
    tid = _mk_session(str(tmp_path / "proj"))

    from devflow.checklist.library import resolve_root, write_checklist
    from web.server import DistillCommit, commit_checklist, import_checklist

    # 预置已有清单（带旧溯源）
    root = resolve_root("")
    write_checklist(root, "payment", "---\nname: 支付业务\n---\n旧",
                    "---\nname: 支付业务\nbusiness: payment\nsources: [sess-old]\n---\n\n## 正向\n- [P0] 旧条目\n")

    prev = asyncio.run(import_checklist(
        tid, file=_upload("wiki.md", "支付规则若干"), rel_dir="payment", name="", description="",
    ))
    assert prev["mode"] == "merge"
    commit_checklist(tid, DistillCommit(
        rel_dir=prev["rel_dir"], scenario_md=prev["scenario_md"], checklist_md=prev["checklist_md"],
    ))

    from devflow.checklist.distill import parse_checklist_meta

    meta = parse_checklist_meta((root / "payment" / "checklist.md").read_text(encoding="utf-8"))
    assert "sess-old" in meta["sources"]
    assert "import:wiki.md" in meta["sources"]


def test_import_rejects_bad_rel_dir_and_format(monkeypatch, tmp_path):
    monkeypatch.setenv("DEVFLOW_CHECKLIST_ROOT", str(tmp_path / "lib"))
    _fake_doc_distill(monkeypatch)
    tid = _mk_session(str(tmp_path / "proj"))

    from fastapi import HTTPException

    from web.server import import_checklist

    with pytest.raises(HTTPException) as ei:
        asyncio.run(import_checklist(
            tid, file=_upload("a.md", "x"), rel_dir="../escape", name="", description=""))
    assert ei.value.status_code == 400

    with pytest.raises(HTTPException) as ei:
        asyncio.run(import_checklist(
            tid, file=_upload("a.xlsx", "x"), rel_dir="payment", name="", description=""))
    assert ei.value.status_code == 415


def test_distill_manual_mode(monkeypatch, tmp_path):
    """手写清单走 source=manual：规范化预览，空内容 400。"""
    monkeypatch.setenv("DEVFLOW_CHECKLIST_ROOT", str(tmp_path / "lib"))
    seen = _fake_doc_distill(monkeypatch)
    tid = _mk_session(str(tmp_path / "proj"))

    from fastapi import HTTPException

    from web.server import DistillRequest, distill_checklist

    prev = asyncio.run(distill_checklist(tid, DistillRequest(
        source="manual", text="## 正向\n- [P0] 退款 3 个工作日到账", business={"rel_dir": "payment/refund"},
    )))
    assert prev["mode"] == "create"
    assert seen["doc_len"] > 0  # 手写原文走同一 doc 归纳通道
    assert "## 正向" in prev["checklist_md"]

    with pytest.raises(HTTPException) as ei:
        asyncio.run(distill_checklist(tid, DistillRequest(source="manual", text="  ")))
    assert ei.value.status_code == 400


def test_merge_sources_accumulates():
    """溯源合并：保序去重，新增追加。"""
    existing = "---\nname: x\nsources: [s1, s2]\n---\nbody"
    assert merge_sources(existing, "s3") == ["s1", "s2", "s3"]
    assert merge_sources(existing, "s1") == ["s1", "s2"]  # 去重
    assert merge_sources("", "import:a.md") == ["import:a.md"]
    assert merge_sources(existing, "") == ["s1", "s2"]  # 空来源不追加
