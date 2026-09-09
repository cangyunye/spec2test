"""langgraph stream 解包 → 事件流（CLI 与 Web/SSE 共用的唯一事件源）。

输入两种形态：
  1. stream_mode="updates"（CLI 旧路径）：逐项产出单键 dict {'node': update}；
     旧版 0.2x 为 (node, update) 元组；中断时 {'__interrupt__': (Interrupt,)}
  2. stream_mode=["updates", "messages"]（Web 路径）：逐项产出 (mode, data) 元组

输出：事件 dict，type ∈
  stage / question / messages / artifact / error      —— 与旧版一致
  mode_choice {}                                      —— 澄清首轮追问后弹「头脑风暴 / 拷问」选择卡
  node_done   {node, label}                           —— 节点完成（含友好名）
  token       {node, content}                         —— LLM 增量输出（messages 模式）
  gate        {gate, payload}                         —— 门禁中断
"""
from __future__ import annotations

from typing import Any, Iterator

# 多流模式下 (mode, data) 元组的 mode 白名单；节点名不会与之冲突
_STREAM_MODES = {"updates", "messages", "values", "debug"}

# 节点名 → 中文友好名（事件与前端进度展示共用）
NODE_LABELS: dict[str, str] = {
    "compress_messages": "整理上下文",
    "clarify_extract": "需求澄清",
    "clarify_validate": "需求校验",
    "clarify_build_question": "生成追问",
    "graph_type_select": "选择图种类",
    "graph_generate": "逻辑制图",
    "graph_review": "制图评审",
    "code_search": "代码检索",
    "graph_render": "图表渲染",
    "code_gen": "代码生成",
    "apply_code": "代码落盘",
    "checklist_route_match": "清单路由",
    "checklist_route_gate": "清单确认",
    "test_gen": "测试设计",
    "test_run": "测试执行",
    "review": "人工验收",
    "dead_letter_drain": "错误归档",
}


def node_label(name: str) -> str:
    return NODE_LABELS.get(name, name)


def events_from_stream(stream: Iterator[Any]) -> Iterator[dict[str, Any]]:
    """把 stream 逐项解包为事件。同步版；兼容单模式与多模式产出。"""
    for item in stream:
        # ── 多流模式：(mode, data) 元组 ──────────────────
        if isinstance(item, tuple) and len(item) == 2 and item[0] in _STREAM_MODES:
            mode, data = item
            yield from _events_for_mode(mode, data)
            continue
        # ── 单流模式：dict 或 (node, update) ─────────────
        if isinstance(item, dict):
            if "__interrupt__" in item:
                yield _gate_event(item["__interrupt__"])
                continue
            node_name = next(iter(item.keys()), "")
            update = item.get(node_name)
        else:
            node_name = item[0]
            update = item[1]
        yield from _events_for_update(node_name, update)


async def events_from_astream(stream: Any) -> Any:
    """异步版：与 events_from_stream 同协议。"""
    async for item in stream:
        if isinstance(item, tuple) and len(item) == 2 and item[0] in _STREAM_MODES:
            mode, data = item
            for ev in _events_for_mode(mode, data):
                yield ev
            continue
        if isinstance(item, dict):
            if "__interrupt__" in item:
                yield _gate_event(item["__interrupt__"])
                continue
            node_name = next(iter(item.keys()), "")
            update = item.get(node_name)
        else:
            node_name = item[0]
            update = item[1]
        for ev in _events_for_update(node_name, update):
            yield ev


def _events_for_mode(mode: str, data: Any) -> Iterator[dict[str, Any]]:
    """多流模式下按 mode 分发。"""
    if mode == "messages":
        chunk, meta = data if isinstance(data, tuple) and len(data) == 2 else (data, {})
        mtype = getattr(chunk, "type", "")
        content = getattr(chunk, "content", "")
        if mtype == "AIMessageChunk" and isinstance(content, str) and content:
            node = (meta or {}).get("langgraph_node", "") if isinstance(meta, dict) else ""
            yield {"type": "token", "node": node, "label": node_label(node), "content": content}
        return
    if mode == "updates" and isinstance(data, dict):
        if "__interrupt__" in data:
            yield _gate_event(data["__interrupt__"])
            return
        for node_name, update in data.items():
            yield from _events_for_update(node_name, update)
    # values / debug 模式暂不消费


def _events_for_update(node_name: str, update: Any) -> Iterator[dict[str, Any]]:
    """单节点 update dict → 事件序列。"""
    if node_name and node_name != "__interrupt__":
        yield {"type": "node_done", "node": node_name, "label": node_label(node_name)}
    if not isinstance(update, dict):
        return  # 节点无状态更新（如 compress_messages → None）
    for key, val in update.items():
        if key == "current_stage" and val:
            yield {"type": "stage", "stage": val}
        elif key == "missing_fields" and val:
            yield {"type": "question", "missing": val}
        elif key == "questions" and val:
            yield {"type": "question", "questions": val}
        elif key == "clarify_mode_prompt" and val:
            # 普通澄清首轮追问后弹「头脑风暴 / 拷问」选择卡（一次即收）
            yield {"type": "mode_choice"}
        elif key == "messages" and isinstance(val, list) and val:
            yield {"type": "messages", "messages": _serialize_messages(val)}
        elif key == "logic_graph" and val:
            yield {"type": "artifact", "kind": "logic_graph", "payload": val}
        elif key == "code_context" and val:
            yield {"type": "artifact", "kind": "code_context", "payload": val}
        elif key == "code_changes" and val:
            yield {"type": "artifact", "kind": "code_changes", "payload": val}
        elif key == "test_report" and val:
            yield {"type": "artifact", "kind": "test_report", "payload": val}
        elif key == "last_error" and val:
            err = val if isinstance(val, str) else str(val)
            yield {"type": "error", "error": err}


def _gate_event(raw: Any) -> dict[str, Any]:
    """'__interrupt__' 值 → gate 事件。raw 形如 (Interrupt(value=...),)。"""
    intr = raw[0] if isinstance(raw, (tuple, list)) and raw else raw
    payload = getattr(intr, "value", None) or intr
    ptype = payload.get("type") if isinstance(payload, dict) else None
    return {"type": "gate", "gate": ptype or "review", "payload": payload}

def _serialize_messages(val: list[Any]) -> list[dict[str, Any]]:
    """BaseMessage 列表 → 可 JSON 序列化的 {type, content}。"""
    out = []
    for m in val:
        if isinstance(m, dict):
            out.append({"type": m.get("type", "message"), "content": str(m.get("content", ""))})
            continue
        out.append({"type": getattr(m, "type", "message"), "content": str(getattr(m, "content", ""))})
    return out
