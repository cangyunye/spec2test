"""Checklist 库单元测试：frontmatter 解析 / 扫描路由 / 加载写盘 / 渲染 / 脚手架。

LLM 相关路径（路由匹配、沉淀归纳）在单测里走 Mock 兜底（conftest 强制），
只验证「失败降级不抛异常」与纯函数渲染逻辑；真实链路由 Web/CLI 手工验收。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from devflow.checklist import models
from devflow.checklist.distill import (
    parse_checklist_meta,
    render_checklist_md,
    render_scenario_md,
)
from devflow.checklist.library import (
    checklist_tree,
    library_node,
    library_view,
    load_checklists,
    load_scenario,
    parse_checklist_sections,
    parse_frontmatter,
    resolve_root,
    scan_business_types,
    scan_sub_businesses,
    validate_rel_dir,
    write_checklist,
)
from devflow.checklist.models import (
    DistillOutput,
    RouteMatch,
    RouteMatchBusiness,
    RouteMatchSub,
)
from devflow.checklist.routing import match_businesses, summarize_requirement
from devflow.checklist.scaffold import init_library


# ═══════════════════════════════════════════════════════════════════
# frontmatter 解析
# ═══════════════════════════════════════════════════════════════════


def test_parse_frontmatter_valid():
    meta, body = parse_frontmatter("---\nname: 支付\nkeywords: [a, b]\n---\n正文内容")
    assert meta["name"] == "支付"
    assert meta["keywords"] == ["a", "b"]
    assert body == "正文内容"


def test_parse_frontmatter_missing_and_malformed():
    assert parse_frontmatter("无 frontmatter") == ({}, "无 frontmatter")
    # 未闭合
    assert parse_frontmatter("---\nname: x\n") == ({}, "---\nname: x\n")
    # YAML 非法 → 容错返回原文
    meta, body = parse_frontmatter("---\n: : :\n---\nbody")
    assert meta == {}


def test_validate_rel_dir():
    assert validate_rel_dir("payment/refund") == "payment/refund"
    assert validate_rel_dir("/payment/") == "payment"
    assert validate_rel_dir("../etc") is None
    assert validate_rel_dir("_template") is None
    assert validate_rel_dir("") is None
    assert validate_rel_dir("中文名") is None
    assert validate_rel_dir("a/b/../../c") is None


# ═══════════════════════════════════════════════════════════════════
# 场景加载与扫描
# ═══════════════════════════════════════════════════════════════════


@pytest.fixture()
def lib(tmp_path) -> Path:
    """手工搭一个最小库：payment（含 refund 子业务）+ _template。"""
    write_checklist(
        tmp_path,
        "payment",
        "---\nname: 支付业务\ndescription: 资金流程\nkeywords: [支付]\nreferences:\n  - path: refund\n    desc: 退款\n---\n## 使用场景\n支付",
        "---\nname: 支付业务\nbusiness: payment\n---\n## 正向\n- [P0] 支付成功",
    )
    write_checklist(
        tmp_path,
        "payment/refund",
        "---\nname: 退款子业务\ndescription: 退款流程\nkeywords: [退款]\n---\n退款场景",
        "---\nname: 退款子业务\nbusiness: payment/refund\n---\n## 正向\n- [P0] 全额退款",
    )
    (tmp_path / "_template").mkdir()
    (tmp_path / "_template" / "scenario.md").write_text("---\nname: t\n---\n", encoding="utf-8")
    (tmp_path / "empty_dir").mkdir()  # 无 scenario.md → 不算业务
    return tmp_path


def test_load_scenario_full(lib):
    doc = load_scenario(lib, "payment")
    assert doc is not None
    assert doc.name == "支付业务"
    assert doc.keywords == ["支付"]
    assert doc.references[0].path == "refund"
    assert "使用场景" in doc.usage


def test_load_scenario_missing_fallbacks(lib):
    assert load_scenario(lib, "nope") is None
    assert load_scenario(lib, "../escape") is None
    # 目录存在但无 scenario.md
    assert load_scenario(lib, "empty_dir") is None


def test_scan_skips_template_and_empty(lib):
    tops = scan_business_types(lib)
    assert [d.rel_dir for d in tops] == ["payment"]


def test_scan_sub_businesses(lib):
    subs = scan_sub_businesses(lib, "payment")
    assert [d.rel_dir for d in subs] == ["payment/refund"]
    assert scan_sub_businesses(lib, "nope") == []


def test_resolve_root_project_level(monkeypatch, tmp_path):
    monkeypatch.delenv("DEVFLOW_CHECKLIST_ROOT", raising=False)
    proj = tmp_path / "proj"
    proj.mkdir()
    # 项目下无 .checklist → 回退全局（checkpoint 同级）
    fallback = resolve_root(str(proj))
    assert fallback.name == "checklist"
    # 项目下有 .checklist → 项目级优先
    (proj / ".checklist").mkdir()
    assert resolve_root(str(proj)) == proj / ".checklist"


def test_resolve_root_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("DEVFLOW_CHECKLIST_ROOT", str(tmp_path / "custom"))
    assert resolve_root("/any") == tmp_path / "custom"


# ═══════════════════════════════════════════════════════════════════
# 候选树（LLM 匹配结果 → 门禁数据；目录扫描是事实源）
# ═══════════════════════════════════════════════════════════════════


def test_build_candidates_validates_against_filesystem(lib):
    from devflow.checklist.library import build_candidates

    # 幻觉业务（fintech）被丢弃；子业务短名/全路径都能对上
    match = RouteMatch(
        businesses=[RouteMatchBusiness(name="payment"), RouteMatchBusiness(name="fintech")],
        sub_businesses=[RouteMatchSub(business="payment", name="refund")],
    )
    tree = build_candidates(lib, match)
    assert [c.rel_dir for c in tree] == ["payment"]
    assert tree[0].suggested is True
    assert [c.rel_dir for c in tree[0].children] == ["payment/refund"]
    assert tree[0].children[0].has_checklist is True


def test_build_candidates_reference_fallback(lib):
    from devflow.checklist.library import build_candidates

    # LLM 只选了业务没选子业务 → 回落到 scenario.md 的 references
    match = RouteMatch(businesses=[RouteMatchBusiness(name="payment")])
    tree = build_candidates(lib, match)
    assert [c.rel_dir for c in tree[0].children] == ["payment/refund"]


def test_match_businesses_empty_catalog_is_noop(lib, monkeypatch):
    # 空库：不调 LLM，直接空匹配（静默跳过的依据）
    empty = lib / "empty_lib"
    empty.mkdir()

    async def _boom(*a, **k):  # 若被调用则测试失败
        raise AssertionError("空库不应调用 LLM")

    monkeypatch.setattr("devflow.checklist.routing.invoke_json", _boom)
    import asyncio

    result = asyncio.run(match_businesses("需求文本", empty))
    assert result.businesses == []


# ═══════════════════════════════════════════════════════════════════
# 加载 / 树视图 / 沉淀渲染
# ═══════════════════════════════════════════════════════════════════


def test_load_checklists(lib):
    docs = load_checklists(lib, ["payment", "payment/refund", "empty_dir"])
    assert [d["rel_dir"] for d in docs] == ["payment", "payment/refund"]
    assert "支付成功" in docs[0]["content"]
    assert docs[1]["name"] == "退款子业务"


def test_checklist_tree(lib):
    tree = checklist_tree(lib)
    assert len(tree) == 1
    biz = tree[0]
    assert biz["rel_dir"] == "payment"
    assert biz["item_count"] == 1
    assert biz["children"][0]["rel_dir"] == "payment/refund"


# ═══════════════════════════════════════════════════════════════════
# 文档视图（独立浏览页 /library）
# ═══════════════════════════════════════════════════════════════════


def test_parse_checklist_sections():
    md = (
        "---\nname: x\nupdated: 2026-01-01\n---\n\n"
        "## 正向\n- [P0] 支付成功\n- [p1] 回调幂等\n\n"
        "## 反向\n- 非法输入被拒绝\n"
    )
    secs = parse_checklist_sections(md)
    assert [s["category"] for s in secs] == ["正向", "反向"]
    assert secs[0]["items"][0] == {"priority": "P0", "text": "支付成功"}
    assert secs[0]["items"][1] == {"priority": "P1", "text": "回调幂等"}
    # 无优先级前缀的条目容错保留，不丢内容
    assert secs[1]["items"][0] == {"priority": "", "text": "非法输入被拒绝"}


def test_parse_checklist_sections_empty_and_dangling():
    assert parse_checklist_sections("") == []
    # 分节前悬空的条目无归属，忽略（不虚构分节）
    assert parse_checklist_sections("说明文字\n- [P0] 悬空条目") == []


def test_library_view(lib):
    tree = library_view(lib)
    assert [n["rel_dir"] for n in tree] == ["payment"]
    biz = tree[0]
    assert biz["name"] == "支付业务"
    assert biz["keywords"] == ["支付"]
    assert biz["has_checklist"] is True
    assert biz["item_count"] == 1
    assert biz["sections"][0]["items"][0]["text"] == "支付成功"
    assert [c["rel_dir"] for c in biz["children"]] == ["payment/refund"]
    assert biz["children"][0]["item_count"] == 1


def test_library_view_missing_checklist(tmp_path):
    write_checklist(tmp_path, "plain", "---\nname: 无清单业务\n---\n正文", "")
    tree = library_view(tmp_path)
    assert len(tree) == 1
    assert tree[0]["has_checklist"] is False
    assert tree[0]["item_count"] == 0
    assert tree[0]["sections"] == []

def test_library_node_single(lib):
    node = library_node(lib, "payment")
    assert node["rel_dir"] == "payment"
    assert node["name"] == "支付业务"
    assert node["has_checklist"] is True
    assert node["sections"][0]["items"][0]["text"] == "支付成功"
    assert node["children"] == []  # 单节点视图不含子业务


def test_library_node_missing_and_invalid(lib):
    assert library_node(lib, "no/such") is None      # scenario.md 不存在
    assert library_node(lib, "_template") is None    # "_" 前缀模板目录非法
    assert library_node(lib, "../payment") is None   # 路径穿越拒绝
    assert library_node(lib, "") is None


def _distill_out() -> DistillOutput:
    return DistillOutput.model_validate(
        {
            "scenario": {
                "name": "退款子业务",
                "description": "需求涉及退款时路由到此",
                "keywords": ["退款", "refund"],
                "usage": "退款流程测试",
            },
            "sections": [
                {
                    "category": "正向",
                    "items": [{"priority": "P0", "text": "全额退款成功"}],
                },
                {
                    "category": "资金安全",  # 非法分节 → 子串归一化到「安全」
                    "items": [{"priority": "P0", "text": "退款不越权"}],
                },
                {
                    "category": "异常处理",  # 完全无法归类 → 兜底「场景法」
                    "items": [{"priority": "P1", "text": "退款接口超时重试"}],
                },
            ],
            "merge_notes": "新增 2 条",
        }
    )


def test_render_roundtrip(lib):
    out = _distill_out()
    scenario_md = render_scenario_md(out)
    checklist_md = render_checklist_md(out, business="payment/refund", sources=["w-test"])
    write_checklist(lib, "payment/dispute", scenario_md, checklist_md)

    doc = load_scenario(lib, "payment/dispute")
    assert doc.name == "退款子业务"
    meta = parse_checklist_meta(checklist_md)
    assert meta["business"] == "payment/refund"
    assert meta["sources"] == ["w-test"]
    # 非法分节被归一化到合法集合
    assert "## 安全" in checklist_md
    assert "## 场景法" in checklist_md
    assert "- [P0] 全额退款成功" in checklist_md


def test_normalize_category():
    assert models.normalize_category("反向") == "反向"
    assert models.normalize_category("性能类") == "性能"
    assert models.normalize_category("functional") == "正向"
    assert models.normalize_category("随便") == "场景法"


# ═══════════════════════════════════════════════════════════════════
# 脚手架 + 需求摘要
# ═══════════════════════════════════════════════════════════════════


def test_init_library_creates_example(tmp_path):
    written = init_library(tmp_path, with_example=True)
    assert (tmp_path / "README.md").is_file()
    assert (tmp_path / "_template" / "scenario.md").is_file()
    assert (tmp_path / "payment" / "scenario.md").is_file()
    assert (tmp_path / "payment" / "refund" / "checklist.md").is_file()
    # 幂等：重复 init 不炸
    init_library(tmp_path, with_example=True)
    assert written  # 首次有产出记录


def test_init_library_seeds_general(tmp_path):
    init_library(tmp_path)
    rels = {n["rel_dir"]: n for n in library_view(tmp_path)}
    assert {"api", "frontend", "sql", "shell", "skill", "agent", "ci", "unittest"} <= set(rels)
    assert rels["api"]["item_count"] == 24
    assert rels["skill"]["item_count"] == 15
    assert rels["unittest"]["item_count"] == 15
    assert [c["rel_dir"] for c in rels["frontend"]["children"]] == [
        "frontend/auth", "frontend/layout", "frontend/usability",
    ]
    # 内置业务已存在时跳过，不覆盖用户修改
    cl = tmp_path / "shell" / "checklist.md"
    cl.write_text(
        cl.read_text(encoding="utf-8").replace("set -euo pipefail", "用户自定义内容"),
        encoding="utf-8",
    )
    init_library(tmp_path)
    assert "用户自定义内容" in cl.read_text(encoding="utf-8")


def test_init_library_no_general(tmp_path):
    init_library(tmp_path, with_general=False, with_example=False)
    names = {p.name for p in tmp_path.iterdir() if p.is_dir()}
    assert not names & {"api", "frontend", "sql", "shell"}
    assert (tmp_path / "_template" / "scenario.md").is_file()


def test_summarize_requirement():
    text = summarize_requirement(
        {
            "project_context": "订单系统",
            "io_constraints": {"input": "订单ID", "output": "退款凭证"},
            "target_modules": ["refund", ""],
            "edge_cases": ["并发退款"],
        }
    )
    assert "订单系统" in text
    assert "目标模块: refund" in text
    assert "并发退款" in text
