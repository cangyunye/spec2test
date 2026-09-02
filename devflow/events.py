"""langgraph stream 解包 → 事件流（CLI 与 Web/SSE 共用的唯一事件源）。

输入：graph.stream(..., stream_mode="updates") 的逐项产出
  - langgraph 1.x：单键 dict {'node': update}；中断时 {'__interrupt__': (Interrupt,)}
  - 旧版 0.2x：(node, update) 元组
输出：事件 dict，type ∈ stage / artifact / question / gate / error
"""
from __future__ import annotations

from typing import Any, Iterator


def events_from_stream(stream: Iterator[Any]) -> Iterator[dict[str, Any]]:
    """把 stream 逐项解包为事件。同步版（CLI）；Web 用 events_from_astream 消费同协议。"""
    for item in stream:
        if isinstance(item, dict):
            if "__interrupt__" in item:
                yield _gate_event(item["__interrupt__"])
                continue
            update = next(iter(item.values()))
        else:
            update = item[1]
        if not isinstance(update, dict):
            continue  # 节点无状态更新（如 compress_messages → None）
        for key, val in update.items():
            if key == "current_stage" and val:
                yield {"type": "stage", "stage": val}
            elif key == "missing_fields" and val:
                yield {"type": "question", "missing": val}
            elif key == "questions" and val:
                yield {"type": "question", "questions": val}
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


async def events_from_astream(stream: Any) -> Any:
    """异步版：Web SSE 消费。返回异步生成器，逐事件产出（与 events_from_stream 同协议）。"""
    async for item in stream:
        if isinstance(item, dict):
            if "__interrupt__" in item:
                yield _gate_event(item["__interrupt__"])
                continue
            update = next(iter(item.values()))
        else:
            update = item[1]
        if not isinstance(update, dict):
            continue
        for key, val in update.items():
            if key == "current_stage" and val:
                yield {"type": "stage", "stage": val}
            elif key == "missing_fields" and val:
                yield {"type": "question", "missing": val}
            elif key == "questions" and val:
                yield {"type": "question", "questions": val}
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
    intr = raw[0] if isinstance(raw, (tuple, list)) else raw
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
