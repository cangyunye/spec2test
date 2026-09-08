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
                "input": {"type": "string", "minLength": 1},
                "output": {"type": "string", "minLength": 1},
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
    """校验逻辑图是否合规，并附加图拓扑一致性检查。"""
    errors: list[str] = []
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
