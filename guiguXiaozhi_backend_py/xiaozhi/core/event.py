"""运行时事件定义 —— Agent 流程的"步骤视图"。

对齐参考项目 mini_harness/runtime/events.py。事件流回答"发生了什么"，
span 回答"花了多久"，两者合起来就是完整的跟踪流程。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict


class EventType(str, Enum):
    """事件类型"""

    AGENT_START = "agent_start"
    TURN_START = "turn_start"
    RETRIEVAL = "retrieval"
    TOOL_EXECUTE = "tool_execute"
    TOOL_RESULT = "tool_result"
    TEXT_RESPONSE = "text_response"
    TURN_END = "turn_end"
    AGENT_END = "agent_end"
    DEGRADED = "degraded"
    ERROR = "error"


@dataclass
class Event:
    """事件基类"""

    event_type: EventType
    trace_id: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_type": self.event_type.value,
            "trace_id": self.trace_id,
            "timestamp": self.timestamp.isoformat(),
            "metadata": self.metadata,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, default=str)

    def to_sse(self) -> str:
        """SSE 单帧（``data: {json}\\n\\n``）—— 供 /xiaozhi/chat?format=events 使用。

        默认裸文本流保持前端零改动；events 通道让新前端/调试工具
        能还原完整执行过程（检索、工具调用、降级、纠正重试…）。
        """
        return f"data: {self.to_json()}\n\n"


@dataclass
class AgentStartEvent(Event):
    event_type: EventType = EventType.AGENT_START


@dataclass
class TurnStartEvent(Event):
    event_type: EventType = EventType.TURN_START


@dataclass
class RetrievalEvent(Event):
    event_type: EventType = EventType.RETRIEVAL


@dataclass
class ToolExecuteEvent(Event):
    event_type: EventType = EventType.TOOL_EXECUTE


@dataclass
class ToolResultEvent(Event):
    event_type: EventType = EventType.TOOL_RESULT


@dataclass
class TextResponseEvent(Event):
    event_type: EventType = EventType.TEXT_RESPONSE


@dataclass
class TurnEndEvent(Event):
    event_type: EventType = EventType.TURN_END


@dataclass
class AgentEndEvent(Event):
    event_type: EventType = EventType.AGENT_END


@dataclass
class DegradedEvent(Event):
    """降级事件：某个依赖不可用时走了兜底路径"""

    event_type: EventType = EventType.DEGRADED


@dataclass
class ErrorEvent(Event):
    event_type: EventType = EventType.ERROR
