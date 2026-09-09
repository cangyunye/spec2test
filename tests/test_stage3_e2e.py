"""阶段三端到端集成测试。

用 Mock Provider + Mock LLM 跑通 build_graph_with_providers 全链路：
  clarify → graph_generate → code_search → graph_render → code_gen → test_gen → review (interrupt)
  → resume(approve) → END

以及验收 reject → 回退 code_gen → 再次到达 review。
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import HumanMessage
from langgraph.types import Command

from devflow.doc_reader import read_doc
from devflow.nodes.review import review_node, route_after_review
from devflow.orchestrator import build_graph_with_providers, initial_state
from devflow.providers import (
    MockCodeEdit,
    MockCodeGraphRender,
    MockCodeSearch,
    MockTestGen,
    Providers,
)
from devflow.schemas import empty_requirement


# ═══════════════════════════════════════════════════════════════════
# 辅助
# ═══════════════════════════════════════════════════════════════════


def _mock_providers() -> Providers:
    return Providers(
        code_search=MockCodeSearch(),
        graph_render=MockCodeGraphRender(),
        code_edit=MockCodeEdit(),
        test_gen=MockTestGen(),
    )


def _complete_requirement() -> dict[str, Any]:
    """构造一个所有字段都非空的完整需求（通过 validate_requirement 校验）。"""
    req = empty_requirement()
    req.update({
        "req_type": "new_feature",
        "project_root": "/workspace",
        "project_context": "一个测试项目，用于验证 DevFlow 全链路",
        "target_modules": ["src/main.py"],
        "existing_code_accessible": True,
        "io_constraints": {
            "input": "用户文本输入",
            "output": "JSON 格式响应",
            "latency_ms": None,
            "throughput_qps": None,
            "env": None,
        },
        "edge_cases": ["空输入", "超长文本"],
        "acceptance_criteria": ["正常返回 JSON", "空输入返回错误提示"],
    })
    return req


def _full_initial_state() -> dict[str, Any]:
    """构造一个带完整需求的初始 state（跳过澄清循环）。

    不放 HumanMessage：clarify_extract 发现无用户输入时直接返回已有 requirement，
    避免 mock LLM 覆盖预填充的字段。
    """
    state = initial_state()
    state["requirement"] = _complete_requirement()
    return state


# ═══════════════════════════════════════════════════════════════════
# TC310: build_graph_with_providers 图结构完整性
# ═══════════════════════════════════════════════════════════════════


class TestGraphStructure:
    def test_all_nodes_registered(self):
        graph = build_graph_with_providers(_mock_providers())
        node_names = set(graph.nodes.keys())
        expected = {
            "clarify_extract", "clarify_validate", "clarify_build_question",
            "compress_messages", "graph_generate", "dead_letter_drain",
            "code_search", "graph_render", "graph_review", "code_gen",
            "apply_code", "test_gen", "test_run", "review",
        }
        assert expected.issubset(node_names), f"缺失节点: {expected - node_names}"


# ═══════════════════════════════════════════════════════════════════
# TC301: Mock 全链路正常流程 → review interrupt → approve → END
# ═══════════════════════════════════════════════════════════════════


class TestE2EHappyPath:
    """完整跑通 clarify → ... → review → approve → END。"""

    @pytest.fixture(autouse=True)
    def _setup_graph(self):
        self.graph = build_graph_with_providers(_mock_providers())
        self.tid = f"tc301-{uuid.uuid4().hex[:8]}"
        self.config = {"configurable": {"thread_id": self.tid}}

    def test_full_pipeline_approve(self):
        # 1. 启动 graph（带完整需求 + 用户消息）
        state = _full_initial_state()
        list(self.graph.stream(state, self.config, stream_mode="updates"))
        # 1.5 先过制图前图种类门禁（graph_type_select interrupt）
        list(self.graph.stream(Command(resume="flowchart"), self.config, stream_mode="updates"))

        # 2. 先停在制图门禁（graph_review）：确认图↔需求对齐
        snapshot = self.graph.get_state(self.config)
        next_nodes = snapshot.next or []
        assert "graph_review" in next_nodes, f"期望先到达制图门禁，实际 next={next_nodes}"
        vals = snapshot.values
        assert vals.get("logic_graph") is not None
        assert len(vals.get("logic_graph", {}).get("nodes", [])) >= 2

        # 3. 制图门 approve → 继续到终审 review
        list(self.graph.stream(Command(resume="approve"), self.config, stream_mode="updates"))
        snapshot = self.graph.get_state(self.config)
        next_nodes = snapshot.next or []
        assert "review" in next_nodes, f"制图门通过后应到达终审 review，实际 next={next_nodes}"

        # 4. 检查 state 产物
        vals = snapshot.values
        assert len(vals.get("code_context", [])) > 0
        assert len(vals.get("code_changes", [])) > 0
        assert vals.get("test_report") is not None

        # 5. 终审 approve
        list(self.graph.stream(Command(resume="approve"), self.config, stream_mode="updates"))

        # 6. 检查流程结束
        snapshot = self.graph.get_state(self.config)
        assert snapshot.next == (), f"期望流程结束，实际 next={snapshot.next}"
        assert snapshot.values.get("current_stage") == "done"


# ═══════════════════════════════════════════════════════════════════
# TC302: 验收 reject → 回退 code_gen → 再次到达 review
# ═══════════════════════════════════════════════════════════════════


class TestE2ERejectPath:
    """验收 reject 后回退到 code_gen，重新生成后再次到达 review。"""

    @pytest.fixture(autouse=True)
    def _setup_graph(self):
        self.graph = build_graph_with_providers(_mock_providers())
        self.tid = f"tc302-{uuid.uuid4().hex[:8]}"
        self.config = {"configurable": {"thread_id": self.tid}}

    def test_reject_goes_back_to_code_gen(self):
        # 1. 跑到制图门禁并通过
        state = _full_initial_state()
        list(self.graph.stream(state, self.config, stream_mode="updates"))
        list(self.graph.stream(Command(resume="flowchart"), self.config, stream_mode="updates"))
        snapshot = self.graph.get_state(self.config)
        assert "graph_review" in (snapshot.next or [])
        list(self.graph.stream(Command(resume="approve"), self.config, stream_mode="updates"))

        # 2. 到达终审 review 后 reject
        snapshot = self.graph.get_state(self.config)
        assert "review" in (snapshot.next or [])
        list(self.graph.stream(Command(resume="reject"), self.config, stream_mode="updates"))

        # 3. 应该回退到 code_gen → test_gen → 再次到达 review
        snapshot = self.graph.get_state(self.config)
        next_nodes = snapshot.next or []
        assert "review" in next_nodes, f"reject 后应再次到达 review，实际 next={next_nodes}"


# ═══════════════════════════════════════════════════════════════════
# TC308 + TC309: review 路由函数
# ═══════════════════════════════════════════════════════════════════


class TestReviewRouting:
    def test_approve_route(self):
        state = {"current_stage": "done"}
        assert route_after_review(state) == "approved"

    def test_reject_route(self):
        state = {"current_stage": "code"}
        assert route_after_review(state) == "rejected"

    def test_review_node_approve(self):
        """review_node 在 approve 时写 current_stage=done。"""
        state = {
            "code_changes": [{"file_path": "a.py", "action": "create", "lint_passed": True}],
            "test_report": {"run": {"passed": 5, "failed": 0, "coverage_pct": 80.0}},
            "logic_graph": {"graph_id": "g1", "nodes": [], "edges": []},
        }
        # review_node 用 interrupt()，无法直接调用（会暂停）
        # 这里只测 route_after_review 的逻辑
        result = route_after_review({"current_stage": "done"})
        assert result == "approved"


# ═══════════════════════════════════════════════════════════════════
# TC311: 执行闭环 e2e — diff 真实落盘 + pytest 真实执行
# ═══════════════════════════════════════════════════════════════════

class TestE2EExecutionLoop:
    """project_root 指向真实 tmp 项目：apply_code 落盘成功，test_run 真实跑 pytest。

    Mock 的 diff 上下文首行是 "# mock"，目标项目文件预置同内容即可匹配应用。
    项目里没有测试文件 → executed=True 且 total=0 → 不声称通过，进人工验收。
    """

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path):
        self.proj = tmp_path / "proj"
        (self.proj / "src" / "mock").mkdir(parents=True)
        (self.proj / "src" / "mock" / "symbol_from_0.py").write_text("# mock\n", encoding="utf-8")
        (self.proj / "src" / "mock" / "symbol_from_1.py").write_text("# mock\n", encoding="utf-8")

        self.graph = build_graph_with_providers(_mock_providers())
        self.tid = f"tc311-{uuid.uuid4().hex[:8]}"
        self.config = {"configurable": {"thread_id": self.tid}}

        state = _full_initial_state()
        state["requirement"] = {**state["requirement"], "project_root": str(self.proj)}
        self._input_state = state

    def test_apply_and_real_test_run(self):
        # 1. 启动 → 过图种类门禁 → 停在制图门禁
        list(self.graph.stream(self._input_state, self.config, stream_mode="updates"))
        list(self.graph.stream(Command(resume="flowchart"), self.config, stream_mode="updates"))
        snapshot = self.graph.get_state(self.config)
        assert "graph_review" in (snapshot.next or [])

        # 2. 制图门通过 → 一路落盘/设计/执行到人工验收
        list(self.graph.stream(Command(resume="approve"), self.config, stream_mode="updates"))
        vals = self.graph.get_state(self.config).values

        # 3. diff 已真实落盘（含备份）
        ca = vals.get("code_apply") or {}
        assert ca.get("applied") is True, f"落盘失败: {ca}"
        changed = self.proj / "src" / "mock" / "symbol_from_0.py"
        text = changed.read_text(encoding="utf-8")
        assert text.startswith("# mock\n# "), f"文件未被修改: {text!r}"
        backup = Path(ca["backup_dir"])
        assert backup.is_dir() and (backup / "manifest.json").is_file()
        # 备份里是应用前的内容
        assert (backup / "src/mock/symbol_from_0.py").read_text(encoding="utf-8") == "# mock\n"

        # 4. pytest 真实执行过（项目无测试 → executed=True, total=0, 不声称通过）
        run = (vals.get("test_report") or {}).get("run") or {}
        assert run.get("executed") is True, f"测试未真实执行: {run}"
        assert run.get("total") == 0
        assert vals["code_changes"][0].get("test_passed") is None

        # 5. 终审 approve → 结束
        assert "review" in (self.graph.get_state(self.config).next or [])
        list(self.graph.stream(Command(resume="approve"), self.config, stream_mode="updates"))
        snapshot = self.graph.get_state(self.config)
        assert snapshot.next == ()
        assert snapshot.values.get("current_stage") == "done"


class TestE2ETestFailLoop:
    """测试失败 → 回 code_gen 回修（mock 修不好）→ 连续失败超限 → 带失败报告进人工验收。

    验证自动修复循环有界收敛，不会无限打转。
    """

    def test_failing_tests_converge_to_review(self, tmp_path):
        from devflow.config import settings as cfg

        proj = tmp_path / "proj"
        (proj / "src" / "mock").mkdir(parents=True)
        (proj / "src" / "mock" / "symbol_from_0.py").write_text("# mock\n", encoding="utf-8")
        (proj / "src" / "mock" / "symbol_from_1.py").write_text("# mock\n", encoding="utf-8")
        (proj / "tests").mkdir()
        (proj / "tests" / "test_bad.py").write_text(
            "def test_broken():\n    assert 1 == 2, '修复循环验证'\n", encoding="utf-8"
        )

        graph = build_graph_with_providers(_mock_providers())
        tid = f"tc312-{uuid.uuid4().hex[:8]}"
        config = {"configurable": {"thread_id": tid}}

        state = _full_initial_state()
        state["requirement"] = {**state["requirement"], "project_root": str(proj)}

        list(graph.stream(state, config, stream_mode="updates"))
        list(graph.stream(Command(resume="flowchart"), config, stream_mode="updates"))
        list(graph.stream(Command(resume="approve"), config, stream_mode="updates"))

        snapshot = graph.get_state(config)
        assert "review" in (snapshot.next or []), "连续失败超限后应停在人工验收"
        vals = snapshot.values

        run = (vals.get("test_report") or {}).get("run") or {}
        assert run.get("executed") is True
        assert run.get("failed") >= 1
        assert (vals["code_changes"][0] or {}).get("test_passed") is False
        # 回修轮次 = MAX_FIX_ROUNDS + 1 次失败后收敛
        assert vals["retry_count"]["test_run"] == cfg.TEST_RUN_MAX_FIX_ROUNDS + 1
        assert "test_broken" in (vals.get("test_failure") or "")
        # 失败明细进了报告（供人工验收查看）
        assert any("test_broken" in f["id"] for f in run.get("failures", []))


# ═══════════════════════════════════════════════════════════════════
# TC303-305: doc_reader 测试
# ═══════════════════════════════════════════════════════════════════


class TestDocReader:
    def test_read_txt(self, tmp_path):
        f = tmp_path / "req.txt"
        f.write_text("项目名称：测试项目\n需求：实现用户登录", encoding="utf-8")
        text = read_doc(str(f))
        assert "测试项目" in text
        assert "用户登录" in text

    def test_read_md(self, tmp_path):
        f = tmp_path / "req.md"
        f.write_text("# 需求文档\n\n实现 **用户注册** 功能", encoding="utf-8")
        text = read_doc(str(f))
        assert "需求文档" in text
        assert "用户注册" in text

    def test_read_docx(self, tmp_path):
        """创建一个 .docx 文件并读取。"""
        try:
            from docx import Document
        except ImportError:
            pytest.skip("python-docx 未安装")

        doc = Document()
        doc.add_paragraph("项目名称：DOCX 测试项目")
        doc.add_paragraph("需求概述：实现数据导出功能")

        # 添加表格
        table = doc.add_table(rows=2, cols=2)
        table.cell(0, 0).text = "字段"
        table.cell(0, 1).text = "说明"
        table.cell(1, 0).text = "验收标准"
        table.cell(1, 1).text = "导出 CSV 格式"

        f = tmp_path / "req.docx"
        doc.save(str(f))

        text = read_doc(str(f))
        assert "DOCX 测试项目" in text
        assert "数据导出功能" in text
        # 表格内容
        assert "验收标准" in text
        assert "导出 CSV 格式" in text

    def test_unsupported_format(self, tmp_path):
        f = tmp_path / "req.pdf"
        f.write_bytes(b"%PDF-1.4 fake")
        with pytest.raises(ValueError, match="不支持的需求文档格式"):
            read_doc(str(f))

    def test_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            read_doc("/nonexistent/path/to/file.docx")

    def test_empty_docx(self, tmp_path):
        """空 .docx 文件返回空字符串。"""
        try:
            from docx import Document
        except ImportError:
            pytest.skip("python-docx 未安装")

        doc = Document()
        f = tmp_path / "empty.docx"
        doc.save(str(f))

        text = read_doc(str(f))
        assert text == ""
