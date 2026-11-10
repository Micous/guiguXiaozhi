"""engine 共享的小工具（E6 拆分产物）—— engine 与 engine_phases 共用。"""

from __future__ import annotations

from typing import Any

from xiaozhi.core.message import Role


class _TurnView:
    """只读的模型输出视图 —— 用来在"检测"和"下发"之间传递不同的文本。"""

    __slots__ = ("text", "tool_calls")

    def __init__(self, text: str, tool_calls: Any) -> None:
        self.text = text
        self.tool_calls = tool_calls


def _empty_turn():
    """空响应兜底"""
    from xiaozhi.models.ollama import ChatTurn

    return ChatTurn()


def _last_assistant_text(history) -> str:
    """取历史里最后一条 assistant 回复的正文（用于上下文相关的意图判定）"""
    for message in reversed(history or []):
        if getattr(message, "role", None) == Role.ASSISTANT:
            return str(getattr(message, "content", "") or "")
    return ""
