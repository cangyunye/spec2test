"""测试设计 feature 化（拆分 → map-reduce 生成 → 评审合并 → 交付）集成测试。

覆盖：
  1. 纯逻辑层：聚类 / 覆盖自检必归属 / 超限合并 / 单 feature 评审 / 合并重编
  2. 节点层：single 模式直通、降级整单、回炉不重拆、覆盖缺口转门禁问题
  3. 门禁层：feature_questions interrupt 的回答合并（confirm / skip）
  4. map-reduce：并发逐 feature 生成、F0 集成轮、单点失败隔离、全失败回退、
     回炉计数、清单子集分配
  5. 交付：timetravel 下游清理、导出 feature 章节、技能路径解析与 prompt 前缀
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from devflow import feature_split as fs
from devflow.errors import DevFlowError
from devflow.feature_split import (
    F0_ID,
    assign_uncovered,
    build_feature_design_prompt,
    checklists_for_feature,
    clamp_features,
    cluster_nodes_by_target,
    make_feature,
    make_f0,
    merge_feature_reports,
    renumber_features,
    render_test_design_plan,
    resolve_skill_paths,
    skill_dispatch_prefix,
    validate_feature_report,
)
from devflow.nodes.feature_split import feature_gate_node, feature_split_node
from devflow.nodes.provider_nodes import make_test_gen_node
from devflow.providers import Providers
from devflow.providers.base import TestGenProvider
from devflow.providers.mock import (
    MockCodeEdit,
    MockCodeGraphRender,
    MockCodeSearch,
)
from devflow.timetravel import downstream_fields


def _req(**kw: Any) -> dict[str, Any]:
    base = {
        "project_context": "电商结算",
        "target_modules": ["购物车", "优惠券"],
        "acceptance_criteria": ["总价计算正确", "优惠券抵扣生效"],
        "edge_cases": ["数量为 0", "优惠券过期"],
    }
    base.update(kw)
    return base


# ═══════════════════════════════════════════════════════════════════
# 1. 纯逻辑层
# ═══════════════════════════════════════════════════════════════════

class TestCluster:
    def test_cluster_by_code_ref(self):
        graph = {"nodes": [
            {"node_id": "n1", "label": "加购", "code_ref": {"file_path": "src/cart.py"}, "is_modified": True},
            {"node_id": "n2", "label": "改量", "code_ref": {"file_path": "src/cart.py"}},
            {"node_id": "n3", "label": "核销", "code_ref": {"file_path": "src/coupon.py"}},
        ]}
        feats = cluster_nodes_by_target(graph, _req())
        assert [f["feature_id"] for f in feats] == ["F1", "F2"]
        assert feats[0]["node_ids"] == ["n1", "n2"]
        assert feats[1]["node_ids"] == ["n3"]
        assert feats[0]["is_modified"] is False  # 聚类不推断 modified

    def test_cluster_by_module_then_type(self):
        graph = {"nodes": [
            {"node_id": "n1", "label": "购物车页面"},
            {"node_id": "n2", "label": "输出渲染", "node_type": "io"},
        ]}
        feats = cluster_nodes_by_target(graph, _req())
        assert len(feats) == 2
        assert feats[0]["target_modules"] == ["购物车"]

    def test_cluster_empty_graph(self):
        assert cluster_nodes_by_target(None, _req()) == []
        assert cluster_nodes_by_target({"nodes": []}, _req()) == []


class TestAssignUncovered:
    def test_exact_assignment_kept(self):
        feats = [make_feature("F1", "购物车", acceptance_criteria=["总价计算正确"])]
        out, gaps = assign_uncovered(feats, _req())
        assert out[0]["acceptance_criteria"] == ["总价计算正确"]
        # 未在 split 里出现的准则关键词归入 / F0 兜底
        assert gaps  # 优惠券抵扣生效、数量为 0、优惠券过期都未归属

    def test_unassigned_go_to_f0(self):
        feats = [make_feature("F1", "购物车", acceptance_criteria=["总价计算正确"])]
        out, gaps = assign_uncovered(feats, _req())
        f0 = next(f for f in out if f["feature_id"] == F0_ID)
        texts = f0["acceptance_criteria"] + f0["edge_cases"]
        assert "优惠券抵扣生效" in texts and "优惠券过期" in texts
        assert len(gaps) >= 2

    def test_keyword_match_assigns_to_feature(self):
        feats = [make_feature("F1", "优惠券核算", target_modules=["优惠券"])]
        out, _ = assign_uncovered(feats, _req())
        f1 = out[0]
        assert "优惠券抵扣生效" in (f1["acceptance_criteria"] + f1["edge_cases"]) or \
               "优惠券过期" in (f1["acceptance_criteria"] + f1["edge_cases"])


class TestClampAndRenumber:
    def test_renumber_keeps_f0_last(self):
        feats = [make_feature("", "a"), make_feature("", "b"), make_f0()]
        out = renumber_features(feats)
        assert [f["feature_id"] for f in out] == ["F1", "F2", F0_ID]

    def test_clamp_overflow_into_f0(self):
        feats = [make_feature("", f"f{i}", acceptance_criteria=[f"准则{i}"]) for i in range(5)]
        out = clamp_features(feats, 3)
        assert len(out) == 3
        assert out[-1]["feature_id"] == F0_ID
        # 被合并的准则不丢
        f0 = out[-1]
        assert "准则4" in (f0["acceptance_criteria"] + f0["edge_cases"])

    def test_clamp_within_limit_unchanged(self):
        feats = [make_feature("F1", "a"), make_feature("F2", "b")]
        assert clamp_features(feats, 8) == feats


def _report(cases: list[dict[str, Any]], **kw: Any) -> dict[str, Any]:
    return {"test_cases": cases, "self_check": kw.get("self_check", []),
            "checklist_refs": kw.get("refs", []), "open_issues": kw.get("issues", []),
            "session_id": kw.get("sid", "s")}


def _case(title: str, *, case_type: str = "正向", rationale: str = "", **kw: Any) -> dict[str, Any]:
    return {"title": title, "case_type": case_type, "rationale": rationale, **kw}


class TestValidateFeatureReport:
    def test_empty_cases(self):
        f = make_feature("F1", "购物车")
        assert validate_feature_report(f, _report([])) == ["未产出任何用例"]

    def test_missing_positive(self):
        f = make_feature("F1", "购物车")
        issues = validate_feature_report(f, _report([_case("x", case_type="反向")]))
        assert any("正向" in i for i in issues)

    def test_uncovered_criteria_flagged(self):
        f = make_feature("F1", "购物车", acceptance_criteria=["总价计算正确"])
        issues = validate_feature_report(
            f, _report([_case("正常下单", rationale="功能:购物车")]))
        assert any("总价计算正确" in i for i in issues)

    def test_covered_via_rationale_marker(self):
        f = make_feature("F1", "购物车",
                         acceptance_criteria=["总价计算正确"],
                         edge_cases=["数量为 0"])
        rep = _report([
            _case("正常下单总价正确", rationale="验收:总价计算正确"),
            _case("零数量提示", case_type="边界值", rationale="边界:数量为 0"),
        ])
        assert validate_feature_report(f, rep) == []


class TestMerge:
    def test_merge_renumber_dedupe_stamp(self):
        f1 = make_feature("F1", "购物车", acceptance_criteria=["总价计算正确"])
        f2 = make_feature("F2", "优惠券", acceptance_criteria=["优惠券抵扣生效"])
        r1 = _report([_case("正常下单"), _case("重复用例")], sid="s1",
                     self_check=["正向已覆盖"])
        r2 = _report([_case("重复用例"), _case("过期券被拒", case_type="反向")], sid="s2",
                     self_check=["反向已覆盖"])
        merged = merge_feature_reports(
            [(f1, r1), (f2, r2)], features=[f1, f2], requirement=_req())
        cases = merged["test_cases"]
        assert [c["case_id"] for c in cases] == ["TC-001", "TC-002", "TC-003"]
        assert cases[0]["feature_id"] == "F1" and cases[2]["feature_id"] == "F2"
        # 去重：F2 的「重复用例」被丢弃（title 归一）
        assert [c["title"] for c in cases] == ["正常下单", "重复用例", "过期券被拒"]
        assert merged["features"] == [f1, f2]
        assert any("[F1]" in s for s in merged["self_check"])
        assert merged["run"]["passed"] == 3
        # 会话 id 聚合
        assert "s1" in merged["session_id"] and "s2" in merged["session_id"]


class TestChecklistSubset:
    def test_keyword_subset(self):
        cls = [
            {"rel_dir": "payment", "name": "支付清单", "content": "购物车结算必须校验库存"},
            {"rel_dir": "refund", "name": "退款清单", "content": "退款需原路退回"},
        ]
        f = make_feature("F1", "购物车结算", target_modules=["购物车"])
        sub = checklists_for_feature(cls, f)
        assert [c["rel_dir"] for c in sub] == ["payment"]

    def test_no_feature_returns_empty(self):
        cls = [{"rel_dir": "payment", "name": "n", "content": "c"}]
        assert checklists_for_feature(cls, None) == []
        assert checklists_for_feature([], make_feature("F1", "x")) == []


class TestPlanAndSkill:
    def test_render_plan_markdown(self):
        feats = [make_feature("F1", "购物车", "结算职责",
                              target_modules=["购物车"],
                              acceptance_criteria=["总价计算正确"])]
        plan = render_test_design_plan(feats, _req(),
                                       questions=[{"question": "Q?", "recommended": "A"}])
        assert "F1 购物车" in plan and "验收：总价计算正确" in plan
        assert "Q?" in plan and "A" in plan
        assert "验收准出" in plan

    def test_resolve_skill_paths_and_prefix(self, tmp_path):
        skill_dir = tmp_path / "skills"
        (skill_dir / "test-design-execute").mkdir(parents=True)
        (skill_dir / "test-design-execute" / "SKILL.md").write_text("x", encoding="utf-8")
        paths = resolve_skill_paths(skill_dir, "test-design-execute", "test-design-plan")
        assert len(paths) == 1 and paths[0].endswith("test-design-execute\\SKILL.md") or \
               paths[0].endswith("test-design-execute/SKILL.md")
        prefix = skill_dispatch_prefix(paths)
        assert paths[0] in prefix and "完整读取" in prefix

    def test_feature_prompt_contains_contract(self):
        f = make_feature("F1", "购物车", acceptance_criteria=["总价计算正确"])
        prompt = build_feature_design_prompt(
            f, requirement=_req(), checklists=[{"rel_dir": "p", "name": "n", "content": "c"}],
            skill_paths=["/x/SKILL.md"])
        assert "/x/SKILL.md" in prompt
        assert "验收：总价计算正确" in prompt or "- 总价计算正确" in prompt
        assert "cases" in prompt  # envelope 契约

    def test_f0_prompt_mentions_siblings(self):
        f0 = make_f0()
        f0["sibling_features"] = ["购物车", "优惠券"]
        prompt = build_feature_design_prompt(f0)
        assert "跨功能端到端" in prompt and "购物车" in prompt


# ═══════════════════════════════════════════════════════════════════
# 2. 节点层：feature_split_node
# ═══════════════════════════════════════════════════════════════════

class TestFeatureSplitNode:
    def test_single_mode_passthrough(self, monkeypatch):
        from devflow.config import settings

        monkeypatch.setattr(settings, "TEST_DESIGN_MODE", "single")
        assert feature_split_node({"requirement": _req(), "logic_graph": None}) == {}

    def test_features_exist_skip(self, monkeypatch):
        from devflow.config import settings

        monkeypatch.setattr(settings, "TEST_DESIGN_MODE", "feature")
        state = {"features": [make_feature("F1", "已有")]}
        assert feature_split_node(state) == {}

    def test_llm_unavailable_falls_back_to_single(self, monkeypatch):
        from devflow.config import settings
        import devflow.nodes.feature_split as node_mod

        monkeypatch.setattr(settings, "TEST_DESIGN_MODE", "feature")
        async def _none(*a, **k):
            return None
        monkeypatch.setattr(node_mod, "llm_split_features", _none)
        out = feature_split_node({"requirement": _req(), "logic_graph": None})
        feats = out["features"]
        assert len(feats) == 1
        assert "总价计算正确" in feats[0]["acceptance_criteria"]
        assert "优惠券过期" in feats[0]["edge_cases"]

    def test_code_mode_clusters(self, monkeypatch):
        from devflow.config import settings

        monkeypatch.setattr(settings, "TEST_DESIGN_MODE", "feature")
        req = _req(project_root="/tmp/prj")
        graph = {"nodes": [
            {"node_id": "n1", "label": "加购", "code_ref": {"file_path": "src/cart.py"}},
            {"node_id": "n2", "label": "核销", "code_ref": {"file_path": "src/coupon.py"}},
        ]}
        out = feature_split_node({"requirement": req, "logic_graph": graph})
        # 2 个代码聚类功能点 + 未归属准则的 F0 兜底（覆盖自检"必归属"）
        assert [f["feature_id"] for f in out["features"]][:2] == ["F1", "F2"]
        assert out["features"][-1]["feature_id"] == F0_ID
        assert out["feature_questions"]


# ═══════════════════════════════════════════════════════════════════
# 3. 门禁：feature_gate_node
# ═══════════════════════════════════════════════════════════════════

class TestFeatureGateNode:
    def test_no_pending_passthrough(self):
        assert feature_gate_node({"feature_questions": [
            {"question": "q", "resolved": True}]}) == {}
        assert feature_gate_node({"feature_questions": [{"assumption": "a"}]}) == {}

    def test_confirm_merges_answers(self, monkeypatch):
        import devflow.nodes.feature_split as node_mod

        captured: dict = {}

        def fake_interrupt(payload):
            captured["payload"] = payload
            return {"decision": "confirm", "answers": {"走哪种优惠叠加？": "叠加使用"}}

        monkeypatch.setattr(node_mod, "interrupt", fake_interrupt)
        state = {"features": [make_feature("F1", "x")],
                 "feature_questions": [
                     {"question": "走哪种优惠叠加？", "options": ["叠加", "互斥"],
                      "recommended": "互斥", "resolved": False},
                     {"assumption": "按标准定价", "resolved": True},
                 ]}
        out = feature_gate_node(state)
        qs = out["feature_questions"]
        assert qs[0]["resolved"] is True and qs[0]["answer"] == "叠加使用"
        assert captured["payload"]["type"] == "feature_questions"
        assert captured["payload"]["questions"][0]["question"] == "走哪种优惠叠加？"

    def test_skip_defaults_to_recommended(self, monkeypatch):
        import devflow.nodes.feature_split as node_mod

        monkeypatch.setattr(node_mod, "interrupt",
                            lambda payload: {"decision": "skip"})
        state = {"feature_questions": [
            {"question": "q", "options": ["a"], "recommended": "a", "resolved": False}]}
        out = feature_gate_node(state)
        assert out["feature_questions"][0]["answer"] == "a"


# ═══════════════════════════════════════════════════════════════════
# 4. map-reduce：make_test_gen_node
# ═══════════════════════════════════════════════════════════════════

class StubTestGen(TestGenProvider):
    name = "stub_test_gen"

    def __init__(self, handler) -> None:
        self.calls: list[dict[str, Any]] = []
        self._handler = handler

    async def generate(self, project_root, target_symbols, **kw) -> dict[str, Any]:
        self.calls.append({"project_root": project_root, "target_symbols": target_symbols, **kw})
        return await self._handler(**kw, calls=self.calls)


def _providers(handler) -> tuple[Providers, StubTestGen]:
    stub = StubTestGen(handler)
    return Providers(code_search=MockCodeSearch(), graph_render=MockCodeGraphRender(),
                     code_edit=MockCodeEdit(), test_gen=stub), stub


def _feat(fid: str, name: str, criteria: list[str] | None = None) -> dict[str, Any]:
    return make_feature(fid, name, acceptance_criteria=criteria or [f"{name}主流程正确"],
                        target_modules=[name])


def _state(**kw: Any) -> dict[str, Any]:
    base = {
        "requirement": _req(project_root=""),
        "logic_graph": {"nodes": [], "edges": []},
        "code_changes": [],
        "opencode_sessions": {"test_gen": None},
        "review_feedback": None,
        "features": [_feat("F1", "购物车"), _feat("F2", "优惠券")],
        "feature_questions": [],
        "checklist_context": None,
        "retry_count": {},
    }
    base.update(kw)
    return base


class TestMapReduce:
    def test_per_feature_calls_and_merge(self, monkeypatch):
        from devflow.config import settings

        monkeypatch.setattr(settings, "TEST_DESIGN_REVIEW_ROUNDS", 2)
        monkeypatch.setattr(settings, "TEST_DESIGN_CONCURRENCY", 2)
        monkeypatch.setattr(settings, "TEST_DESIGN_USE_SKILLS", False)

        async def handler(**kw):
            f = kw["feature"]
            fid = f["feature_id"]
            if fid == F0_ID:
                return _report([_case("全流程端到端", rationale="功能:综合")])
            return _report([_case(f"{f['name']}正常路径",
                                  rationale=f"验收:{f['acceptance_criteria'][0]}")])

        p, stub = _providers(handler)
        node = make_test_gen_node(p)
        out = node(_state())
        report = out["test_report"]
        # 3 次调用：F1、F2、F0 集成轮（≥2 功能点补 F0）
        assert len(stub.calls) == 3
        assert {c["feature"]["feature_id"] for c in stub.calls} == {"F1", "F2", F0_ID}
        # 每次调用只喂自己的目标
        f1_call = next(c for c in stub.calls if c["feature"]["feature_id"] == "F1")
        assert f1_call["target_symbols"] == ["购物车"]
        # 合并后 case_id 全局连续 + feature 归属
        assert [c["case_id"] for c in report["test_cases"]] == ["TC-001", "TC-002", "TC-003"]
        assert report["features"][0]["feature_id"] == "F1"
        assert report["test_cases"][2]["feature_id"] == F0_ID

    def test_single_feature_failure_isolated(self, monkeypatch):
        from devflow.config import settings

        monkeypatch.setattr(settings, "TEST_DESIGN_USE_SKILLS", False)

        async def handler(**kw):
            if kw["feature"]["feature_id"] == "F2":
                raise DevFlowError("LLM.UPSTREAM", "boom")
            f = kw["feature"]
            return _report([_case(f"{f['name']}正常路径", rationale="验收:主流程")])

        p, _ = _providers(handler)
        out = make_test_gen_node(p)(_state())
        report = out["test_report"]
        # F2 失败记 gap，不拖垮整单
        assert any("F2" in s for s in report["self_check"])
        assert {c["feature_id"] for c in report["test_cases"]} == {"F1", F0_ID}
        assert out["last_error_code"] is None

    def test_all_failed_falls_back_to_single(self, monkeypatch):
        from devflow.config import settings

        monkeypatch.setattr(settings, "TEST_DESIGN_USE_SKILLS", False)
        calls = {"n": 0}

        async def handler(**kw):
            calls["n"] += 1
            if kw.get("feature") is not None:
                raise DevFlowError("LLM.UPSTREAM", "boom")
            return {"session_id": "single", "test_cases": [
                {"case_id": "TC-001", "title": "整单", "case_type": "正向"}],
                "run": {"passed": 1, "failed": 0, "skipped": 0, "coverage_pct": None, "logs": ""},
                "target_symbols": ["x"], "overview": "整单", "self_check": []}

        p, _ = _providers(handler)
        out = make_test_gen_node(p)(_state())
        assert out["test_report"]["session_id"] == "single"
        assert not out["test_report"].get("features")  # 单次路径不带 feature 章节

    def test_reheat_counts_and_retries(self, monkeypatch):
        from devflow.config import settings

        monkeypatch.setattr(settings, "TEST_DESIGN_REVIEW_ROUNDS", 2)
        monkeypatch.setattr(settings, "TEST_DESIGN_USE_SKILLS", False)
        attempt: dict[str, int] = {}

        async def handler(**kw):
            fid = kw["feature"]["feature_id"]
            attempt[fid] = attempt.get(fid, 0) + 1
            if fid == "F1" and attempt[fid] == 1:
                # 首轮不带归属标注 → 评审不过 → 回炉
                return _report([_case("F1正常路径", rationale="功能:购物车")])
            return _report([_case("F1正常路径",
                                  rationale="验收:购物车主流程正确")])

        p, _ = _providers(handler)
        out = make_test_gen_node(p)(_state(
            features=[_feat("F1", "购物车")]))
        assert attempt["F1"] == 2
        assert out["retry_count"].get("test_gen:F1") == 1
        report = out["test_report"]
        assert not any("F1" in s and "遗留" in s for s in report["self_check"])

    def test_checklist_subset_and_unclaimed_to_f0(self, monkeypatch):
        from devflow.config import settings

        monkeypatch.setattr(settings, "TEST_DESIGN_USE_SKILLS", False)
        cl_cart = {"rel_dir": "cart", "name": "购物车清单", "content": "购物车要校验库存"}
        cl_misc = {"rel_dir": "misc", "name": "杂项清单", "content": "完全无关的规则"}
        seen: dict[str, Any] = {}

        async def handler(**kw):
            seen[kw["feature"]["feature_id"]] = kw.get("checklists")
            return _report([_case(f"{kw['feature']['name']}路径", rationale="验收:主流程")])

        p, _ = _providers(handler)
        make_test_gen_node(p)(_state(
            checklist_context={"checklists": [cl_cart, cl_misc]}))
        assert seen["F1"] == [cl_cart]
        # 无人认领的清单归 F0 集成轮
        assert cl_misc in (seen.get(F0_ID) or [])

    def test_no_features_single_call(self, monkeypatch):
        from devflow.config import settings

        monkeypatch.setattr(settings, "TEST_DESIGN_USE_SKILLS", False)

        async def handler(**kw):
            assert kw.get("feature") is None
            return _report([_case("整单用例")])

        p, stub = _providers(handler)
        out = make_test_gen_node(p)(_state(features=[]))
        assert len(stub.calls) == 1
        assert out["test_report"]["test_cases"][0]["title"] == "整单用例"

    def test_skill_paths_dispatched_when_enabled(self, monkeypatch, tmp_path):
        from devflow.config import settings

        skill_dir = tmp_path / "skills"
        (skill_dir / "test-design-execute").mkdir(parents=True)
        (skill_dir / "test-design-execute" / "SKILL.md").write_text("x", encoding="utf-8")
        monkeypatch.setattr(settings, "TEST_DESIGN_USE_SKILLS", True)
        monkeypatch.setattr(settings, "TEST_DESIGN_SKILL_DIR", skill_dir)
        captured: dict[str, Any] = {}

        async def handler(**kw):
            captured["skill_paths"] = kw.get("skill_paths")
            captured["qa"] = (kw["feature"] or {}).get("qa_decisions")
            return _report([_case("x")])

        p, _ = _providers(handler)
        make_test_gen_node(p)(_state(
            features=[_feat("F1", "购物车")],
            feature_questions=[{"question": "Q?", "answer": "A", "resolved": True}],
        ))
        assert captured["skill_paths"] and captured["skill_paths"][0].endswith("SKILL.md")
        assert any("Q?" in q for q in captured["qa"])


# ═══════════════════════════════════════════════════════════════════
# 5. 交付：timetravel / 导出
# ═══════════════════════════════════════════════════════════════════

class TestTimetravelAndExport:
    def test_downstream_clears_features(self):
        # 回退到清单门禁锚点 = 拆分前：features 与问题一起清空（可改功能点重拆）
        fields_after_route_gate = downstream_fields("checklist_route_gate")
        assert "features" in fields_after_route_gate
        assert "feature_questions" in fields_after_route_gate
        # 回退到拆分锚点 = 保留拆分结果、重走问题门禁（问题原样重放，无重算）
        after_split = downstream_fields("feature_split")
        assert "features" not in after_split
        assert "feature_questions" not in after_split
        after_gate = downstream_fields("feature_gate")
        assert "test_report" in after_gate

    def test_export_feature_chapters(self):
        from web.server import _build_export_md

        f1 = make_feature("F1", "购物车", "结算职责",
                          target_modules=["购物车"], acceptance_criteria=["总价计算正确"])
        f2 = make_feature("F2", "优惠券")
        vals = {
            "current_stage": "test",
            "requirement": _req(),
            "logic_graph": None,
            "code_changes": [],
            "test_report": {
                "overview": "总览",
                "test_cases": [
                    {"case_id": "TC-001", "title": "A", "feature_id": "F1",
                     "priority": "P0", "case_type": "正向"},
                    {"case_id": "TC-002", "title": "B", "feature_id": "F2",
                     "priority": "P1", "case_type": "反向"},
                ],
                "run": {"passed": 2, "failed": 0, "skipped": 0},
                "features": [f1, f2],
                "self_check": ["[F1] ok"],
            },
        }
        md = _build_export_md(vals)
        assert "### 3.1 F1 购物车" in md
        assert "### 3.2 F2 优惠券" in md
        assert "test-design-plan" in md
        assert "| TC-001 |" in md and "| TC-002 |" in md

    def test_export_fallback_legacy_grouping(self):
        from web.server import _build_export_md

        vals = {
            "current_stage": "test",
            "requirement": _req(),
            "logic_graph": None,
            "code_changes": [],
            "test_report": {
                "test_cases": [
                    {"case_id": "TC-001", "title": "A", "target": "模块甲",
                     "priority": "P0", "case_type": "正向"},
                ],
                "run": {"passed": 1, "failed": 0, "skipped": 0},
            },
        }
        md = _build_export_md(vals)
        assert "### 2.1 模块甲" in md
