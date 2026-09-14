"""devflow spec 一次性自动全流程测试（auto_run）。

覆盖：
  - decide_gate: 六个门禁的自动应答值与 resume 契约一致
  - _parse_missing / _merge_fill: missing_fields 解析与点路径合并
  - fill_missing: 脑补直写 state（as_node="clarify_validate"）且来源标 inferred
  - cases_to_csv: BOM / 11 列表头 / tier 中文 / 引号转义 / CRLF（与前端 testToCSV 对齐）
  - render_spec_md: 脑补标注、不写库声明、决策时间线、用例表
  - export_all: 产物落盘
运行: pytest -v tests/test_auto_run.py
"""
from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest

import devflow.auto_run as auto_run_mod
from devflow.auto_run import (
    CSV_HEADERS,
    SpecJournal,
    cases_to_csv,
    decide_gate,
    export_all,
    fill_missing,
    _merge_fill,
    _parse_missing,
    render_spec_md,
)


def _req(**over):
    base = {
        "req_type": "new_feature",
        "project_context": "商城下单支付流程",
        "io_constraints": {"input": "提交订单请求", "output": "支付结果通知"},
        "edge_cases": ["库存不足时下单"],
        "acceptance_criteria": ["支付成功后订单状态为已支付"],
    }
    base.update(over)
    return base


# ═══════════════════════════════════════════════════════════════════
# decide_gate：六门禁自动应答
# ═══════════════════════════════════════════════════════════════════

class TestDecideGate:
    def test_requirement_review_confirm(self):
        vals = {
            "requirement": _req(),
            "requirement_sources": {"edge_cases": "inferred", "project_context": "user"},
        }
        decision, rationale = decide_gate("requirement_review", vals)
        assert decision == "confirm"
        assert "edge_cases" in rationale  # 脑补字段在理由中点名

    def test_graph_type_select_default_flowchart(self):
        # 无任何图类型关键词命中的中性需求 → 恒推荐的默认 flowchart
        req = _req(
            project_context="工具函数的输入校验增强",
            io_constraints={"input": "非法参数", "output": "校验报告"},
            edge_cases=["参数为空时给出提示"],
            acceptance_criteria=["非法参数被拦截并生成报告"],
        )
        decision, rationale = decide_gate("graph_type_select", {"requirement": req})
        assert decision == "flowchart"

    def test_graph_type_select_sequence_keywords(self):
        req = _req(project_context="微服务接口调用时序与消息交互顺序",
                   acceptance_criteria=["接口调用顺序可追踪", "消息交互不丢"])
        decision, rationale = decide_gate("graph_type_select", {"requirement": req})
        assert decision == "sequence"
        assert "推荐" in rationale

    def test_graph_review_approve(self):
        decision, _ = decide_gate("graph_review", {})
        assert decision == "approve"

    def test_checklist_route_confirm_suggested(self):
        vals = {"checklist_route": {
            "candidates": [
                {"rel_dir": "payment", "name": "支付", "suggested": True,
                 "children": [{"rel_dir": "payment/refund", "name": "退款", "suggested": True}]},
                {"rel_dir": "user", "name": "用户", "suggested": False, "children": []},
            ],
        }}
        decision, rationale = decide_gate("checklist_route_gate", vals)
        assert decision == {"decision": "confirm", "selected": ["payment", "payment/refund"]}
        assert "payment" in rationale

    def test_checklist_route_skip_when_no_candidates(self):
        decision, rationale = decide_gate(
            "checklist_route_gate", {"checklist_route": {"status": "empty_library", "candidates": []}}
        )
        assert decision == {"decision": "skip"}
        assert "为空" in rationale

    def test_feature_gate_skip_uses_recommended(self):
        decision, rationale = decide_gate("feature_gate", {"feature_questions": []})
        assert decision == {"decision": "skip"}
        assert "推荐" in rationale

    def test_review_approve_adopts_all_cases(self):
        vals = {"test_report": {"test_cases": [
            {"case_id": "TC-001"}, {"case_id": "TC-002"}, {"case_id": "TC-003"},
        ]}}
        decision, rationale = decide_gate("review", vals)
        assert decision == {"decision": "approve", "adopted": ["TC-001", "TC-002", "TC-003"]}
        assert "3" in rationale

    def test_review_adopts_pi_execution_entries(self):
        """pi 代码模式的执行条目（无 case_id，有 test_symbol）也要被全部采纳。"""
        vals = {"test_report": {"test_cases": [
            {"test_symbol": "test_div_zero", "test_file": "tests/test_calc.py"},
            {"test_symbol": "test_div_negative", "test_file": "tests/test_calc.py"},
        ]}}
        decision, _ = decide_gate("review", vals)
        assert decision == {"decision": "approve",
                            "adopted": ["test_div_zero", "test_div_negative"]}

    def test_unknown_gate_raises(self):
        with pytest.raises(ValueError):
            decide_gate("nope", {})


# ═══════════════════════════════════════════════════════════════════
# 缺失解析与合并
# ═══════════════════════════════════════════════════════════════════

class TestParseMissing:
    def test_parse(self):
        out = _parse_missing([
            "edge_cases: 至少列出 1 个边界场景",
            "io_constraints.input: 必须填写输入约束",
        ])
        assert out == {
            "edge_cases": "至少列出 1 个边界场景",
            "io_constraints.input": "必须填写输入约束",
        }

    def test_empty(self):
        assert _parse_missing([]) == {}
        assert _parse_missing(None) == {}


class TestMergeFill:
    def test_top_level_and_dotted(self):
        req = _req()
        out = _merge_fill(req, {
            "edge_cases": ["a", "b"],
            "io_constraints.input": "新输入",
        })
        assert out["edge_cases"] == ["a", "b"]
        assert out["io_constraints"]["input"] == "新输入"
        assert out["io_constraints"]["output"] == "支付结果通知"  # 原字段保留
        assert req["io_constraints"]["input"] == "提交订单请求"  # 原 dict 不被改

    def test_creates_missing_parent(self):
        out = _merge_fill({}, {"io_constraints.output": "x"})
        assert out == {"io_constraints": {"output": "x"}}


# ═══════════════════════════════════════════════════════════════════
# fill_missing：脑补直写 state
# ═══════════════════════════════════════════════════════════════════

class _FakeGraph:
    """记录 update_state / stream 调用的假图。"""

    def __init__(self):
        self.updates: list[tuple[dict, str | None]] = []
        self.streams: list = []

    def update_state(self, config, values, as_node=None):
        self.updates.append((values, as_node))

    def stream(self, payload, config, **kw):
        self.streams.append(payload)
        return iter([])


class TestFillMissing:
    def _run(self, monkeypatch, fields, llm_exc=None, mock_fallback=False):
        graph = _FakeGraph()
        vals = {
            "requirement": _req(),
            "requirement_sources": {"project_context": "user"},
            "missing_fields": ["edge_cases: 至少列出 1 个边界场景",
                               "acceptance_criteria: 至少定义 1 条验收标准"],
        }
        journal = SpecJournal()

        async def fake_sleep(sec):
            return None

        monkeypatch.setattr(auto_run_mod, "_sleep", fake_sleep)

        async def fake_invoke_json(system, user, **kw):
            if llm_exc:
                raise llm_exc
            if mock_fallback and kw.get("meta") is not None:
                kw["meta"]["mock"] = True
            return {"fields": fields}

        import devflow.llm_client as lc
        monkeypatch.setattr(lc, "invoke_json", fake_invoke_json)
        ok = fill_missing(graph, {}, vals, "需求文档全文", journal, console=None)
        return graph, vals, journal, ok

    def test_fill_writes_state_with_inferred(self, monkeypatch):
        graph, vals, journal, ok = self._run(
            monkeypatch,
            {"edge_cases": ["库存不足", "重复支付"], "acceptance_criteria": ["AC-1"]},
        )
        assert ok
        (values, as_node), = graph.updates  # 恰好一次 update_state
        assert as_node == "clarify_validate"
        assert values["requirement"]["edge_cases"] == ["库存不足", "重复支付"]
        assert values["requirement"]["acceptance_criteria"] == ["AC-1"]
        assert values["requirement"]["io_constraints"]["input"] == "提交订单请求"  # 已有字段不动
        # 新补字段标 inferred，原有来源保留
        assert values["requirement_sources"]["edge_cases"] == "inferred"
        assert values["requirement_sources"]["acceptance_criteria"] == "inferred"
        assert values["requirement_sources"]["project_context"] == "user"
        assert values["missing_fields"] == []
        assert journal.filled_fields == {
            "edge_cases": ["库存不足", "重复支付"], "acceptance_criteria": ["AC-1"],
        }

    def test_fill_ignores_non_missing_fields(self, monkeypatch):
        """模型顺手返回非缺失字段 → 不采纳，防止改写文档原话。"""
        graph, vals, journal, ok = self._run(
            monkeypatch,
            {"edge_cases": ["x"], "acceptance_criteria": ["y"], "project_context": "被篡改"},
        )
        assert ok
        (values, _), = graph.updates
        assert values["requirement"]["project_context"] == "商城下单支付流程"

    def test_fill_llm_failure_returns_false(self, monkeypatch):
        graph, vals, journal, ok = self._run(
            monkeypatch, None, llm_exc=RuntimeError("all providers down")
        )
        assert not ok
        assert graph.updates == []
        assert journal.entries[-1]["kind"] == "error"

    def test_fill_empty_result_returns_false(self, monkeypatch):
        _, _, journal, ok = self._run(monkeypatch, {})
        assert not ok

    def test_fill_mock_fallback_flagged(self, monkeypatch):
        """LLM 全挂走 mock 兜底：字段照填但 journal 标记 mock（报告需警示）。"""
        _, _, journal, ok = self._run(
            monkeypatch, {"edge_cases": ["x"], "acceptance_criteria": ["y"]}, mock_fallback=True,
        )
        assert ok
        assert journal.fill_mock is True

    def test_fill_retries_transient_errors(self, monkeypatch):
        """前序节点抖动把熔断器打开：脑补调用带间隔重试,恢复后成功。"""
        import devflow.auto_run as ar

        graph = _FakeGraph()
        vals = {
            "requirement": _req(),
            "requirement_sources": {},
            "missing_fields": ["edge_cases: 至少列出 1 个边界场景"],
        }
        journal = SpecJournal()
        calls = {"n": 0}
        sleeps: list[float] = []

        async def fake_sleep(sec):
            sleeps.append(sec)

        async def fake_invoke_json(system, user, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("Connection error")
            return {"fields": {"edge_cases": ["恢复后补上"]}}

        monkeypatch.setattr(ar, "_sleep", fake_sleep)
        import devflow.llm_client as lc
        monkeypatch.setattr(lc, "invoke_json", fake_invoke_json)
        ok = fill_missing(graph, {}, vals, "文档", journal, console=None)
        assert ok
        assert calls["n"] == 2
        assert sleeps == [20.0]  # 第 1 次失败后等 20s 重试
        assert journal.filled_fields == {"edge_cases": ["恢复后补上"]}

    def test_fill_retries_exhausted_returns_false(self, monkeypatch):
        """重试全部失败：返回 False,错误入 journal。"""
        import devflow.auto_run as ar

        graph = _FakeGraph()
        vals = {
            "requirement": _req(),
            "requirement_sources": {},
            "missing_fields": ["edge_cases: 至少列出 1 个边界场景"],
        }
        journal = SpecJournal()
        calls = {"n": 0}

        async def fake_sleep(sec):
            return None

        async def fake_invoke_json(system, user, **kw):
            calls["n"] += 1
            raise RuntimeError("still down")

        monkeypatch.setattr(ar, "_sleep", fake_sleep)
        import devflow.llm_client as lc
        monkeypatch.setattr(lc, "invoke_json", fake_invoke_json)
        ok = fill_missing(graph, {}, vals, "文档", journal, console=None)
        assert not ok
        assert calls["n"] == ar._FILL_ATTEMPTS
        assert graph.updates == []
        assert journal.entries[-1]["kind"] == "error"

    def test_fill_mock_requirement_shaped_result(self, monkeypatch):
        """mock 兜底返回需求形状数据（无 fields 包装）：按缺失字段取值，点路径取子键。"""
        graph = _FakeGraph()
        vals = {
            "requirement": {},
            "requirement_sources": {},
            "missing_fields": ["project_context: 必须填写项目背景简述",
                               "io_constraints.input: 必须填写输入约束",
                               "edge_cases: 至少列出 1 个边界场景"],
        }
        journal = SpecJournal()

        async def fake_invoke_json(system, user, **kw):
            if kw.get("meta") is not None:
                kw["meta"]["mock"] = True
            # _MockLLM 返回的是 RequirementExtract 形状的演示需求，无 "fields" 包装
            return {
                "project_context": "Mock 演示项目",
                "io_constraints": {"input": "按钮点击", "output": "运算结果"},
                "target_modules": ["calc.py"],
            }

        import devflow.llm_client as lc
        monkeypatch.setattr(lc, "invoke_json", fake_invoke_json)
        ok = fill_missing(graph, {}, vals, "文档", journal, console=None)
        assert ok
        assert journal.fill_mock is True
        (values, as_node), = graph.updates
        assert as_node == "clarify_validate"
        # 相交字段被采纳（含点路径 → 子键），未提供的 acceptance_criteria 不在其中
        assert values["requirement"]["project_context"] == "Mock 演示项目"
        assert values["requirement"]["io_constraints"]["input"] == "按钮点击"
        assert "edge_cases" not in values["requirement_sources"]
        assert values["requirement_sources"]["project_context"] == "inferred"
        assert values["requirement_sources"]["io_constraints.input"] == "inferred"


# ═══════════════════════════════════════════════════════════════════
# cases_to_csv：与前端 testToCSV 对齐
# ═══════════════════════════════════════════════════════════════════

def _case(**over):
    base = {
        "case_id": "TC-001", "tier": "functional", "priority": "P0",
        "case_type": "正向", "title": "正常下单支付", "target": "支付模块",
        "precondition": "库存充足", "steps": "1. 下单\n2. 支付", "expected": "状态=已支付",
        "data_requirement": "有效订单号", "rationale": "覆盖核心路径", "feature_id": "F1",
    }
    base.update(over)
    return base


class TestCasesToCsv:
    def test_bom_headers_crlf(self):
        text = cases_to_csv([_case()])
        assert text.startswith("﻿")
        assert "\r\n" in text
        rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
        assert rows[0] == list(CSV_HEADERS)
        assert len(rows[0]) == 12  # 11 列 + 来源（人工/AI）
        assert rows[1][0] == "TC-001"
        assert rows[1][1] == "功能"  # tier 中文映射

    def test_quote_escaping(self):
        text = cases_to_csv([_case(title='带"引号"的标题')])
        assert '"带""引号""的标题"' in text

    def test_empty_cases(self):
        rows = list(csv.reader(io.StringIO(cases_to_csv([]).lstrip("﻿"))))
        assert rows == [list(CSV_HEADERS)]

    def test_pi_execution_entry_fallback(self):
        """pi 执行条目形态：标识/标题回退 test_symbol,所属模块回退 test_file,步骤回退代码。"""
        text = cases_to_csv([{
            "test_symbol": "test_div_zero", "test_file": "tests/test_calc.py",
            "code_snippet": "def test_div_zero():\n    assert div(1, 0) is None",
        }])
        rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
        assert rows[1][0] == "test_div_zero"       # 标识
        assert rows[1][4] == "test_div_zero"       # 标题
        assert rows[1][5] == "tests/test_calc.py"  # 所属模块
        assert "div(1, 0)" in rows[1][7]           # 步骤 = 代码
        assert rows[1][8] == ""                    # 预期结果无回退,留空


# ═══════════════════════════════════════════════════════════════════
# render_spec_md / export_all
# ═══════════════════════════════════════════════════════════════════

def _journal_with_fill():
    journal = SpecJournal()
    journal.record("doc", path="req.md", chars=1200)
    journal.record("gate", gate="graph_type_select", decision="sequence", rationale="命中关键词")
    journal.record("gate", gate="review", decision={"decision": "approve", "adopted": ["TC-001"]},
                   rationale="验收通过")
    journal.filled_fields["edge_cases"] = ["库存不足"]
    journal.record("fill", round=1, fields={"edge_cases": ["库存不足"]}, mock=False)
    return journal


def _state():
    return {
        "current_stage": "done",
        "requirement": _req(),
        "requirement_sources": {
            "project_context": "user", "edge_cases": "inferred",
            "io_constraints.input": "user", "io_constraints.output": "user",
            "acceptance_criteria": "user",
        },
        "logic_graph": {
            "graph_id": "g1", "graph_type": "flowchart",
            "mermaid_source": "flowchart TD\n  A[下单] --> B[支付]",
            "nodes": [], "edges": [],
        },
        "checklist_route": {"decision": "confirm", "selected": ["payment"]},
        "test_report": {
            "overview": "覆盖下单支付核心链路",
            "test_cases": [_case(), _case(case_id="TC-002", priority="P1", title="库存不足下单")],
            "self_check": ["边界场景已覆盖"],
            "run": {"executed": True, "passed": 2, "failed": 0, "errors": 0,
                    "skipped": 0, "coverage_pct": 100.0, "duration_sec": 3.2},
            "features": [{"feature_id": "F1", "name": "下单支付", "description": "核心链路"}],
        },
        "adopted_cases": ["TC-001", "TC-002"],
    }


class TestRenderSpecMd:
    def test_header_and_no_library_write(self):
        md = render_spec_md("spec-t1", _journal_with_fill(), _state(), doc_path="req.md")
        assert md.startswith("# CaseCraft spec 全自动流程报告 · spec-t1")
        assert "不写入 checklist 库" in md
        assert "无人工评审" in md

    def test_inferred_field_labelled(self):
        md = render_spec_md("spec-t1", _journal_with_fill(), _state())
        assert "AI 脑补的最佳选择" in md
        assert "用户文档" in md

    def test_sections_present(self):
        md = render_spec_md("spec-t1", _journal_with_fill(), _state())
        for sec in ("## 1. 需求清单", "## 2. 自动决策时间线", "## 3. AI 脑补字段明细",
                    "## 4. 逻辑图", "## 5. Checklist 路由", "## 6. Feature 拆分",
                    "## 7. 测试用例", "## 8. 测试执行与代码变更"):
            assert sec in md, f"缺少章节 {sec}"
        assert "flowchart TD" in md
        assert "TC-001" in md and "TC-002" in md
        assert "passed=2" in md

    def test_no_fill_section(self):
        journal = SpecJournal()  # 无脑补
        md = render_spec_md("spec-t2", journal, _state())
        assert "无需脑补" in md

    def test_mock_fill_warning(self):
        journal = _journal_with_fill()
        journal.fill_mock = True
        md = render_spec_md("spec-t3", journal, _state())
        assert "mock 兜底演示数据" in md


class TestExportAll:
    def test_writes_files(self, tmp_path):
        files = export_all(_state(), _journal_with_fill(), "spec-x", tmp_path, doc_path="req.md")
        names = {f.name for f in files}
        assert f"casecraft-spec-spec-x.md" in names
        assert f"casecraft-tests-spec-x.csv" in names
        assert "requirement.json" in names
        assert "logic_graph.mmd" in names
        assert "test_report.json" in names
        for f in files:
            assert f.exists() and f.stat().st_size > 0
