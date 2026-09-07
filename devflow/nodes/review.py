"""人工验收节点：使用 LangGraph interrupt 暂停流程，等待用户 approve/reject。

SPEC 3.2 工作流图最后一步：「人工验收节点 ← 人工中断 (Interrupt)」。
SPEC 3.4 节点输入输出契约表：人工验收读取 code_changes, test_report, logic_graph，
等待人工输入 approve/reject，reject → 回代码生成阶段。

resume 值兼容两种形态：
  - "approve" / "reject"（CLI 旧路径）
  - {"decision": ..., "comment": "修改意见"}（Web 带意见回传）
reject 的意见写入 state.review_feedback，code_gen 重做时针对性修正。

LangGraph interrupt 机制：
  - interrupt(value) 会暂停 graph 执行，把 value 返回给调用方
  - 调用方用 Command(resume=<user_input>) 恢复执行
  - 恢复后 interrupt() 调用点返回 resume 值
"""
from __future__ import annotations

from typing import Any

from langgraph.types import interrupt

from ..state import GlobalState
from .graph_review import _parse_decision


def review_node(state: GlobalState) -> dict[str, Any]:
    """人工验收节点。

    读取：code_changes, test_report, logic_graph
    写入：current_stage="done"（approve）或 current_stage="code"（reject）
    """
    code_changes = state.get("code_changes") or []
    test_report = state.get("test_report") or {}
    logic_graph = state.get("logic_graph") or {}

    # 组装给用户看的验收摘要
    summary_parts: list[str] = []
    summary_parts.append(f"逻辑图: graph_id={logic_graph.get('graph_id', '?')}")

    nodes = logic_graph.get("nodes", [])
    modified = [n for n in nodes if n.get("is_modified")]
    summary_parts.append(f"  节点 {len(nodes)} 个（修改 {len(modified)} 个）")

    summary_parts.append(f"代码变更: {len(code_changes)} 个文件")
    for ch in code_changes:
        lint_str = "lint ✓" if ch.get("lint_passed") else "lint ✗"
        test_str = "test ✓" if ch.get("test_passed") else "test ?"
        summary_parts.append(f"  - {ch.get('file_path', '?')} [{ch.get('action', '?')}] {lint_str} {test_str}")

    run = test_report.get("run") or {}
    if run:
        summary_parts.append(
            f"测试: passed={run.get('passed', 0)} failed={run.get('failed', 0)} "
            f"coverage={run.get('coverage_pct', 0)}%"
        )

    cases = test_report.get("test_cases") or []
    review_payload = {
        "type": "human_review",
        "summary": "\n".join(summary_parts),
        "code_changes_count": len(code_changes),
        "test_passed": run.get("failed", 0) == 0 and run.get("passed", 0) > 0,
        "logic_graph_id": logic_graph.get("graph_id"),
        # 结构化视图（Web 渲染用）
        "code_changes": [
            {
                "file_path": ch.get("file_path"),
                "action": ch.get("action"),
                "lint_passed": bool(ch.get("lint_passed")),
                "test_passed": ch.get("test_passed"),
            }
            for ch in code_changes
        ],
        "test_summary": {
            "passed": run.get("passed", 0),
            "failed": run.get("failed", 0),
            "coverage_pct": run.get("coverage_pct", 0),
            "case_count": len(cases),
        },
    }

    # interrupt 暂停 graph；调用方恢复时传 "approve" / "reject"（或带 comment 的 dict）
    decision, comment = _parse_decision(interrupt(review_payload))

    if decision == "approve":
        return {
            "current_stage": "done",
            "review_feedback": None,
            "last_error": None,
            "last_error_code": None,
        }
    # reject → 回到代码生成阶段重做；意见随行
    feedback = comment or "验收未通过（未填写具体意见），请重点自查边界场景与验收标准"
    return {
        "current_stage": "code",
        "review_feedback": feedback,
        "last_error": "[review] 用户拒绝验收，回退到代码生成",
        "last_error_code": None,
    }


def route_after_review(state: GlobalState) -> str:
    """review 节点后的路由：approve → END；reject → code_gen。"""
    if state.get("current_stage") == "done":
        return "approved"
    return "rejected"
