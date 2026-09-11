"""步骤级回退重跑（LangGraph time-travel 入口层）。

LangGraph 每个节点完成都会落一条 checkpoint 且历史全量保留（SqliteSaver），
本模块只是把这一现成能力暴露出来：

  - list_steps   —— get_state_history → 前端可展示的回退锚点（checkpoint_id + 中文标签）
  - revert       —— 回到"某节点刚完成"的时点：清空下游字段 → 可选就地改需求 →
                   update_state(目标 checkpoint, as_node=目标节点) 开新分支 →
                   返回续跑 config（graph.stream(None, config) 即重跑下游）

语义约定：
  - 锚点粒度 = "节点 N 的写入已落盘"（历史分支保留，可再反悔回更早分支）
  - 回退到 turn 末尾（next 为空）时续跑立即结束，用户发新消息走正常新一轮
  - messages 无需手工截断：新分支的 channel values 继承目标 checkpoint，
    目标点之后新增的消息只存在于被放弃的旧分支
  - 外部副作用：回退跨过 apply_code 时，用最近一次落盘的 backup_dir 还原
    项目文件（code_apply.rollback）；更早轮次的备份链不在 state 里，不追溯
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from devflow import code_apply
from devflow.events import NODE_LABELS

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
# 节点全序与产出字段表
# ═══════════════════════════════════════════════════════════════════

# 全链路节点全序（与 build_graph_with_providers 拓扑一致；abort 侧的
# dead_letter_drain 不参与下游清理排序，仅作为锚点展示）
NODE_ORDER: tuple[str, ...] = (
    "compress_messages",
    "clarify_extract",
    "clarify_validate",
    "clarify_build_question",
    "requirement_review",
    "graph_type_select",
    "graph_generate",
    "graph_review",
    "code_search",
    "graph_render",
    "code_gen",
    "apply_code",
    "checklist_route_match",
    "checklist_route_gate",
    "test_gen",
    "test_run",
    "review",
)

# 各节点写入的业务字段（messages / current_stage / retry_count / last_error*
# 不在此表，回退时统一特判）。requirement / session_title / clarify_mode 是
# 跨轮累积量，重跑 clarify_extract 会增量合并，不在清理范围。
NODE_FIELDS: dict[str, tuple[str, ...]] = {
    "clarify_extract": ("requirement_confirmed", "clarify_mode_prompt",
                        "clarify_round_user_chars", "clarify_round_no_progress"),
    "clarify_validate": ("missing_fields",),
    "requirement_review": ("requirement_confirmed",),
    "graph_type_select": ("graph_type",),
    "graph_generate": ("logic_graph",),
    "graph_review": ("review_feedback",),
    "code_search": ("code_context",),
    "graph_render": ("logic_graph",),          # 渲染后端回填 mermaid，同一字段
    "code_gen": ("code_changes", "test_failure"),
    "apply_code": ("code_apply",),
    "checklist_route_match": ("checklist_route", "checklist_routed"),
    "checklist_route_gate": ("checklist_context",),
    "test_gen": ("test_report",),
    "test_run": ("test_report", "test_failure"),
    "review": ("review_feedback",),
}

# 清理字段时的空值（与各节点写入类型对齐）
CLEAR_VALUES: dict[str, Any] = {
    "requirement_confirmed": False,
    "clarify_mode_prompt": False,
    "clarify_round_user_chars": 0,
    "clarify_round_no_progress": False,
    "missing_fields": [],
    "graph_type": None,
    "logic_graph": None,
    "review_feedback": None,
    "code_context": [],
    "code_changes": [],
    "code_apply": None,
    "test_failure": None,
    "checklist_route": None,
    "checklist_routed": False,
    "checklist_context": None,
    "test_report": None,
}

# 回退后 current_stage 的提示值（节点重跑时会自行覆盖，这里只求不显示过期阶段）
STAGE_AT: dict[str, str] = {
    "compress_messages": "clarify",
    "clarify_extract": "clarify",
    "clarify_validate": "clarify",
    "clarify_build_question": "clarify",
    "requirement_review": "clarify",
    "graph_type_select": "graph",
    "graph_generate": "graph",
    "graph_review": "graph",
    "code_search": "search",
    "graph_render": "search",
    "code_gen": "code",
    "apply_code": "test",
    "checklist_route_match": "test",
    "checklist_route_gate": "test",
    "test_gen": "test",
    "test_run": "test",
    "review": "review",
}

# retry_count 里非节点名键的归属（clarify_validate 用轮次计数器而非节点名）
RETRY_KEY_NODE: dict[str, str] = {"clarify_loop_cnt": "clarify_validate"}

_APPLY_IDX = NODE_ORDER.index("apply_code")


# ═══════════════════════════════════════════════════════════════════
# 下游清理计算（纯函数）
# ═══════════════════════════════════════════════════════════════════


def _node_index(node: str) -> int:
    try:
        return NODE_ORDER.index(node)
    except ValueError:
        return len(NODE_ORDER)  # 未知节点视为最末端，不做清理


def downstream_fields(node: str) -> tuple[str, ...]:
    """回退到"节点 node 刚完成"时应清空的 state 字段。

    = node 之后节点的产出 − node 及其上游已产出过的字段。同一字段可能两级都写
    （如 logic_graph 由 graph_generate 产出、graph_render 只回填渲染结果），
    此时回退锚点自身的产出必须保留——清掉它，锚点后的首个节点（如 graph_review）
    就没有可评审的图了。
    """
    idx = _node_index(node)
    own: dict[str, None] = {}
    for n in NODE_ORDER[: idx + 1]:
        for f in NODE_FIELDS.get(n, ()):
            own.setdefault(f, None)
    seen: dict[str, None] = {}
    for n in NODE_ORDER[idx + 1:]:
        for f in NODE_FIELDS.get(n, ()):
            if f not in own:
                seen.setdefault(f, None)
    return tuple(seen)


def filter_retry_count(retry: dict[str, int], node: str) -> dict[str, int]:
    """保留目标节点及其上游的重试计数；下游清零，避免重跑提前触发 LOOP_EXHAUSTED 等上限。"""
    idx = _node_index(node)
    out: dict[str, int] = {}
    for key, val in (retry or {}).items():
        owner = RETRY_KEY_NODE.get(key, key)
        if owner in NODE_ORDER and _node_index(owner) > idx:
            continue
        out[key] = val
    return out


def _apply_field_edits(values: dict[str, Any], fields: dict[str, Any]) -> None:
    """把用户就地编辑合并进 requirement（与 CLI --set / 需求确认门禁同一语义：
    io_constraints.input=x 只覆盖子字段；其余键整体覆盖）。"""
    req = dict(values.get("requirement") or {})
    for path, val in (fields or {}).items():
        if "." in path:
            sub, _, leaf = str(path).partition(".")
            req.setdefault(sub, {})
            if isinstance(req[sub], dict):
                req[sub][leaf] = val
            else:
                req[sub] = {leaf: val}
        else:
            req[str(path)] = val
    values["requirement"] = req


# ═══════════════════════════════════════════════════════════════════
# 锚点清单 / 回退
# ═══════════════════════════════════════════════════════════════════


def _config(thread_id: str, checkpoint_id: str | None = None) -> dict[str, Any]:
    # checkpoint_ns 必须显式带上：SqliteSaver.upsert_writes 直接取该键（update_state 落分支用）
    cfg: dict[str, Any] = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    if checkpoint_id:
        cfg["configurable"]["checkpoint_id"] = checkpoint_id
    return cfg


def list_steps(graph: Any, tid: str) -> list[dict[str, Any]]:
    """回退锚点清单（新→旧）。每条 = 一次节点落盘：{checkpoint_id, node, label, ts, next}。

    langgraph 1.x 的 checkpoint metadata 不含 writes，产出节点改由父链推导：
    检查点 C 的产出节点 = 其父检查点的 next[0]（父执行完毕才产生 C）。
    """
    snaps = list(graph.get_state_history(_config(tid)))
    by_id = {s.config["configurable"]["checkpoint_id"]: s for s in snaps}
    out: list[dict[str, Any]] = []
    for snap in snaps:
        node = _producer_of(snap, by_id)
        if node is None:
            continue
        out.append({
            "checkpoint_id": snap.config["configurable"]["checkpoint_id"],
            "node": node,
            "label": NODE_LABELS.get(node, node),
            "ts": getattr(snap, "created_at", None) or "",
            "next": list(snap.next or []),
        })
    return out


def _producer_of(snap: Any, by_id: dict[str, Any]) -> str | None:
    """检查点 snap 的产出节点；非节点产出（input/update）或起点返回 None（不作锚点）。

    父链推导只对 source=="loop" 的检查点成立：input/update 检查点的父 next[0]
    是"即将运行"的节点，不是"已产出"该检查点的节点。
    """
    if (snap.metadata or {}).get("source") != "loop":
        return None
    parent_cfg = getattr(snap, "parent_config", None)
    parent = by_id.get((parent_cfg or {}).get("configurable", {}).get("checkpoint_id", ""))
    if parent is None:
        return None
    nxt = [n for n in (parent.next or ()) if not str(n).startswith("__")]
    if not nxt:
        return None
    node = str(nxt[0])
    return None if node == "__start__" else node


def _anchor_node(graph: Any, tid: str, checkpoint_id: str) -> str:
    """update_state(as_node=) 所需的产出节点。"""
    snaps = list(graph.get_state_history(_config(tid)))
    by_id = {s.config["configurable"]["checkpoint_id"]: s for s in snaps}
    snap = by_id.get(checkpoint_id)
    if snap is None or snap.values is None:
        raise ValueError(f"检查点不存在: {checkpoint_id}")
    node = _producer_of(snap, by_id)
    if node is None:
        raise ValueError("该检查点没有节点产出信息，无法作为回退锚点")
    return node


def _restore_applied_files(tip_values: dict[str, Any], target_idx: int) -> str | None:
    """回退跨过 apply_code 时，把最近一次落盘的文件从备份还原。返回说明或 None。"""
    if target_idx >= _APPLY_IDX:
        return None
    applied = tip_values.get("code_apply") or {}
    if not applied.get("applied"):
        return None
    backup_dir = applied.get("backup_dir")
    root = str((tip_values.get("requirement") or {}).get("project_root") or "")
    if not backup_dir or not root or not Path(backup_dir).is_dir():
        logger.warning("回退跨过落盘点但备份不可用: backup_dir=%r root=%r", backup_dir, root)
        return "落盘备份不可用，项目文件未还原"
    code_apply.rollback(root, backup_dir)
    logger.info("回退还原落盘文件: %s ← %s", root, backup_dir)
    return f"已从备份还原 {len(applied.get('files') or [])} 个落盘文件"


def revert(
    graph: Any,
    tid: str,
    checkpoint_id: str,
    fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """回到 checkpoint_id（某节点刚落盘的时点），开新分支并返回续跑所需信息。

    fields：可选的需求就地编辑（dot-path → 值，语义同需求确认门禁的 fields）。
    返回 {checkpoint_id(新分支), node, next, restored, checkpoint_id_old}；
    续跑：graph.stream(None, {"configurable": {"thread_id": tid,
          "checkpoint_id": result["checkpoint_id"]}}, ...)。
    """
    target_cfg = _config(tid, checkpoint_id)
    try:
        snap = graph.get_state(target_cfg)
    except Exception as e:
        raise ValueError(f"检查点不存在: {checkpoint_id} ({e})") from e
    if snap.values is None:
        raise ValueError(f"检查点不存在: {checkpoint_id}")
    target_node = _anchor_node(graph, tid, checkpoint_id)
    target_idx = _node_index(target_node)

    values = dict(snap.values)
    for f in downstream_fields(target_node):
        values[f] = CLEAR_VALUES.get(f)  # 全部下游字段都在 CLEAR_VALUES；缺省 None 也安全
    values["current_stage"] = STAGE_AT.get(target_node, values.get("current_stage", "clarify"))
    values["last_error"] = None
    values["last_error_code"] = None
    values["last_error_retryable"] = None
    values["retry_count"] = filter_retry_count(values.get("retry_count") or {}, target_node)
    if fields:
        _apply_field_edits(values, fields)

    restored = _restore_applied_files(graph.get_state(_config(tid)).values or {}, target_idx)

    new_cfg = graph.update_state(target_cfg, values, as_node=target_node)
    new_tid = new_cfg["configurable"]["thread_id"]
    new_ck = new_cfg["configurable"]["checkpoint_id"]
    next_nodes = list(graph.get_state(_config(new_tid, new_ck)).next or [])
    return {
        "checkpoint_id": new_ck,
        "checkpoint_id_old": checkpoint_id,
        "node": target_node,
        "label": NODE_LABELS.get(target_node, target_node),
        "next": next_nodes,
        "restored": restored,
    }
