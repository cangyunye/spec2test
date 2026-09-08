"""mermaid_fix 单元测试 + graph_generate 集成测试。

覆盖 LLM 产出 mermaid 的五类常见渲染失败：
  代码块围栏 / 字面量 \\n / 特殊字符标签未引号 / classDef 缺失 / 声明行缺失
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from devflow.mermaid_fix import mermaid_problems, sanitize_mermaid


class TestSanitizeMermaid:
    def test_strips_code_fence(self):
        src = "```mermaid\nflowchart TD\n  n-1[输入] --> n-2[处理]\n```"
        out = sanitize_mermaid(src)
        assert "```" not in out
        assert out.startswith("flowchart TD")

    def test_literal_newline_becomes_real(self):
        src = "flowchart TD\\n  n-1[输入] --> n-2[处理]"
        out = sanitize_mermaid(src)
        assert "\n" in out
        assert "\\n" not in out

    def test_quotes_risky_node_labels(self):
        src = "flowchart TD\n  n-1[表单校验(含必填)] --> n-2[处理]"
        out = sanitize_mermaid(src)
        assert 'n-1["表单校验(含必填)"]' in out
        assert "n-2[处理]" in out  # 无特殊字符不动

    def test_quotes_risky_diamond_labels(self):
        src = 'flowchart TD\n  n-1{{"已是引号"}} --> n-2{状态:已支付;继续}'
        out = sanitize_mermaid(src)
        assert 'n-2{"状态:已支付;继续"}' in out
        assert out.count("{{") == 1  # 已引号的不重复包裹

    def test_quotes_risky_edge_labels(self):
        src = "flowchart TD\n  n-1 -->|通过(OK)| n-2"
        out = sanitize_mermaid(src)
        assert '|"通过(OK)"|' in out

    def test_prepends_missing_header(self):
        out = sanitize_mermaid("  n-1[输入] --> n-2[输出]")
        assert out.splitlines()[0] == "flowchart TD"

    def test_adds_missing_classdef(self):
        src = (
            "flowchart TD\n"
            "  n-1[输入] --> n-2[处理]:::modified\n"
            "class n-1,modified-ish modified\n"  # class 语句引用
        )
        out = sanitize_mermaid(src)
        assert "classDef modified" in out
        assert mermaid_problems(out) == []

    def test_class_statement_only_form(self):
        """class <id列表> <类名> 语法：最后 token 才是类名，补的 classDef 名要正确。"""
        src = (
            "flowchart TD\n"
            "  n-1[输入] --> n-2[处理]\n"
            "class n-1,n-2 modified\n"
        )
        out = sanitize_mermaid(src)
        assert "classDef modified fill:" in out
        assert mermaid_problems(out) == []

    def test_plain_labels_untouched(self):
        src = "flowchart TD\n  n-1[输入] -->|通过| n-2[输出]"
        assert sanitize_mermaid(src) == src + "\n"

    def test_double_quoted_labels_untouched(self):
        src = 'flowchart TD\n  n-1["含(括号)"] --> n-2[处理]'
        out = sanitize_mermaid(src)
        assert 'n-1["含(括号)"]' in out
        assert out.count('["含(括号)"]') == 1

    def test_inner_quotes_escaped(self):
        src = 'flowchart TD\n  n-1[提示"已支付"] --> n-2[输出]'
        out = sanitize_mermaid(src)
        assert '"提示#quot;已支付#quot;"' in out
        assert mermaid_problems(out) == []


class TestMermaidProblems:
    def test_clean_source_no_problems(self):
        src = (
            "flowchart TD\n"
            "  n-1[输入] -->|通过| n-2[输出]\n"
            "classDef modified fill:#f96\n"
        )
        assert mermaid_problems(src) == []

    def test_detects_missing_header_and_undefined_class(self):
        src = "  n-1[输入] --> n-2[输出]:::hl\n"
        problems = "\n".join(mermaid_problems(src))
        assert "声明行" in problems
        assert "hl" in problems

    def test_detects_odd_quotes(self):
        src = 'flowchart TD\n  n-1["未闭合] --> n-2[输出]\n'
        assert any("引号" in p for p in mermaid_problems(src))

    def test_empty(self):
        assert mermaid_problems("") == ["mermaid 源码为空"]


# ═══════════════════════════════════════════════════════════════════
# 集成：graph_generate 落库前已修复
# ═══════════════════════════════════════════════════════════════════


class TestGraphGenerateIntegration:
    def test_dirty_mermaid_sanitized_before_store(self, monkeypatch):
        from devflow.nodes.graph_gen import graph_generate_async

        dirty_mermaid = "```mermaid\nflowchart TD\n  n-1[输入(按钮)] -->|通过(OK)| n-2[处理]\n```"

        async def _fake_invoke(**kwargs):
            return {
                "nodes": [
                    {"node_id": "n-1", "label": "输入", "node_type": "io",
                     "code_ref": None, "is_modified": False},
                    {"node_id": "n-2", "label": "处理", "node_type": "module",
                     "code_ref": None, "is_modified": True},
                ],
                "edges": [
                    {"edge_id": "e-1", "from_node": "n-1", "to_node": "n-2",
                     "edge_type": "call", "condition": None, "is_modified": False},
                ],
                "mermaid_source": dirty_mermaid,
            }

        monkeypatch.setattr("devflow.nodes.graph_gen.invoke_json", _fake_invoke)
        out = asyncio.run(graph_generate_async({"requirement": {}}))
        assert out["last_error"] is None
        src = out["logic_graph"]["mermaid_source"]
        assert "```" not in src
        assert 'n-1["输入(按钮)"]' in src
        assert '|"通过(OK)"|' in src
        assert mermaid_problems(src) == []


class TestMockMermaid:
    def test_mock_llm_mermaid_is_multiline_valid(self):
        """_MockLLM 纯文本路径的 mermaid 必须有真实换行（此前是字面量 \\n 必渲染失败）。"""
        from devflow.llm_client import _MockLLM
        from langchain_core.messages import HumanMessage

        msg = asyncio.run(_MockLLM().ainvoke([
            HumanMessage(content="生成逻辑图 mermaid"),
        ]))
        src = json.loads(msg.content)["mermaid_source"]
        assert "\n" in src and "\\n" not in src
        assert mermaid_problems(sanitize_mermaid(src)) == []
