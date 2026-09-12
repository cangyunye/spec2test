"""LangGraph 全局状态（GlobalState）定义。

严格对齐 SPEC 3.3 章节：所有环节间信息传递靠结构化字段，不靠原始对话。
"""
from __future__ import annotations

from typing import Annotated, Any, Literal, Optional, TypedDict

from langgraph.graph.message import add_messages

# requirement_sources 取值：字段值来自用户原话 / 模型提炼推断 / mock 兜底编造
SOURCE_USER = "user"
SOURCE_INFERRED = "inferred"
# LLM 全挂时 mock 兜底填入的演示数据：不是真实抽取结果，后续真实抽取必须能覆盖它，
# 且到需求确认门禁时强制人工确认（requirement_review 视同 inferred）
SOURCE_MOCK = "mock"


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


class SequenceParticipant(TypedDict, total=False):
    alias: str                 # mermaid participant 别名（ASCII）
    label: str
    kind: Literal["actor", "service", "external"]
    is_modified: bool


class SequenceMessage(TypedDict, total=False):
    msg_id: str
    from_participant: str      # 对端 alias
    to_participant: str
    label: str
    kind: Literal["sync", "async", "return"]
    is_modified: bool


class StateNode(TypedDict, total=False):
    state_id: str
    label: str
    kind: Literal["initial", "final", "normal"]
    is_modified: bool


class StateTransition(TypedDict, total=False):
    trans_id: str
    from_state: str
    to_state: str
    event: Optional[str]       # 触发事件 / 条件
    is_modified: bool


class ErAttribute(TypedDict, total=False):
    name: str
    type: str
    is_pk: bool


class ErEntity(TypedDict, total=False):
    e_id: str
    table: str
    attributes: list[ErAttribute]
    is_modified: bool


class ErRelation(TypedDict, total=False):
    rel_id: str
    from_entity: str
    to_entity: str
    cardinality: Literal["one_to_one", "one_to_many", "many_to_one", "many_to_many"]
    label: str
    is_modified: bool


class JourneySection(TypedDict, total=False):
    section_id: str
    label: str                 # 旅程阶段名（如「下单支付」）
    is_modified: bool


class JourneyTask(TypedDict, total=False):
    task_id: str
    label: str                 # 用户动作 / 触点
    score: int                 # 满意度 0~9（mermaid journey 按分数段画表情）
    actors: list[str]          # 参与角色
    section_id: Optional[str]  # 归属分组；null = 不归组
    is_modified: bool


class LogicGraph(TypedDict, total=False):
    graph_id: str
    graph_type: str            # flowchart（默认）/ sequence / state / er / journey
    nodes: list[LogicNode]     # 全种类兜底投影（下游 code_gen/test_gen/review 只认 nodes/edges）
    edges: list[LogicEdge]
    mermaid_source: str
    # ── 各种类的原生结构化字段（flowchart 用 nodes/edges 本体） ──
    participants: list[SequenceParticipant]
    messages: list[SequenceMessage]
    states: list[StateNode]
    transitions: list[StateTransition]
    entities: list[ErEntity]
    relations: list[ErRelation]
    sections: list[JourneySection]
    tasks: list[JourneyTask]


class Feature(TypedDict, total=False):
    """测试设计 feature 拆分项（feature_split 节点产出）。

    每条验收标准/边界场景必须归属到某个 feature；无归属的由拆分覆盖自检
    归入 F0「综合与集成」功能点，不允许凭空丢失。
    """

    feature_id: str             # F1, F2...（F0 保留给综合与集成）
    name: str
    description: str
    target_modules: list[str]
    acceptance_criteria: list[str]
    edge_cases: list[str]
    node_ids: list[str]         # 对应 LogicNode.node_id（代码模式聚类产物）
    is_modified: bool


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
    code_apply: Optional[dict[str, Any]]     # diff 落盘结果 {applied, files, backup_dir, reason}
    checklist_route: Optional[dict[str, Any]]   # 清单路由结果 {root, candidates, status?, business_count?, decision?, selected?}；门禁恢复重放数据源
    checklist_context: Optional[dict[str, Any]]  # 确认后加载的清单 {root, checklists: [{rel_dir, name, content}]}，注入测试设计
    checklist_routed: bool                   # 本会话已做过路由（用例回炉重生成时不重复弹门禁）
    adopted_cases: Optional[list[str]]       # 用户在测试卡勾选采纳的用例（采纳 = 评审通过）；None = 未做采纳
    distill_dismissed: bool                  # 用户对「沉淀建议卡」点了暂不（本会话不再提示）
    features: list[Feature]                  # feature 拆分结果（feature_split 节点产出；空 = 未拆分/单次整单）
    feature_questions: list[dict[str, Any]]  # 拆分阶段待确认问题（feature_gate 门禁承接用）

    # ── 3. 子系统会话映射 ──────────────────────────────
    opencode_sessions: OpenCodeSessions

    # ── 4. 流程控制字段 ────────────────────────────────
    session_title: str                        # 会话名称：澄清阶段从主要功能生成（LLM 起名，失败退 project_context 截断）
    current_stage: StageType
    graph_type: Optional[str]                # 制图前门禁选定：flowchart/sequence/state/er/journey
    clarify_mode: Literal["normal", "brainstorm", "grill"]   # 澄清模式：普通列表 / 头脑风暴 / 拷问
    clarify_mode_prompt: bool                # 首轮追问后弹「头脑风暴 / 拷问」选择卡（一次即收）
    missing_fields: list[str]                # 校验节点输出的缺失字段/错误
    clarify_round_user_chars: int            # 本轮用户输入长度（观测 + 静默失败识别）
    clarify_round_no_progress: bool          # 本轮有输入（≥30字）却零抽取：抽取静默失败
    requirement_confirmed: bool              # 制图前需求确认门禁是否已通过（抽取到新信息时重置）
    requirement_sources: dict[str, str]      # 需求字段来源：field → "user"（用户原话）/ "inferred"（AI 推断）/ "mock"（兜底编造）
    review_feedback: Optional[str]           # 门禁 reject 时用户填写的修改意见
    test_failure: Optional[str]              # test_run 失败摘要，code_gen 修复时拼进 instruction
    last_error: Optional[str]
    # 注意：这三个键必须声明在 TypedDict 里——LangGraph 只写入 schema 内的键，
    # 未声明键会被节点返回值里静默丢弃（曾导致图级重试路由与失败可见化前缀失效）
    last_error_code: Optional[str]
    last_error_retryable: Optional[bool]
    retry_count: dict[str, int]              # e.g. {"clarify_validate": 1}
    dead_letters: list                       # SPEC 5.7 死信队列（dead_letter_drain 落盘前暂存）
