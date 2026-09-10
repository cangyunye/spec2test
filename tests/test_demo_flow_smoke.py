"""演示模式（Mock LLM）基本流程冒烟：全新会话第一条真实消息 → 澄清 → 需求确认 → 制图。

这是「基本流程跑不通」的直接回归位。历史缺陷：需求抽取提示词里出现「制图」等词时，
mock 按关键词猜任务类型，把需求抽取误判成制图请求（返回 nodes/edges/mermaid_source），
需求永远抽不满 → 反复返回通用澄清问题。此前的测试全部 monkeypatch invoke_json 或
预填 requirement 启动，没有任何用例覆盖「全新会话 + 第一条真实消息」这条最基本路径。
"""
from __future__ import annotations

import uuid

from langchain_core.messages import HumanMessage
from langgraph.types import Command

from devflow.orchestrator import build_graph, initial_state


def _smoke(graph, text: str):
    config = {"configurable": {"thread_id": f"smoke-{uuid.uuid4().hex[:8]}"}}
    state = initial_state() | {"messages": [HumanMessage(content=text)]}
    list(graph.stream(state, config, stream_mode="updates"))
    return config


def test_first_message_extracts_requirement_and_reaches_review_gate():
    """第一条消息：抽取结果必须是「需求」而非制图模板，并停在需求确认门禁。"""
    graph = build_graph()
    config = _smoke(graph, "给商城下单支付流程加库存校验、15 分钟超时取消与支付状态流转")

    snap = graph.get_state(config)
    assert "requirement_review" in (snap.next or []), (
        f"基本流程应停在需求确认门禁，实际 next={snap.next}，"
        f"missing={snap.values.get('missing_fields')}"
    )
    req = snap.values.get("requirement") or {}
    # 抽取产物是需求 schema，而不是 mock 制图模板（历史症状：混入 nodes/edges）
    assert "nodes" not in req and "edges" not in req and "mermaid_source" not in req
    assert req.get("project_context"), f"project_context 未抽取到: {req}"


def test_confirm_then_type_gate_then_graph_generates():
    """确认需求 → 选图种类 → mock 制图成功到 END：基本链路全程无异常。"""
    graph = build_graph()
    config = _smoke(graph, "开发一个桌面计算器，支持四则运算与除零报错提示")
    snap = graph.get_state(config)
    assert "requirement_review" in (snap.next or [])

    list(graph.stream(Command(resume="confirm"), config, stream_mode="updates"))
    assert "graph_type_select" in (graph.get_state(config).next or [])

    list(graph.stream(Command(resume="flowchart"), config, stream_mode="updates"))
    done = graph.get_state(config)
    assert done.next == (), f"制图后流程应结束，实际 next={done.next}"
    assert done.values.get("logic_graph"), "应产出逻辑图"
    graph_obj = done.values["logic_graph"]
    assert graph_obj.get("nodes") and graph_obj.get("edges")
