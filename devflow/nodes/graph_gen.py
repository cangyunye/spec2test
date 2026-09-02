"""逻辑图节点组：
  1. graph_generate   —— LLM 从需求 + 代码上下文（阶段二才有）生成可机读逻辑图
  2. graph_validate   —— 程序校验逻辑图格式 & 拓扑
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from pydantic import BaseModel, Field

from ..llm_client import invoke_json
from ..schemas import LOGIC_GRAPH_SCHEMA, new_graph_id, validate_logic_graph
from ..state import GlobalState


# ── Pydantic 结构化输出模型（对齐 LOGIC_GRAPH_SCHEMA） ───────
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
    """LLM 生成的可机读逻辑图。"""
    nodes: list[_LogicNode]
    edges: list[_LogicEdge]
    mermaid_source: str = Field(min_length=5, description="完整的 Mermaid flowchart TD 源码")


SYSTEM_PROMPT_GRAPH = """你是一个资深软件架构师，擅长把需求拆解为可机读的逻辑图。
核心原则：
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
6. Mermaid 源码必须是合法的 flowchart TD 语法，能直接渲染；修改部分用:::modified 样式标记（你自己定义 classDef）。
7. 逻辑图必须覆盖：输入 → 核心处理流程（含所有分支）→ 输出。
8. 节点数量建议 3~8 个（MVP），复杂项目后续再扩展。
9. 最终输出必须是一个合法 JSON 对象，仅包含 nodes / edges / mermaid_source 三个字段；
   不要输出 Markdown 代码块、不要加任何解释文字，直接输出 JSON。
"""


USER_PROMPT_TEMPLATE = """
# 需求清单（已结构化）
{requirement_json}

# 代码上下文（阶段一可能为空数组，不用强依赖）
{code_context_json}

# 制图要求
- 生成一份可机读逻辑图，节点 3~8 个
- 所有 is_modified 标记要与需求的「新增/修改」部分对应
- 最后同时输出 Mermaid 源码
"""


def graph_generate(state: GlobalState) -> dict[str, Any]:
    """同步入口（LangGraph .invoke 使用）：内部用 asyncio.run 跑 async 实现。"""
    return asyncio.run(graph_generate_async(state))


async def graph_generate_async(state: GlobalState) -> dict[str, Any]:
    """异步入口：生成可机读逻辑图。

    读取：requirement, code_context
    写入：logic_graph, last_error, last_error_code, last_error_retryable, current_stage
    """
    from ..errors import DevFlowError, wrap_exception

    req = state.get("requirement") or {}
    code_ctx = state.get("code_context") or []

    try:
        result = await invoke_json(
            system_prompt=SYSTEM_PROMPT_GRAPH,
            user_prompt=USER_PROMPT_TEMPLATE.format(
                requirement_json=json.dumps(req, ensure_ascii=False, indent=2),
                code_context_json=json.dumps(code_ctx, ensure_ascii=False, indent=2),
            ),
            response_model=LogicGraphModel,
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

    # 包装成 LogicGraph TypedDict 结构，补 graph_id
    # invoke_json 返回的 result 可能是纯 dict（mock 路径）或含 Pydantic 模型
    def _dump(item: Any) -> dict[str, Any]:
        if hasattr(item, "model_dump"):
            return item.model_dump(mode="json")
        return dict(item)

    nodes = []
    for n in result["nodes"]:
        d = _dump(n)
        # D1b 防御：模型偶发把 code_ref 输出成字符串 → 规范化为对象
        d["code_ref"] = _normalize_code_ref(d.get("code_ref"))
        nodes.append(d)

    logic_graph = {
        "graph_id": new_graph_id(),
        "nodes": nodes,
        "edges": [_dump(e) for e in result["edges"]],
        "mermaid_source": result["mermaid_source"],
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
