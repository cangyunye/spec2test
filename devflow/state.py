"""LangGraph 全局状态（GlobalState）定义。

严格对齐 SPEC 3.3 章节：所有环节间信息传递靠结构化字段，不靠原始对话。
"""
from __future__ import annotations

from typing import Annotated, Any, Literal, Optional, TypedDict

from langgraph.graph.message import add_messages


# ═══════════════════════════════════════════════════════════════════
# TypedDict 子结构 —— 字段与 schemas.py 保持一致
# ═══════════════════════════════════════════════════════════════════

class RequirementSchema(TypedDict, total=False):
    req_type: Literal["new_feature", "component_iteration", "bug_fix"]
    project_root: str
    project_context: str
    target_modules: list[str]
    existing_code_accessible: bool
    reference_files: list[str]
    io_constraints: dict[str, str]      # {"input": ..., "output": ...}
    edge_cases: list[str]
    acceptance_criteria: list[str]


class CodeRef(TypedDict, total=False):
    file_path: str
    symbol: Optional[str]
    line_start: int
    line_end: int


class LogicNode(TypedDict, total=False):
    node_id: str
    label: str
    node_type: Literal["function", "module", "condition", "io", "external"]
    code_ref: Optional[CodeRef]
    is_modified: bool
    input_spec: Optional[dict[str, Any]]
    output_spec: Optional[dict[str, Any]]


class LogicEdge(TypedDict, total=False):
    edge_id: str
    from_node: str
    to_node: str
    edge_type: Literal["call", "data_flow", "condition"]
    condition: Optional[str]
    is_modified: bool


class LogicGraph(TypedDict, total=False):
    graph_id: str
    nodes: list[LogicNode]
    edges: list[LogicEdge]
    mermaid_source: str


class OpenCodeSessions(TypedDict, total=False):
    search: Optional[str]
    code_gen: Optional[str]
    test_gen: Optional[str]


class CodeChange(TypedDict, total=False):
    file_path: str
    action: Literal["created", "modified", "deleted"]
    diff: str
    lint_passed: bool
    test_passed: Optional[bool]


StageType = Literal[
    "clarify", "search", "graph", "code", "test", "review", "done"
]


class GlobalState(TypedDict, total=False):
    """LangGraph 全局状态黑板（Single Source of Truth）。"""

    # ── 1. 对话层（热记忆，只保留最近 N 轮） ──────────────
    messages: Annotated[list, add_messages]

    # ── 2. 结构化业务数据（温记忆） ─────────────────────
    requirement: RequirementSchema
    code_context: list[dict[str, Any]]       # 阶段二才会有
    logic_graph: Optional[LogicGraph]
    code_changes: list[CodeChange]           # 阶段三才会有
    test_report: Optional[dict[str, Any]]    # 阶段三才会有

    # ── 3. 子系统会话映射 ──────────────────────────────
    opencode_sessions: OpenCodeSessions

    # ── 4. 流程控制字段 ────────────────────────────────
    current_stage: StageType
    missing_fields: list[str]                # 校验节点输出的缺失字段/错误
    last_error: Optional[str]
    retry_count: dict[str, int]              # e.g. {"clarify_validate": 1}
