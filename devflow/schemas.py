"""JSON Schema 定义：用于校验需求清单、逻辑图的结构化输出。

LLM 输出必须严格符合这些 Schema；程序读取也依赖这些字段。
"""
from __future__ import annotations

import json
import uuid
from typing import Any

import jsonschema


# ═══════════════════════════════════════════════════════════════════
# 1. 需求信息完备性清单 Schema
#
# 两种模式（由 has_project_code 判定）：
#   代码模式（提供了项目代码）：project_root / target_modules 必填，走完整链路
#   仅需求模式（不提供代码）  ：两者选填，澄清后直接生成端到端测试用例
# ═══════════════════════════════════════════════════════════════════
REQUIRED_REQ_FIELDS = [
    "req_type",
    "project_context",
    "existing_code_accessible",
    "io_constraints",
    "edge_cases",
    "acceptance_criteria",
]
# 仅代码模式额外必填（existing_code_accessible=true 时由 allOf/if-then 触发）
CODE_MODE_REQ_FIELDS = ["project_root", "target_modules"]

REQUIREMENT_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "RequirementSchema",
    "type": "object",
    "additionalProperties": False,
    "required": REQUIRED_REQ_FIELDS,
    "allOf": [
        {
            "if": {
                "properties": {"existing_code_accessible": {"const": True}},
                "required": ["existing_code_accessible"],
            },
            "then": {"required": CODE_MODE_REQ_FIELDS},
        }
    ],
    "properties": {
        "req_type": {
            "type": "string",
            "enum": ["new_feature", "component_iteration", "bug_fix"],
            "description": "需求类型：全新功能 / 组件迭代 / 缺陷修复",
        },
        "project_root": {
            "type": "string",
            "description": "代码项目根目录绝对路径；不提供项目代码时留空",
        },
        "project_context": {
            "type": "string",
            "minLength": 5,
            "description": "项目背景简述，比如技术栈、业务场景",
        },
        "target_modules": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "description": "本次需求涉及的模块/目录",
        },
        "existing_code_accessible": {
            "type": "boolean",
            "description": "是否提供现有项目代码；false 时走「仅需求模式」直接生成端到端测试用例",
        },
        "reference_files": {
            "type": "array",
            "items": {"type": "string"},
            "default": [],
            "description": "参考文件/文档路径（选填）",
        },
        "io_constraints": {
            "type": "object",
            "required": ["input", "output"],
            "properties": {
                # 不设 minLength：空串由 validate_requirement 的业务校验给中文提示，
                # 避免 jsonschema 的原始英文报错（'': should be non-empty）混进追问卡片
                "input": {"type": "string"},
                "output": {"type": "string"},
            },
            "description": "输入输出约束",
        },
        "edge_cases": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "description": "边界场景/异常场景清单",
        },
        "acceptance_criteria": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "description": "验收标准（每条可独立验证）",
        },
    },
}

# ═══════════════════════════════════════════════════════════════════
# 2. 可机读逻辑图 Schema
# ═══════════════════════════════════════════════════════════════════
LOGIC_GRAPH_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "LogicGraph",
    "type": "object",
    "additionalProperties": False,
    "required": ["graph_id", "nodes", "edges", "mermaid_source"],
    "properties": {
        "graph_id": {"type": "string", "minLength": 1},
        "graph_type": {"type": "string", "enum": ["flowchart", "sequence", "state", "er"]},
        "nodes": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["node_id", "label", "node_type", "is_modified"],
                "additionalProperties": False,
                "properties": {
                    "node_id": {"type": "string", "pattern": r"^n-[A-Za-z0-9_-]+$"},
                    "label": {"type": "string", "minLength": 1},
                    "node_type": {
                        "type": "string",
                        "enum": ["function", "module", "condition", "io", "external"],
                    },
                    "code_ref": {
                        "type": ["object", "null"],
                        "properties": {
                            "file_path": {"type": "string"},
                            "symbol": {"type": ["string", "null"]},
                            "line_start": {"type": "integer", "minimum": 1},
                            "line_end": {"type": "integer", "minimum": 1},
                        },
                    },
                    "is_modified": {"type": "boolean"},
                    "input_spec": {"type": ["object", "null"]},
                    "output_spec": {"type": ["object", "null"]},
                },
            },
        },
        "edges": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["edge_id", "from_node", "to_node", "edge_type", "is_modified"],
                "additionalProperties": False,
                "properties": {
                    "edge_id": {"type": "string", "pattern": r"^e-[A-Za-z0-9_-]+$"},
                    "from_node": {"type": "string"},
                    "to_node": {"type": "string"},
                    "edge_type": {"type": "string", "enum": ["call", "data_flow", "condition"]},
                    "condition": {"type": ["string", "null"]},
                    "is_modified": {"type": "boolean"},
                },
            },
        },
        "mermaid_source": {"type": "string", "minLength": 5},
    },
}


# ═══════════════════════════════════════════════════════════════════
# 2b. 多种类逻辑图 Schema（sequence / state / er）
#
# 结构化字段按种类分叉；flowchart 沿用上方 LOGIC_GRAPH_SCHEMA。
# nodes/edges 依旧是全种类兜底投影（下游只认这两个字段），
# 因此在种类 Schema 里作为可选字段放行（落盘图 = 原生字段 + 投影）。
# ═══════════════════════════════════════════════════════════════════
# 投影 nodes/edges 与 LOGIC_GRAPH_SCHEMA 的 items 同构，直接引用其定义
_NODES_FIELD: dict[str, Any] = {
    **LOGIC_GRAPH_SCHEMA["properties"]["nodes"], "type": "array",
}
_EDGES_FIELD: dict[str, Any] = {
    **LOGIC_GRAPH_SCHEMA["properties"]["edges"], "type": "array",
}
_BASE_GRAPH_FIELDS: dict[str, Any] = {
    "graph_id": {"type": "string", "minLength": 1},
    "graph_type": {"type": "string", "enum": ["flowchart", "sequence", "state", "er"]},
    "mermaid_source": {"type": "string", "minLength": 5},
}
_PARTICIPANT_ITEM = {
    "type": "object",
    "required": ["alias", "label", "kind", "is_modified"],
    "additionalProperties": False,
    "properties": {
        "alias": {"type": "string", "pattern": r"^[A-Za-z][A-Za-z0-9_]*$"},
        "label": {"type": "string", "minLength": 1},
        "kind": {"type": "string", "enum": ["actor", "service", "external"]},
        "is_modified": {"type": "boolean"},
    },
}
_MESSAGE_ITEM = {
    "type": "object",
    "required": ["msg_id", "from_participant", "to_participant", "label", "kind", "is_modified"],
    "additionalProperties": False,
    "properties": {
        "msg_id": {"type": "string", "pattern": r"^m[A-Za-z0-9_]*$"},
        "from_participant": {"type": "string"},
        "to_participant": {"type": "string"},
        "label": {"type": "string", "minLength": 1},
        "kind": {"type": "string", "enum": ["sync", "async", "return"]},
        "is_modified": {"type": "boolean"},
    },
}
_STATE_ITEM = {
    "type": "object",
    "required": ["state_id", "label", "kind", "is_modified"],
    "additionalProperties": False,
    "properties": {
        "state_id": {"type": "string", "pattern": r"^s[A-Za-z0-9_]*$"},
        "label": {"type": "string", "minLength": 1},
        "kind": {"type": "string", "enum": ["initial", "final", "normal"]},
        "is_modified": {"type": "boolean"},
    },
}
_TRANSITION_ITEM = {
    "type": "object",
    "required": ["trans_id", "from_state", "to_state", "is_modified"],
    "additionalProperties": False,
    "properties": {
        "trans_id": {"type": "string", "pattern": r"^t[A-Za-z0-9_]*$"},
        "from_state": {"type": "string"},
        "to_state": {"type": "string"},
        "event": {"type": ["string", "null"]},
        "is_modified": {"type": "boolean"},
    },
}
_ENTITY_ITEM = {
    "type": "object",
    "required": ["e_id", "table", "attributes", "is_modified"],
    "additionalProperties": False,
    "properties": {
        "e_id": {"type": "string", "pattern": r"^n-[A-Za-z0-9_-]+$"},
        "table": {"type": "string", "pattern": r"^[A-Za-z][A-Za-z0-9_]*$"},
        "attributes": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["name", "type"],
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string", "pattern": r"^[A-Za-z][A-Za-z0-9_]*$"},
                    "type": {"type": "string", "minLength": 1},
                    "is_pk": {"type": "boolean"},
                },
            },
        },
        "is_modified": {"type": "boolean"},
    },
}
_RELATION_ITEM = {
    "type": "object",
    "required": ["rel_id", "from_entity", "to_entity", "cardinality", "label", "is_modified"],
    "additionalProperties": False,
    "properties": {
        "rel_id": {"type": "string", "pattern": r"^r[A-Za-z0-9_]*$"},
        "from_entity": {"type": "string"},
        "to_entity": {"type": "string"},
        "cardinality": {
            "type": "string",
            "enum": ["one_to_one", "one_to_many", "many_to_one", "many_to_many"],
        },
        "label": {"type": "string", "minLength": 1},
        "is_modified": {"type": "boolean"},
    },
}

SEQUENCE_GRAPH_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "SequenceGraph",
    "type": "object",
    "additionalProperties": False,
    "required": ["graph_id", "graph_type", "participants", "messages", "mermaid_source"],
    "properties": {
        **_BASE_GRAPH_FIELDS,
        "participants": {"type": "array", "minItems": 2, "items": _PARTICIPANT_ITEM},
        "messages": {"type": "array", "minItems": 2, "items": _MESSAGE_ITEM},
        # 兜底投影（落盘后存在，生成时不要求）
        "nodes": _NODES_FIELD,
        "edges": _EDGES_FIELD,
    },
}

STATE_GRAPH_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "StateGraphData",
    "type": "object",
    "additionalProperties": False,
    "required": ["graph_id", "graph_type", "states", "transitions", "mermaid_source"],
    "properties": {
        **_BASE_GRAPH_FIELDS,
        "states": {"type": "array", "minItems": 2, "items": _STATE_ITEM},
        "transitions": {"type": "array", "minItems": 1, "items": _TRANSITION_ITEM},
        "nodes": _NODES_FIELD,
        "edges": _EDGES_FIELD,
    },
}

ER_GRAPH_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "ErGraph",
    "type": "object",
    "additionalProperties": False,
    "required": ["graph_id", "graph_type", "entities", "relations", "mermaid_source"],
    "properties": {
        **_BASE_GRAPH_FIELDS,
        "entities": {"type": "array", "minItems": 2, "items": _ENTITY_ITEM},
        "relations": {"type": "array", "minItems": 1, "items": _RELATION_ITEM},
        "nodes": _NODES_FIELD,
        "edges": _EDGES_FIELD,
    },
}

# 种类 → (schema, 引用完整性检查配置)：[引用字段] → 被引用集合的字段
_TYPED_GRAPH_SCHEMAS: dict[str, tuple[dict[str, Any], list[tuple[str, str, str]]]] = {
    # (容器字段, 引用字段, 被引用 id 字段)
    "sequence": (SEQUENCE_GRAPH_SCHEMA, [("messages", "from_participant", "alias"),
                                         ("messages", "to_participant", "alias")]),
    "state": (STATE_GRAPH_SCHEMA, [("transitions", "from_state", "state_id"),
                                   ("transitions", "to_state", "state_id")]),
    "er": (ER_GRAPH_SCHEMA, [("relations", "from_entity", "e_id"),
                             ("relations", "to_entity", "e_id")]),
}


# ═══════════════════════════════════════════════════════════════════
# 3. 校验工具函数
# ═══════════════════════════════════════════════════════════════════
def has_project_code(requirement: dict[str, Any] | None) -> bool:
    """流程分支开关：本次需求是否携带可检索/可落盘的项目代码。

    满足任一即视为代码模式：
      - 用户声明 existing_code_accessible=true
      - requirement 里给了 project_root（如 Web 配置面板 / CLI --set 直接填了路径）
    都不满足 → 仅需求模式：跳过代码检索/生成，直接基于需求+逻辑图产出端到端测试用例。
    """
    req = requirement or {}
    if bool(req.get("existing_code_accessible")):
        return True
    return bool(str(req.get("project_root") or "").strip())


def validate_requirement(data: dict[str, Any]) -> list[str]:
    """校验需求清单是否合规，返回缺失字段/错误列表。

    project_root / target_modules 只在代码模式（has_project_code）下必填；
    仅需求模式不强制提供项目代码，需求本身完备即可直接进入制图与用例设计。
    """
    errors: list[str] = []
    try:
        jsonschema.validate(data, REQUIREMENT_SCHEMA)
    except jsonschema.ValidationError as e:
        errors.append(f"{'.'.join(str(p) for p in e.path)}: {e.message}")
    # 额外业务校验：必填项必须有实质内容（防止空字符串 / 空数组蒙混过关）
    if not data.get("edge_cases"):
        errors.append("edge_cases: 至少列出 1 个边界场景")
    if not data.get("acceptance_criteria"):
        errors.append("acceptance_criteria: 至少定义 1 条验收标准")
    if not (data.get("project_context") or "").strip():
        errors.append("project_context: 必须填写项目背景简述")
    io = data.get("io_constraints") or {}
    if not (io.get("input") or "").strip():
        errors.append("io_constraints.input: 必须填写输入约束")
    if not (io.get("output") or "").strip():
        errors.append("io_constraints.output: 必须填写输出约束")
    if has_project_code(data):
        if not (data.get("project_root") or "").strip():
            errors.append(
                "project_root: 提供了项目代码就必须给出可访问的根目录路径"
                "（不提供代码请明确说明，可直接按需求生成测试用例）"
            )
        if not data.get("target_modules"):
            errors.append("target_modules: 至少指定 1 个涉及模块")
    return errors


def validate_logic_graph(data: dict[str, Any]) -> list[str]:
    """校验逻辑图是否合规：按 graph_type 分派对应 Schema + 引用完整性检查。

    flowchart（含旧数据无 graph_type 字段）→ LOGIC_GRAPH_SCHEMA + nodes/edges 拓扑；
    sequence / state / er → 各自 Schema + 引用字段 ⊆ 实体 id 集合。
    """
    errors: list[str] = []
    graph_type = str(data.get("graph_type") or "flowchart")
    if graph_type != "flowchart":
        spec = _TYPED_GRAPH_SCHEMAS.get(graph_type)
        if spec is None:
            return [f"graph_type: 未知种类 {graph_type!r}"]
        schema, ref_checks = spec
        try:
            jsonschema.validate(data, schema)
        except jsonschema.ValidationError as e:
            return [f"{'/'.join(str(p) for p in e.path)}: {e.message}"]
        for container, ref_field, id_field in ref_checks:
            entity_key = {"messages": "participants", "transitions": "states",
                          "relations": "entities"}[container]
            known = {str(i.get(id_field)) for i in data.get(entity_key, []) if isinstance(i, dict)}
            for item in data.get(container, []):
                ref = str(item.get(ref_field))
                if ref not in known:
                    errors.append(
                        f"{container}[{item.get(next(k for k in item if k.endswith('_id')), '?')}]"
                        f" 的 {ref_field}={ref} 不存在"
                    )
        return errors

    try:
        jsonschema.validate(data, LOGIC_GRAPH_SCHEMA)
    except jsonschema.ValidationError as e:
        errors.append(f"{'/'.join(str(p) for p in e.path)}: {e.message}")
        return errors

    node_ids = {n["node_id"] for n in data["nodes"]}
    for edge in data["edges"]:
        if edge["from_node"] not in node_ids:
            errors.append(f"边 {edge['edge_id']} 的 from_node={edge['from_node']} 不存在")
        if edge["to_node"] not in node_ids:
            errors.append(f"边 {edge['edge_id']} 的 to_node={edge['to_node']} 不存在")

    # MVP 阶段：如果 existing_code_accessible=True，则至少有 1 个节点绑 code_ref
    # （阶段一没接 OpenCode，可能暂时没 code_ref，所以不强求；这里只记录 warning）
    return errors


# ═══════════════════════════════════════════════════════════════════
# 4. 初始空值构造
# ═══════════════════════════════════════════════════════════════════
def empty_requirement() -> dict[str, Any]:
    """返回一个全为空 / 默认值的需求模板，字段与 Schema 对齐。"""
    return {
        "req_type": "new_feature",
        "project_root": "",
        "project_context": "",
        "target_modules": [],
        "existing_code_accessible": False,
        "reference_files": [],
        "io_constraints": {"input": "", "output": ""},
        "edge_cases": [],
        "acceptance_criteria": [],
    }


def new_graph_id() -> str:
    return f"graph-{uuid.uuid4().hex[:8]}"
