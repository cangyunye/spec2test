"""采纳用例库存储层测试：登记幂等 / 过滤列举 / 删除 / CSV·JSON 导入。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from devflow import case_library as cl


def _case(cid: str, title: str = "全额退款成功", **kw) -> dict:
    base = {
        "case_id": cid, "tier": "functional", "priority": "P0", "case_type": "正向",
        "title": title, "target": "refund", "precondition": "已支付订单",
        "steps": "发起退款", "expected": "已退款", "rationale": "主流程",
    }
    base.update(kw)
    return base


def test_register_filters_by_adopted_ids_and_stamps_provenance(tmp_path):
    root = tmp_path / "lib"
    reg = cl.register_cases(
        "w-aaa", [_case("TC-001"), _case("TC-002")], ["TC-001"], root=root
    )
    assert reg["registered"] == 1
    assert reg["missing"] == []
    recs = cl.load_thread("w-aaa", root=root)
    assert [r["case_id"] for r in recs] == ["TC-001"]
    assert recs[0]["thread_id"] == "w-aaa"
    assert recs[0]["adopted_at"]  # 登记时间已补（checkpoint 无时间戳）

    # 未匹配的编号如实回报，且不制造空文件
    reg2 = cl.register_cases("w-bbb", [_case("TC-001")], ["TC-009"], root=root)
    assert reg2 == {"registered": 0, "total": 0, "missing": ["TC-009"]}
    assert not (root / "w-bbb.jsonl").exists()


def test_register_is_idempotent_and_overwrites(tmp_path):
    root = tmp_path / "lib"
    cl.register_cases("w-aaa", [_case("TC-001", title="旧标题")], ["TC-001"], root=root,
                      adopted_at="2026-01-01T00:00:00+08:00")
    cl.register_cases("w-aaa", [_case("TC-001", title="新标题"), _case("TC-002")],
                      ["TC-001", "TC-002"], root=root)
    recs = {r["case_id"]: r for r in cl.load_thread("w-aaa", root=root)}
    assert len(recs) == 2
    assert recs["TC-001"]["title"] == "新标题"  # 重登记覆盖旧记录


def test_list_cases_filters(tmp_path):
    root = tmp_path / "lib"
    cl.register_cases("w-a", [_case("TC-001"), _case("TC-002", title="登录鉴权", target="auth")],
                      ["TC-001", "TC-002"], root=root)
    cl.register_cases("w-b", [_case("TC-001", title="B 会话用例")], ["TC-001"], root=root)
    assert len(cl.list_cases(root=root)) == 3
    only_a = cl.list_cases(root=root, thread="w-a")
    assert {r["thread_id"] for r in only_a} == {"w-a"}
    hit = cl.list_cases(root=root, keyword="鉴权")
    assert [r["case_id"] for r in hit] == ["TC-002"]


def test_remove_cases_single_and_whole_thread(tmp_path):
    root = tmp_path / "lib"
    cl.register_cases("w-a", [_case("TC-001"), _case("TC-002"), _case("TC-003")],
                      ["TC-001", "TC-002", "TC-003"], root=root)
    cl.register_cases("w-b", [_case("TC-001", title="B")], ["TC-001"], root=root)

    out = cl.remove_cases(thread="w-a", case_ids=["TC-002"], root=root)
    assert out["removed"] == 1 and out["threads_removed"] == []
    assert [r["case_id"] for r in cl.load_thread("w-a", root=root)] == ["TC-001", "TC-003"]

    # thread 收窄：同编号只删指定来源的
    out = cl.remove_cases(thread="w-a", case_ids=["TC-001"], root=root)
    assert out["removed"] == 1
    assert len(cl.list_cases(root=root, thread="w-b")) == 1

    # 删空整线程：文件一并移除；无收窄整线程注销
    out = cl.remove_cases(thread="w-a", case_ids=["TC-003"], root=root)
    assert out["threads_removed"] == ["w-a"]
    out = cl.remove_cases(thread="w-b", root=root)
    assert out["removed"] == 1 and out["threads_removed"] == ["w-b"]
    assert cl.list_cases(root=root) == []

    with pytest.raises(ValueError):
        cl.remove_cases(root=root)  # 无参删除必须拒绝


def test_thread_slug_defends_path_escape(tmp_path):
    root = tmp_path / "lib"
    cl.register_cases("../../evil", [_case("TC-001")], ["TC-001"], root=root)
    files = list(root.glob("*"))
    assert len(files) == 1
    assert files[0].parent == root  # 未逃出库根
    assert cl.load_thread("../../evil", root=root)[0]["case_id"] == "TC-001"


def _csv_text() -> str:
    header = "标识,层级,优先级,类型,标题,所属模块,前置条件,步骤,预期结果,数据要求,设计依据,来源"
    rows = [
        'TC-001,功能,P0,正向,全额退款成功,refund,已支付订单,发起退款,已退款,,主流程,AI',
        'TC-002,安全,P1,反向,越权退款被拒,refund,已登录,伪造他人订单,拒绝,,资损,人工',
        ',功能,P2,正向,无编号自动补,refund,,,,,,,AI',
    ]
    return "\ufeff" + header + "\r\n" + "\r\n".join(rows) + "\r\n"


def test_import_csv_roundtrip_casecraft_export(tmp_path):
    root = tmp_path / "lib"
    csv_path = tmp_path / "casecraft-tests-w1.csv"
    csv_path.write_text(_csv_text(), encoding="utf-8")

    out = cl.import_cases_file(csv_path, root=root)
    assert out["registered"] == 3
    assert out["thread_id"] == "imported-casecraft-tests-w1"
    recs = {r["case_id"]: r for r in cl.load_thread(out["thread_id"], root=root)}
    assert set(recs) == {"TC-001", "TC-002", "IMP-003"}
    assert recs["TC-001"]["tier"] == "functional"      # 层级中文名 → tier key
    assert recs["TC-001"]["origin"] == "ai"
    assert recs["TC-002"]["tier"] == "security"
    assert recs["TC-002"]["origin"] == "manual"        # 「人工」→ manual
    assert recs["IMP-003"]["case_id"] == "IMP-003"     # 缺编号自动补
    assert recs["TC-001"]["expected"] == "已退款"


def test_import_json_list_and_test_report_shape(tmp_path):
    root = tmp_path / "lib"
    p1 = tmp_path / "cases.json"
    p1.write_text(json.dumps([_case("TC-001"), {"title": "无编号", "priority": "P1"}],
                             ensure_ascii=False), encoding="utf-8")
    out = cl.import_cases_file(p1, thread_label="手工整理", root=root)
    assert out == {"thread_id": "手工整理", "registered": 2, "total_in_file": 2}

    p2 = tmp_path / "report.json"
    p2.write_text(json.dumps({"test_cases": [_case("TC-009")], "run": {}}, ensure_ascii=False),
                  encoding="utf-8")
    out = cl.import_cases_file(p2, root=root)
    assert out["registered"] == 1
    assert cl.load_thread("imported-report", root=root)[0]["case_id"] == "TC-009"

    bad = tmp_path / "bad.txt"
    bad.write_text("not cases", encoding="utf-8")
    with pytest.raises(ValueError):
        cl.import_cases_file(bad, root=root)


def test_iter_threads_orders_by_recent_and_stats(tmp_path):
    root = tmp_path / "lib"
    cl.register_cases("w-old", [_case("TC-001")], ["TC-001"], root=root,
                      adopted_at="2026-01-01T00:00:00+08:00")
    cl.register_cases("w-new", [_case("TC-001")], ["TC-001"], root=root,
                      adopted_at="2026-09-21T00:00:00+08:00")
    threads = cl.iter_threads(root=root)
    assert [t["thread_id"] for t in threads] == ["w-new", "w-old"]
    s = cl.stats(root=root)
    assert s["threads"] == 2 and s["cases"] == 2
