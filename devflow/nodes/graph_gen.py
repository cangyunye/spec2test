"""逻辑图节点组（按图种类分叉）：
  1. graph_generate   —— LLM 从需求 + 代码上下文生成可机读逻辑图
                        （种类由 state.graph_type 决定：flowchart / sequence / state / er）
  2. graph_validate   —— 程序校验逻辑图格式 & 拓扑/引用

所有种类都会把原生结构投影成 nodes/edges 兜底视图（下游 code_gen / test_gen /
review 只认这两个字段）；flowchart 的 nodes/edges 是本体，其余种类是投影。
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from pydantic import BaseModel, Field

from ..graph_types import normalize_graph_type
from ..llm_client import invoke_json
from ..mermaid_fix import (
    is_stub_mermaid,
    mermaid_problems,
    rebuild_mermaid_source,
    sanitize_mermaid,
)
from ..schemas import new_graph_id, validate_logic_graph
from ..state import GlobalState


# ── Pydantic 结构化输出模型（与 schemas.py 各种类 Schema 对齐） ───────
class _CodeRef(BaseModel):
    file_path: str
    symbol: str | None = None
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)


class _LogicNode(BaseModel):
    node_id: str = Field(pattern=r"^n-[A-Za-z0-9_-]+$")
    label: str = Field(min_length=1)
    node_type: str = Field(pattern=r"^(function|module|condition|io|external)$")
    code_ref: _CodeRef | str | None = None
    is_modified: bool
    input_spec: dict[str, Any] | None = None
    output_spec: dict[str, Any] | None = None


class _LogicEdge(BaseModel):
    edge_id: str = Field(pattern=r"^e-[A-Za-z0-9_-]+$")
    from_node: str
    to_node: str
    edge_type: str = Field(pattern=r"^(call|data_flow|condition)$")
    condition: str | None = None
    is_modified: bool


class LogicGraphModel(BaseModel):
    """LLM 生成的可机读逻辑图（flowchart）。"""
    nodes: list[_LogicNode]
    edges: list[_LogicEdge]
    mermaid_source: str = Field(min_length=5, description="完整的 Mermaid flowchart TD 源码")


class _Participant(BaseModel):
    alias: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]*$", description="参与者别名，ASCII")
    label: str = Field(min_length=1)
    kind: str = Field(pattern=r"^(actor|service|external)$")
    is_modified: bool


class _Message(BaseModel):
    msg_id: str = Field(pattern=r"^m[A-Za-z0-9_]*$")
    from_participant: str
    to_participant: str
    label: str = Field(min_length=1)
    kind: str = Field(pattern=r"^(sync|async|return)$")
    is_modified: bool


class SequenceGraphModel(BaseModel):
    """LLM 生成的可机读时序图。"""
    participants: list[_Participant] = Field(min_length=2)
    messages: list[_Message] = Field(min_length=2)
    mermaid_source: str = Field(min_length=5, description="完整的 Mermaid sequenceDiagram 源码")


class _StateNode(BaseModel):
    state_id: str = Field(pattern=r"^s[A-Za-z0-9_]*$")
    label: str = Field(min_length=1)
    kind: str = Field(pattern=r"^(initial|final|normal)$")
    is_modified: bool


class _Transition(BaseModel):
    trans_id: str = Field(pattern=r"^t[A-Za-z0-9_]*$")
    from_state: str
    to_state: str
    event: str | None = None
    is_modified: bool


class StateGraphModel(BaseModel):
    """LLM 生成的可机读状态图。"""
    states: list[_StateNode] = Field(min_length=2)
    transitions: list[_Transition] = Field(min_length=1)
    mermaid_source: str = Field(min_length=5, description="完整的 Mermaid stateDiagram-v2 源码")


class _Attribute(BaseModel):
    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]*$")
    type: str = Field(min_length=1)
    is_pk: bool = False


class _Entity(BaseModel):
    e_id: str = Field(pattern=r"^n-[A-Za-z0-9_-]+$")
    table: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]*$")
    attributes: list[_Attribute] = Field(min_length=1)
    is_modified: bool


class _Relation(BaseModel):
    rel_id: str = Field(pattern=r"^r[A-Za-z0-9_]*$")
    from_entity: str
    to_entity: str
    cardinality: str = Field(pattern=r"^(one_to_one|one_to_many|many_to_one|many_to_many)$")
    label: str = Field(min_length=1)
    is_modified: bool


class ErGraphModel(BaseModel):
    """LLM 生成的可机读 ER 图。"""
    entities: list[_Entity] = Field(min_length=2)
    relations: list[_Relation] = Field(min_length=1)
    mermaid_source: str = Field(min_length=5, description="完整的 Mermaid erDiagram 源码")


# ── 各种类系统提示词 ───────────────────────────────────────────
_FLOWCHART_RULES = """核心原则：
1. 图 = 程序可遍历的结构化数据，不是给人看的装饰图。所有节点 node_id 必须以 n- 开头，所有边 edge_id 以 e- 开头。
2. 节点的 is_modified / 边的 is_modified 必须准确标记：本次需求会改动到的节点和边标 true，完全复用的标 false。
3. 节点类型（node_type）严格五选一：
   - function  —— 具体函数 / 方法
   - module    —— 模块 / 文件 / 类
   - condition —— 条件分支节点（如 if/else，对应的下游边要带 condition 描述）
   - io        —— 输入 / 输出边界（HTTP 入口、DB 写入、外部 API 调用）
   - external  —— 第三方依赖 / 外部系统
4. 边类型（edge_type）三选一：
   - call      —— 函数 / 模块调用关系
   - data_flow —— 数据流 / 参数传递
   - condition —— 从 condition 节点出发的条件分支（每条边必须填 condition 字段）
5. 代码引用 code_ref：阶段一没接 OpenCode 时，如果你能从需求文本推断出文件路径，就填；推断不出来就填 null。
   阶段二会由程序回填 code_ref。
6. Mermaid 源码语法硬规则（违反会导致渲染失败）：
   - 第一行必须是 `flowchart TD`，不要用 ``` 代码块包裹输出；
   - 节点标签里含括号/引号/冒号/分号/竖线等特殊字符时，必须用双引号包裹整个标签：
     正确 `n-1["表单校验(含必填项)"]`，错误 `n-1[表单校验(含必填项)]`；
   - 边条件文字同理：`n-2 -->|"已支付(含税费)"| n-3`；
   - 修改高亮：先用 `classDef modified fill:#f96,stroke:#333,stroke-width:2px;` 定义，
     再用 `class n-2,n-3 modified;` 标记；禁止引用未定义的 class；
   - 禁止用 end 作节点 id；每条语句独占一行；标签内不要出现换行。
7. 逻辑图必须覆盖：输入 → 核心处理流程（含所有分支）→ 输出。
8. 节点数量建议 3~8 个（MVP），复杂项目后续再扩展。
9. 最终输出必须是一个合法 JSON 对象，仅包含 nodes / edges / mermaid_source 三个字段；
   不要输出 Markdown 代码块、不要加任何解释文字，直接输出 JSON。
"""

SEQUENCE_PROMPT = """你是一个资深软件架构师，擅长把需求拆解为可机读的时序图（sequenceDiagram）。
核心原则：
1. 图 = 程序可遍历的结构化数据。参与者 alias 必须是 ASCII 标识（如 FE / API / DB），label 可中文；
   消息 msg_id 以 m 开头（如 m1、m2）。
2. is_modified 必须准确标记：本次需求会改动到的参与者与消息标 true，完全复用的标 false。
3. 参与者 kind 三选一：
   - actor    —— 人类用户 / 前端入口
   - service  —— 本系统内的服务 / 模块
   - external —— 第三方依赖 / 外部系统
4. 消息 kind 三选一（与 mermaid 箭头一一对应）：
   - sync   —— 同步调用，箭头 ->>
   - async  —— 异步 / 消息投递，箭头 -)
   - return —— 返回 / 响应，箭头 -->>
5. Mermaid 源码语法硬规则（违反会导致渲染失败）：
   - 第一行必须是 `sequenceDiagram`，不要用 ``` 代码块包裹输出；
   - 参与者声明：`participant API as 后端服务`；人类用户用 `actor USER as 操作用户`；
   - 消息：`FE->>API: 提交表单`（sync）、`API-)MQ: 发布事件`（async）、`API-->>FE: 返回结果`（return）；
   - 消息文字独占一行，不要出现换行，不要用分号；
   - 分支场景可用 alt/else/end 块，但必须保证 alt / else / end 三者配对完整；不用 activate/deactivate；
   - 每条语句独占一行。
6. 时序必须覆盖：发起方 → 核心交互（含关键分支与异常返回）→ 最终响应。
7. 参与者 2~6 个，消息 4~14 条。
8. 最终输出必须是一个合法 JSON 对象，仅包含 participants / messages / mermaid_source 三个字段；
   不要输出 Markdown 代码块、不要加任何解释文字，直接输出 JSON。
"""

STATE_PROMPT = """你是一个资深软件架构师，擅长把需求拆解为可机读的状态图（stateDiagram-v2）。
核心原则：
1. 图 = 程序可遍历的结构化数据。状态 state_id 以 s 开头（如 s1、s_pending，纯 ASCII，不用连字符）；
   迁移 trans_id 以 t 开头（如 t1、t2）。
2. is_modified 必须准确标记：本次需求新增/改动的状态与迁移标 true，复用的标 false。
3. 状态 kind 三选一：
   - initial —— 初始态（整个状态机的唯一入口，mermaid 里写 `[*] --> 状态id`）
   - final   —— 终态（mermaid 里写 `状态id --> [*]`）
   - normal  —— 普通中间态
4. Mermaid 源码语法硬规则（违反会导致渲染失败）：
   - 第一行必须是 `stateDiagram-v2`，不要用 ``` 代码块包裹输出；
   - 转移：`s1 --> s2: 事件/条件`（事件文字可中文，独占一行，不要出现换行与分号）；
   - 初始态：`[*] --> s1`；终态：`s3 --> [*]`；
   - 状态显示名：`s1 : 待支付`（冒号后跟中文名）；
   - 状态 id 不要用连字符；禁止复合状态嵌套；每条语句独占一行。
5. 必须覆盖：初始态 → 主要流转（含异常 / 回退路径）→ 终态。
6. 状态 3~8 个，迁移 3~12 条。
7. 最终输出必须是一个合法 JSON 对象，仅包含 states / transitions / mermaid_source 三个字段；
   不要输出 Markdown 代码块、不要加任何解释文字，直接输出 JSON。
"""

ER_PROMPT = """你是一个资深数据架构师，擅长把需求拆解为可机读的 ER 图（erDiagram）。
核心原则：
1. 图 = 程序可遍历的结构化数据。实体表名用大写 ASCII（如 CUSTOMER），e_id 以 n- 开头；
   关系 rel_id 以 r 开头（如 r1、r2）。
2. is_modified 必须准确标记：本次需求新增/改动的实体与关系标 true，复用的标 false。
3. 实体属性：name 用小写 snake_case ASCII；type 用 mermaid 支持的标识
   （string / int / long / decimal / date / datetime / bool）；主键 is_pk=true。
4. 关系 cardinality 四选一（与 mermaid 记号一一对应）：
   - one_to_one   → `||--||`
   - one_to_many  → `||--o{`
   - many_to_one  → `}o--||`
   - many_to_many → `}o--o{`
5. Mermaid 源码语法硬规则（违反会导致渲染失败）：
   - 第一行必须是 `erDiagram`，不要用 ``` 代码块包裹输出；
   - 关系：`CUSTOMER ||--o{ ORDER : places`（关系动词用小写英文单词或 snake_case）；
   - 属性块：实体名后跟花括号块，每行一个属性 `string name`，主键加 PK：
     CUSTOMER {
       string name
       int customer_id PK
     }
   - 属性不要加引号/括号/默认值；每条语句独占一行。
6. 实体 2~6 个，关系 1~8 条；必须覆盖需求涉及的核心数据模型。
7. 最终输出必须是一个合法 JSON 对象，仅包含 entities / relations / mermaid_source 三个字段；
   不要输出 Markdown 代码块、不要加任何解释文字，直接输出 JSON。
"""

SYSTEM_PROMPTS: dict[str, str] = {
    "flowchart": "你是一个资深软件架构师，擅长把需求拆解为可机读的逻辑图（flowchart）。\n" + _FLOWCHART_RULES,
    "sequence": SEQUENCE_PROMPT,
    "state": STATE_PROMPT,
    "er": ER_PROMPT,
}

RESPONSE_MODELS: dict[str, type[BaseModel]] = {
    "flowchart": LogicGraphModel,
    "sequence": SequenceGraphModel,
    "state": StateGraphModel,
    "er": ErGraphModel,
}


USER_PROMPT_TEMPLATE = """
# 需求清单（已结构化）
{requirement_json}

# 代码上下文（阶段一可能为空数组，不用强依赖）
{code_context_json}
{feedback_section}
# 制图要求
- 按系统指令规定的图种类与规模，生成一份可机读逻辑图
- 所有 is_modified 标记要与需求的「新增/修改」部分对应
- 最后同时输出 Mermaid 源码
"""

FEEDBACK_SECTION_TEMPLATE = """
# 上轮评审意见（用户 reject 本图后给出的修改要求，本轮必须针对性修正）
{review_feedback}
"""


def graph_generate(state: GlobalState) -> dict[str, Any]:
    """同步入口（LangGraph .invoke 使用）：内部用 asyncio.run 跑 async 实现。"""
    return asyncio.run(graph_generate_async(state))


async def graph_generate_async(state: GlobalState) -> dict[str, Any]:
    """异步入口：按 state.graph_type 生成可机读逻辑图。

    读取：requirement, code_context, graph_type
    写入：logic_graph, last_error, last_error_code, last_error_retryable, current_stage
    """
    from ..errors import DevFlowError, wrap_exception

    graph_type = normalize_graph_type(state.get("graph_type"))
    req = state.get("requirement") or {}
    code_ctx = state.get("code_context") or []
    feedback = (state.get("review_feedback") or "").strip()
    feedback_section = (
        FEEDBACK_SECTION_TEMPLATE.format(review_feedback=feedback) if feedback else ""
    )

    try:
        result = await invoke_json(
            system_prompt=SYSTEM_PROMPTS[graph_type],
            user_prompt=USER_PROMPT_TEMPLATE.format(
                requirement_json=json.dumps(req, ensure_ascii=False, indent=2),
                code_context_json=json.dumps(code_ctx, ensure_ascii=False, indent=2),
                feedback_section=feedback_section,
            ),
            response_model=RESPONSE_MODELS[graph_type],
            response_type="logic_graph",
        )
    except DevFlowError as e:
        retry = state.get("retry_count", {}) or {}
        retry["graph_generate"] = retry.get("graph_generate", 0) + 1
        return {
            "last_error": f"[graph_generate:{e.code}] {e.message}",
            "last_error_code": e.code,
            "last_error_retryable": e.retryable,
            "retry_count": retry,
        }
    except Exception as e:
        err = wrap_exception(e, context="graph_generate")
        retry = state.get("retry_count", {}) or {}
        retry["graph_generate"] = retry.get("graph_generate", 0) + 1
        return {
            "last_error": f"[graph_generate:{err.code}] {err.message}",
            "last_error_code": err.code,
            "last_error_retryable": err.retryable,
            "retry_count": retry,
        }

    # invoke_json 返回的 result 可能是纯 dict（mock 路径）或含 Pydantic 模型
    def _dump(item: Any) -> dict[str, Any]:
        if hasattr(item, "model_dump"):
            return item.model_dump(mode="json")
        return dict(item)

    # Mermaid 语法自检修复：剥代码块围栏、字面量 \n、（flowchart）特殊字符标签补引号、
    # 缺声明行自动补齐（详见 mermaid_fix 模块 docstring）
    mermaid_source = sanitize_mermaid(str(result["mermaid_source"] or ""), graph_type)
    # 网关在 function calling 下偶发把 mermaid_source 截断成只剩声明行的空壳，
    # 而同响应的结构化数据（nodes/edges/participants/…）完整独立——用结构化数据
    # 确定性重建，保证渲染图与结构一致且必定可渲染
    if is_stub_mermaid(mermaid_source, graph_type):
        rebuilt = rebuild_mermaid_source(_dump(result), graph_type)
        if rebuilt:
            import logging
            logging.getLogger(__name__).warning(
                "mermaid_source 为空壳（疑似网关截断），已从结构化数据重建"
            )
            mermaid_source = rebuilt
    residual = mermaid_problems(mermaid_source, graph_type)
    if residual:
        # 修不干净的问题仅记录，不阻断流程（前端有源码视图兜底）
        import logging
        logging.getLogger(__name__).warning(
            "mermaid 自检仍有问题（已尽力修复）: %s", "; ".join(residual)
        )

    logic_graph: dict[str, Any] = {
        "graph_id": new_graph_id(),
        "graph_type": graph_type,
        "mermaid_source": mermaid_source,
    }
    if graph_type == "flowchart":
        nodes = []
        for n in result["nodes"]:
            d = _dump(n)
            # D1b 防御：模型偶发把 code_ref 输出成字符串 → 规范化为对象
            d["code_ref"] = _normalize_code_ref(d.get("code_ref"))
            nodes.append(d)
        logic_graph["nodes"] = nodes
        logic_graph["edges"] = [_dump(e) for e in result["edges"]]
    elif graph_type == "sequence":
        logic_graph["participants"] = [_dump(p) for p in result["participants"]]
        logic_graph["messages"] = [_dump(m) for m in result["messages"]]
        _project_sequence(logic_graph)
    elif graph_type == "state":
        logic_graph["states"] = [_dump(s) for s in result["states"]]
        logic_graph["transitions"] = [_dump(t) for t in result["transitions"]]
        _project_state(logic_graph)
    elif graph_type == "er":
        logic_graph["entities"] = [_dump(e) for e in result["entities"]]
        logic_graph["relations"] = [_dump(r) for r in result["relations"]]
        _project_er(logic_graph)
    else:  # 防御：注册表与分派不同步时兜底报错
        return {
            "logic_graph": None,
            "last_error": f"graph_type: 未知种类 {graph_type!r}",
            "last_error_code": None,
            "last_error_retryable": None,
            "missing_fields": [f"graph_type: 未知种类 {graph_type!r}"],
            "current_stage": "graph",
        }

    # 先做一次校验；不通过的话 missing_fields（这里复用为错误列表）会有值
    errors = validate_logic_graph(logic_graph)
    if errors:
        return {
            "logic_graph": None,
            "last_error": "; ".join(errors),
            "last_error_code": None,
            "last_error_retryable": None,
            "missing_fields": errors,
            "current_stage": "graph",
        }

    return {
        "logic_graph": logic_graph,
        "last_error": None,
        "last_error_code": None,
        "last_error_retryable": None,
        "missing_fields": [],
        "current_stage": "done",  # MVP：制图完成即结束（后续阶段接 code/test）
    }


# ── 非 flowchart 种类 → nodes/edges 兜底投影 ───────────────────
# 下游（code_gen / test_gen / review / 前端检查器）只认 nodes/edges；
# 投影让所有种类在这条契约上保持可用。id 前缀换算：s1→n-1 / t1→e-1 / m1→e-1 / r1→e-1。

_TO_NODE_ID = lambda pid: "n-" + re.sub(r"^[a-z]", "", str(pid), count=1)  # noqa: E731
_TO_EDGE_ID = lambda tid: "e-" + re.sub(r"^[a-z]", "", str(tid), count=1)  # noqa: E731


def _project_sequence(graph: dict[str, Any]) -> None:
    """participants/messages → nodes/edges（actor→io, service→module, external→external）。"""
    kind_map = {"actor": "io", "service": "module", "external": "external"}
    node_by_alias: dict[str, str] = {}
    nodes: list[dict[str, Any]] = []
    for p in graph["participants"]:
        nid = "n-" + p["alias"].lower()
        node_by_alias[p["alias"]] = nid
        nodes.append({
            "node_id": nid, "label": p["label"],
            "node_type": kind_map.get(p.get("kind"), "module"),
            "code_ref": None, "is_modified": bool(p.get("is_modified")),
            "input_spec": None, "output_spec": None,
        })
    edge_kind = {"sync": "call", "async": "call", "return": "data_flow"}
    edges: list[dict[str, Any]] = []
    for m in graph["messages"]:
        src, dst = node_by_alias.get(m["from_participant"]), node_by_alias.get(m["to_participant"])
        if not src or not dst:
            continue  # 引用不完整时跳过该投影边（校验层会拦截原生数据）
        edges.append({
            "edge_id": _TO_EDGE_ID(m["msg_id"]), "from_node": src, "to_node": dst,
            "edge_type": edge_kind.get(m.get("kind"), "call"),
            "condition": None, "is_modified": bool(m.get("is_modified")),
        })
    graph["nodes"], graph["edges"] = nodes, edges


def _project_state(graph: dict[str, Any]) -> None:
    """states/transitions → nodes/edges（initial/final→io 边界，normal→module）。"""
    kind_map = {"initial": "io", "final": "io", "normal": "module"}
    nodes = [{
        "node_id": _TO_NODE_ID(s["state_id"]), "label": s["label"],
        "node_type": kind_map.get(s.get("kind"), "module"),
        "code_ref": None, "is_modified": bool(s.get("is_modified")),
        "input_spec": None, "output_spec": None,
    } for s in graph["states"]]
    node_ids = {n["node_id"] for n in nodes}
    edges = []
    for t in graph["transitions"]:
        src, dst = _TO_NODE_ID(t["from_state"]), _TO_NODE_ID(t["to_state"])
        if src not in node_ids or dst not in node_ids:
            continue
        edges.append({
            "edge_id": _TO_EDGE_ID(t["trans_id"]), "from_node": src, "to_node": dst,
            "edge_type": "condition", "condition": t.get("event"),
            "is_modified": bool(t.get("is_modified")),
        })
    graph["nodes"], graph["edges"] = nodes, edges


def _project_er(graph: dict[str, Any]) -> None:
    """entities/relations → nodes/edges（实体→module，关系→data_flow）。"""
    nodes = [{
        "node_id": e["e_id"], "label": e["table"], "node_type": "module",
        "code_ref": None, "is_modified": bool(e.get("is_modified")),
        "input_spec": None, "output_spec": None,
    } for e in graph["entities"]]
    node_ids = {n["node_id"] for n in nodes}
    edges = []
    for r in graph["relations"]:
        if r["from_entity"] not in node_ids or r["to_entity"] not in node_ids:
            continue
        edges.append({
            "edge_id": _TO_EDGE_ID(r["rel_id"]),
            "from_node": r["from_entity"], "to_node": r["to_entity"],
            "edge_type": "data_flow", "condition": None,
            "is_modified": bool(r.get("is_modified")),
        })
    graph["nodes"], graph["edges"] = nodes, edges


def graph_validate(state: GlobalState) -> dict[str, Any]:
    """节点：校验逻辑图。返回错误列表到 missing_fields。"""
    g = state.get("logic_graph")
    if g is None:
        return {
            "missing_fields": ["logic_graph: 尚未生成"],
            "current_stage": "graph",
        }
    errors = validate_logic_graph(g)
    return {
        "missing_fields": errors,
        "current_stage": "done" if not errors else "graph",
    }


def _normalize_code_ref(v: Any) -> dict[str, Any] | None:
    """把模型可能输出的字符串 code_ref 规范化为对象形式。

    DeepSeek 等模型偶发把 code_ref 输出成字符串（文件路径），
    与 schema 的对象结构不符；这里统一转成 {"file_path": ...}。
    line_start/line_end 未知 → 省略（schema 中非必填）。
    """
    if v is None:
        return None
    if isinstance(v, str):
        return {"file_path": v, "symbol": None}
    return v
