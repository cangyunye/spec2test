"""压缩节点：实施「结构化提取优先 + 对话尾部截断」策略。

SPEC 1.6 首选策略：
- 关键结论全部沉淀为 State 中的结构化字段（requirement / logic_graph / ...）
- 原始对话只保留最近 HOT_MEMORY_LAST_N 轮，历史归档不参与后续推理
- 可选：用 LLM 把旧对话总结 1 段摘要，追加到下一轮 system prompt

目前 MVP 实现【尾部截断 + 结构化字段常驻】，不做 LLM 摘要（后续可加）。
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from ..config import settings
from ..state import GlobalState


def compress_messages(state: GlobalState) -> dict[str, Any]:
    """截断 messages 到最近 N 轮（每轮=1 user + 1 assistant 合计）。

    结构化字段（requirement 等）永远保留，不受压缩影响。
    """
    messages = state.get("messages") or []
    if len(messages) <= settings.HOT_MEMORY_LAST_N * 2:
        return {}  # 无需压缩

    # 保留最后 N*2 条消息（按消息条数近似每轮 user/assistant 各 1）
    trimmed = list(messages[-settings.HOT_MEMORY_LAST_N * 2:])
    return {"messages": trimmed}
