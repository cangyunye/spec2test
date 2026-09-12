"""步骤级回退重跑（timetravel）测试。

纯函数部分：下游字段清理 / 重试计数过滤 / 需求就地编辑。
集成部分：Mock LLM 驱动 build_graph 到门禁→制图→END，验证 revert 开分支、
下游字段清空、messages 截断（旧分支消息不残留）、落盘备份还原。

运行: pytest -v tests/test_timetravel.py
"""
from __future__ import annotations

import uuid
from pathlib import Path

from langchain_core.messages import HumanMessage
from langgraph.types import Command

from devflow import code_apply
from devflow.timetravel import (
    _APPLY_IDX,
    _apply_field_edits,
    _node_index,
    _restore_applied_files,
    downstream_fields,
    filter_retry_count,
    list_steps,
    revert,
)
from devflow.orchestrator import build_graph, initial_state


# ═══════════════════════════════════════════════════════════════════
# 纯函数
# ═══════════════════════════════════════════════════════════════════


def test_downstream_fields_excludes_anchor_own_output():
    """回退到 graph_generate 之后：图保留，其下游（检索/代码/测试）清空。"""
    fields = downstream_fields("graph_generate")
    assert "logic_graph" not in fields
    assert "review_feedback" in fields and "code_context" in fields
    assert "code_changes" in fields and "code_apply" in fields and "test_report" in fields


def test_downstream_fields_before_graph_generation_clears_graph():
    """回退到 graph_type_select 之后：logic_graph 属于下游产出，应清空。"""
    fields = downstream_fields("graph_type_select")
    assert "logic_graph" in fields and "graph_type" not in fields


def test_downstream_fields_of_clarify_keeps_requirement():
    """requirement 是跨轮累积量，任何锚点都不清理。"""
    for node in ("compress_messages", "clarify_extract", "graph_type_select"):
        assert "requirement" not in downstream_fields(node)


def test_filter_retry_count_drops_downstream_only():
    retry = {"clarify_loop_cnt": 2, "graph_generate": 1, "review": 1}
    assert filter_retry_count(retry, "clarify_extract") == {}
    assert filter_retry_count(retry, "graph_type_select") == {"clarify_loop_cnt": 2}
    assert filter_retry_count(retry, "review") == retry


def test_apply_field_edits_dot_path_and_flat():
    values: dict = {"requirement": {"project_context": "x", "io_constraints": {"input": "a"}}}
    _apply_field_edits(values, {
        "io_constraints.input": "b",
        "project_context": "y",
        "target_modules": ["m1"],
    })
    assert values["requirement"]["io_constraints"] == {"input": "b"}
    assert values["requirement"]["project_context"] == "y"
    assert values["requirement"]["target_modules"] == ["m1"]


# ═══════════════════════════════════════════════════════════════════
# 集成：Mock LLM 驱动 MVP 图（澄清 → 门禁 → 制图 → END）
# ═══════════════════════════════════════════════════════════════════


def _new_thread(graph, text: str) -> str:
    tid = f"tt-{uuid.uuid4().hex[:8]}"
    state = initial_state() | {"messages": [HumanMessage(content=text)]}
    list(graph.stream(state, {"configurable": {"thread_id": tid}}, stream_mode="updates"))
    return tid


def _drive_to_graph_done(graph, tid: str) -> None:
    """确认需求 → 选图种类 → mock 制图，推进到 END。"""
    cfg = {"configurable": {"thread_id": tid}}
    assert "requirement_review" in (graph.get_state(cfg).next or [])
    list(graph.stream(Command(resume="confirm"), cfg, stream_mode="updates"))
    assert "graph_type_select" in (graph.get_state(cfg).next or [])
    list(graph.stream(Command(resume="flowchart"), cfg, stream_mode="updates"))
    assert graph.get_state(cfg).next == ()


def test_list_steps_returns_anchor_per_node():
    graph = build_graph()
    tid = _new_thread(graph, "开发一个桌面计算器，支持四则运算与除零报错提示")
    _drive_to_graph_done(graph, tid)

    steps = list_steps(graph, tid)
    nodes = [s["node"] for s in steps]
    assert "clarify_extract" in nodes and "graph_generate" in nodes
    for s in steps:
        assert s["checkpoint_id"] and s["label"] and s["node"]
        assert isinstance(s["message_ids"], list)


def test_list_steps_message_ids_map_bubbles_to_anchors():
    """消息 → 步骤的可回退定位：最早包含某条消息的锚点 = 它刚出现的时点。

    前端据此把气泡下方「回到此处」映射到「这条消息发出前的存档」。
    """
    graph = build_graph()
    tid = _new_thread(graph, "开发一个桌面计算器，支持四则运算与除零报错提示")
    cfg = {"configurable": {"thread_id": tid}}
    vals = graph.get_state(cfg).values
    steps = list_steps(graph, tid)

    ids = {m.id for m in vals["messages"] if getattr(m, "id", None)}
    assert ids, "checkpoint 里的消息应带 id（add_messages 分配）"
    covered = set().union(*(set(s["message_ids"]) for s in steps))
    assert ids <= covered, "每条当前消息都应至少被一个锚点覆盖"

    human_id = next(m.id for m in vals["messages"] if getattr(m, "type", "") == "human")
    containing = [s for s in steps if human_id in s["message_ids"]]  # 新→旧
    assert containing, "用户消息应能在锚点中找到"
    # 最早（列表最末）包含它的锚点 = 本轮 compress_messages；往前一步即「发出前」
    assert containing[-1]["node"] == "compress_messages"
    assert containing[-1] is steps[-1], "首条消息之前没有更早存档（前端据此走回填兜底）"


def test_revert_to_graph_type_select_clears_graph_and_reruns():
    """回退到选完图种类的时点：logic_graph 清空，续跑重新制图。"""
    graph = build_graph()
    tid = _new_thread(graph, "开发一个桌面计算器，支持四则运算与除零报错提示")
    _drive_to_graph_done(graph, tid)
    old_graph = graph.get_state({"configurable": {"thread_id": tid}}).values["logic_graph"]
    assert old_graph

    anchor = next(s for s in list_steps(graph, tid) if s["node"] == "graph_type_select")
    result = revert(graph, tid, anchor["checkpoint_id"])

    snap = graph.get_state({"configurable": {"thread_id": tid}})
    vals = snap.values
    assert vals.get("logic_graph") is None, "回退后旧图应已清空"
    assert vals.get("graph_type") == "flowchart", "锚点节点（选图种类）自身的产出应保留"
    # graph_type_select 是本锚点节点自身，产出不属于下游
    assert result["next"] == ["graph_generate"]
    assert result["checkpoint_id"] != anchor["checkpoint_id"]
    assert result["checkpoint_id_old"] == anchor["checkpoint_id"]

    # 续跑：重新制图到 END，产出新图；旧分支 checkpoint 仍在历史里
    list(graph.stream(None, {"configurable": {"thread_id": tid, "checkpoint_id": result["checkpoint_id"]}},
                      stream_mode="updates"))
    done = graph.get_state({"configurable": {"thread_id": tid}})
    assert done.next == () and done.values.get("logic_graph")
    assert len(list_steps(graph, tid)) > 0
    old_still_there = [
        s for s in list_steps(graph, tid) if s["checkpoint_id"] == anchor["checkpoint_id"]
    ]
    assert old_still_there, "旧分支锚点应保留（可再反悔）"


def test_revert_to_turn_end_truncates_later_messages():
    """回退到第一轮结束（需求确认被驳回 → END）：第二轮的消息不残留，之后可正常发起新一轮。"""
    graph = build_graph()
    tid = _new_thread(graph, "开发一个桌面计算器，支持四则运算与除零报错提示")
    cfg = {"configurable": {"thread_id": tid}}
    assert "requirement_review" in (graph.get_state(cfg).next or [])
    # 驳回 → 本轮 END，停在轮末等用户补充
    list(graph.stream(Command(resume="reject"), cfg, stream_mode="updates"))
    assert graph.get_state(cfg).next == (), "驳回后应停在轮末"
    turn1_msgs = graph.get_state(cfg).values["messages"]

    list(graph.stream({"messages": [HumanMessage(content="补充：还要支持括号与百分号")]},
                      cfg, stream_mode="updates"))
    assert "requirement_review" in (graph.get_state(cfg).next or [])
    turn2_count = len(graph.get_state(cfg).values["messages"])
    assert turn2_count > len(turn1_msgs)

    anchor = next(s for s in list_steps(graph, tid) if s["node"] == "requirement_review")
    result = revert(graph, tid, anchor["checkpoint_id"])

    vals = graph.get_state(cfg).values
    assert len(vals["messages"]) == len(turn1_msgs) < turn2_count, "第二轮消息应被截断"
    assert result["next"] == [], "轮末锚点的 next 应为空（无需续跑）"
    assert vals.get("requirement_confirmed") is False, "驳回态的确认标记应保留为未确认"

    # 截断后正常发起新一轮：补充需求 → 到达需求确认门禁
    list(graph.stream({"messages": [HumanMessage(content="补充：还要支持括号与百分号")]},
                      cfg, stream_mode="updates"))
    assert "requirement_review" in (graph.get_state({"configurable": {"thread_id": tid}}).next or [])


def test_revert_with_field_edits_updates_requirement():
    """回退带 fields：需求就地修改随分支生效，下游重跑读到的是新需求。"""
    graph = build_graph()
    tid = _new_thread(graph, "开发一个桌面计算器，支持四则运算与除零报错提示")
    _drive_to_graph_done(graph, tid)

    anchor = next(s for s in list_steps(graph, tid) if s["node"] == "graph_type_select")
    revert(graph, tid, anchor["checkpoint_id"],
           fields={"project_context": "改后的项目背景", "edge_cases": ["除零"]})

    req = graph.get_state({"configurable": {"thread_id": tid}}).values["requirement"]
    assert req["project_context"] == "改后的项目背景"
    assert req["edge_cases"] == ["除零"]


# ═══════════════════════════════════════════════════════════════════
# 落盘副作用还原
# ═══════════════════════════════════════════════════════════════════


def test_restore_applied_files_rolls_back_across_apply(tmp_path):
    root = tmp_path / "proj"
    (root / "pkg").mkdir(parents=True)
    target = root / "pkg" / "a.py"
    target.write_text("orig\n", encoding="utf-8")

    report = code_apply.apply_patches(
        str(root), [code_apply.patch_from_content("pkg/a.py", "changed\n")], backup=True
    )
    assert target.read_text(encoding="utf-8") == "changed\n"
    tip_values = {"code_apply": report, "requirement": {"project_root": str(root)}}

    # 回退目标在 apply_code 之前 → 自动还原
    msg = _restore_applied_files(tip_values, _node_index("code_gen"))
    assert msg and "还原" in msg
    assert target.read_text(encoding="utf-8") == "orig\n"


def test_restore_skipped_when_anchor_at_or_after_apply(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    target = root / "a.py"
    target.write_text("changed\n", encoding="utf-8")
    tip_values = {
        "code_apply": {"applied": True, "files": [{"path": "a.py"}],
                       "backup_dir": str(tmp_path / "no-such-backup")},
        "requirement": {"project_root": str(root)},
    }
    # 锚点在 apply_code 之后（如 test_gen）：文件保持已落盘状态
    assert _restore_applied_files(tip_values, _node_index("test_gen")) is None
    assert target.read_text(encoding="utf-8") == "changed\n"


def test_apply_index_sanity():
    from devflow.timetravel import NODE_ORDER

    assert _APPLY_IDX == _node_index("apply_code")
    assert _node_index("nonexistent_node") == len(NODE_ORDER)
