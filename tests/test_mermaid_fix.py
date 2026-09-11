"""mermaid_fix 单元测试 + graph_generate 集成测试。

覆盖 LLM 产出 mermaid 的五类常见渲染失败：
  代码块围栏 / 字面量 \\n / 特殊字符标签未引号 / classDef 缺失 / 声明行缺失
"""
from __future__ import annotations

import asyncio

import pytest
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


# ═══════════════════════════════════════════════════════════════════
# 空壳检测 + 结构化数据确定性重建（网关截断 mermaid_source 的兜底）
# ═══════════════════════════════════════════════════════════════════


class TestStubRebuild:
    def test_stub_detection(self):
        from devflow.mermaid_fix import is_stub_mermaid

        assert is_stub_mermaid("", "flowchart")
        assert is_stub_mermaid("flowchart TD", "flowchart")
        assert is_stub_mermaid("sequenceDiagram", "sequence")
        assert not is_stub_mermaid("flowchart TD\n  a[提交订单] --> b[校验库存]", "flowchart")

    def test_rebuild_flowchart(self):
        from devflow.mermaid_fix import mermaid_problems, rebuild_mermaid_source

        src = rebuild_mermaid_source({
            "nodes": [
                {"node_id": "n-1", "label": '提交"订单"', "node_type": "io"},
                {"node_id": "n-2", "label": "校验库存", "node_type": "module"},
            ],
            "edges": [
                {"from_node": "n-1", "to_node": "n-2", "condition": "库存充足"},
                {"from_node": "n-1", "to_node": "n-9", "condition": "坏边"},
            ],
        }, "flowchart")
        assert src and src.startswith("flowchart TD")
        assert 'n-1["提交#quot;订单#quot;"]' in src  # 标签引号被转义
        assert 'n-1 -->|"库存充足"| n-2' in src
        assert "n-9" not in src  # 指向不存在节点的坏边被剔除
        assert mermaid_problems(src, "flowchart") == []

    def test_rebuild_sequence(self):
        from devflow.mermaid_fix import rebuild_mermaid_source

        src = rebuild_mermaid_source({
            "participants": [
                {"alias": "USER", "label": "操作用户", "kind": "actor"},
                {"alias": "APP", "label": "商城", "kind": "service"},
            ],
            "messages": [
                {"from_participant": "USER", "to_participant": "APP",
                 "kind": "sync", "label": "提交订单"},
                {"from_participant": "APP", "to_participant": "USER",
                 "kind": "return", "label": "返回订单号"},
            ],
        }, "sequence")
        assert "sequenceDiagram" in src
        assert "actor USER as 操作用户" in src
        assert "USER ->> APP: 提交订单" in src
        assert "APP -->> USER: 返回订单号" in src

    def test_rebuild_state(self):
        from devflow.mermaid_fix import rebuild_mermaid_source

        src = rebuild_mermaid_source({
            "states": [
                {"state_id": "s_pay", "label": "待支付", "kind": "normal"},
                {"state_id": "s_done", "label": "已支付", "kind": "final"},
            ],
            "transitions": [
                {"from_state": "s_pay", "to_state": "s_done", "event": "扣款成功"},
            ],
        }, "state")
        assert "stateDiagram-v2" in src
        assert "s_pay : 待支付" in src
        assert "s_pay --> s_done: 扣款成功" in src

    def test_rebuild_er(self):
        from devflow.mermaid_fix import mermaid_problems, rebuild_mermaid_source

        src = rebuild_mermaid_source({
            "entities": [
                {"e_id": "e-order", "table": "ORDER",
                 "attributes": [{"name": "order_id", "type": "int", "is_pk": True}]},
                {"e_id": "e-user", "table": "USER", "attributes": []},
            ],
            "relations": [
                {"from_entity": "e-user", "to_entity": "e-order",
                 "cardinality": "one_to_many", "label": "places"},
            ],
        }, "er")
        assert "erDiagram" in src and "ORDER {" in src
        assert "int order_id PK" in src
        assert "USER ||--o{ ORDER : places" in src
        assert mermaid_problems(src, "er") == []

    def test_rebuild_returns_none_when_data_insufficient(self):
        from devflow.mermaid_fix import rebuild_mermaid_source

        assert rebuild_mermaid_source({"nodes": []}, "flowchart") is None
        assert rebuild_mermaid_source({"entities": [{"e_id": "x"}]}, "er") is None
        assert rebuild_mermaid_source({}, "unknown") is None

    @pytest.mark.asyncio
    async def test_graph_generate_rebuilds_stub_mermaid(self, monkeypatch):
        """集成：LLM 返回结构化数据完整但 mermaid_source 是空壳 → 自动重建。"""
        import devflow.nodes.graph_gen as gg

        async def fake(**kwargs):
            return {
                "nodes": [
                    {"node_id": "n-1", "label": "提交订单", "node_type": "io",
                     "code_ref": None, "is_modified": True},
                    {"node_id": "n-2", "label": "校验库存", "node_type": "module",
                     "code_ref": None, "is_modified": True},
                ],
                "edges": [
                    {"edge_id": "e-1", "from_node": "n-1", "to_node": "n-2",
                     "edge_type": "call", "condition": "库存充足", "is_modified": True},
                ],
                "mermaid_source": "flowchart TD",  # 空壳（网关截断形态）
            }

        monkeypatch.setattr(gg, "invoke_json", fake)
        state = {"graph_type": "flowchart",
                 "requirement": {"project_context": "商城下单支付"},
                 "code_context": []}
        out = await gg.graph_generate_async(state)  # type: ignore[arg-type]
        lg = out["logic_graph"]
        assert lg is not None and "提交订单" in lg["mermaid_source"]
        assert "n-1 -->" in lg["mermaid_source"]
        assert out["last_error"] is None
