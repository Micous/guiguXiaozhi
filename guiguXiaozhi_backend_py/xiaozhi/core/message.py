"""消息类型与统一消息模型。

对齐参考项目 mini_harness/core/message.py 的写法，并补上硅谷小智需要的
工具调用 / 工具结果两种消息形态（LangChain4j 侧的 ChatMessage 家族）。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional


class Role(str, Enum):
    """消息角色"""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class MessageType(str, Enum):
    """消息类型"""

    TEXT = "text"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"


@dataclass
class ToolCall:
    """模型发起的一次工具调用"""

    id: str
    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "name": self.name, "arguments": self.arguments}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ToolCall":
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex),
            name=str(data.get("name") or ""),
            arguments=dict(data.get("arguments") or {}),
        )


@dataclass
class Message:
    """统一消息模型"""

    role: Role
    content: str = ""
    type: MessageType = MessageType.TEXT
    message_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    parent_id: Optional[str] = None
    tool_calls: List[ToolCall] = field(default_factory=list)
    tool_call_id: Optional[str] = None
    tool_name: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)

    # ---------- 构造快捷方法 ----------

    @classmethod
    def system(cls, content: str, **metadata: Any) -> "Message":
        return cls(role=Role.SYSTEM, content=content, metadata=metadata)

    @classmethod
    def user(cls, content: str, **metadata: Any) -> "Message":
        return cls(role=Role.USER, content=content, metadata=metadata)

    @classmethod
    def assistant(
        cls,
        content: str = "",
        tool_calls: Optional[List[ToolCall]] = None,
        **metadata: Any,
    ) -> "Message":
        """构造助手消息；metadata 供落库时打结果标记（booking_executed 等）"""
        return cls(
            role=Role.ASSISTANT,
            content=content,
            type=MessageType.TOOL_CALL if tool_calls else MessageType.TEXT,
            tool_calls=list(tool_calls or []),
            metadata=metadata,
        )

    @classmethod
    def tool_result(
        cls,
        content: str,
        tool_call_id: str,
        tool_name: str,
        *,
        is_error: bool = False,
        **metadata: Any,
    ) -> "Message":
        return cls(
            role=Role.TOOL,
            content=content,
            type=MessageType.TOOL_RESULT,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            metadata={"is_error": is_error, **metadata},
        )

    # ---------- 序列化 ----------

    @property
    def is_error(self) -> bool:
        return bool(self.metadata.get("is_error", False))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "message_id": self.message_id,
            "role": self.role.value,
            "type": self.type.value,
            "content": self.content,
            "parent_id": self.parent_id,
            "tool_calls": [c.to_dict() for c in self.tool_calls],
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "metadata": self.metadata,
            "timestamp": self.timestamp.isoformat(),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, default=str)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Message":
        return cls(
            role=Role(data["role"]),
            content=data.get("content", ""),
            type=MessageType(data.get("type", "text")),
            message_id=data.get("message_id") or uuid.uuid4().hex,
            parent_id=data.get("parent_id"),
            tool_calls=[ToolCall.from_dict(c) for c in data.get("tool_calls", [])],
            tool_call_id=data.get("tool_call_id"),
            tool_name=data.get("tool_name"),
            metadata=dict(data.get("metadata") or {}),
            timestamp=datetime.fromisoformat(data["timestamp"])
            if data.get("timestamp")
            else datetime.now(),
        )

    @classmethod
    def from_dict_safe(cls, data: Any) -> Optional["Message"]:
        """容错反序列化：脏数据返回 ``None`` 而不是抛异常。

        Mongo / 文件里的一条坏消息（未知 role、坏 timestamp、非 dict 结构）
        不该让**整段会话历史**加载失败 —— load 返回 [] 等于会话失忆
        （2026-11-10 审计发现的存储缺陷）。存储层应配合本方法，
        跳过损坏条目并记录数量。
        """
        if not isinstance(data, dict):
            return None
        try:
            role = Role(str(data.get("role") or ""))
        except ValueError:
            return None
        try:
            timestamp = (
                datetime.fromisoformat(data["timestamp"])
                if data.get("timestamp")
                else datetime.now()
            )
            return cls(
                role=role,
                content=str(data.get("content") or ""),
                type=MessageType(data.get("type", "text")),
                message_id=data.get("message_id") or uuid.uuid4().hex,
                parent_id=data.get("parent_id"),
                tool_calls=[
                    ToolCall.from_dict(c)
                    for c in data.get("tool_calls", [])
                    if isinstance(c, dict)
                ],
                tool_call_id=data.get("tool_call_id"),
                tool_name=data.get("tool_name"),
                metadata=dict(data.get("metadata") or {}),
                timestamp=timestamp,
            )
        except (ValueError, TypeError, KeyError):
            return None

    def __str__(self) -> str:  # pragma: no cover - 便于日志排查
        return f"[{self.role.value}/{self.type.value}] {self.content[:120]}"
