"""链路追踪 —— 对齐参考项目 mini_harness/reliability/tracing.py。

一次对话 = 一个 trace_id；对话里的每一步 = 一个 span，span 之间用 parent_span_id
构成树。trace_id / span_id 采用 OpenTelemetry W3C 形态（16 字节 / 8 字节 hex）。
"""

from __future__ import annotations

import contextvars
import secrets
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterator, List, Optional

from xiaozhi.reliability.redaction import sanitize_observability_value

_trace_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "trace_id", default=None
)
_current_span_var: contextvars.ContextVar[Optional["Span"]] = contextvars.ContextVar(
    "current_span", default=None
)


def new_trace_id() -> str:
    """生成 trace_id（OTel 形态，32 位 hex）"""
    return secrets.token_hex(16)


def new_span_id() -> str:
    """生成 span_id（OTel 形态，16 位 hex）"""
    return secrets.token_hex(8)


def get_trace_id() -> str:
    """获取当前 trace_id（没有就新建一个）"""
    trace_id = _trace_id_var.get()
    if not trace_id:
        trace_id = new_trace_id()
        _trace_id_var.set(trace_id)
    return trace_id


def set_trace_id(trace_id: str) -> None:
    """显式设置 trace_id"""
    _trace_id_var.set(trace_id)


def get_current_span() -> Optional["Span"]:
    """获取当前 span"""
    return _current_span_var.get()


# span 结束时的订阅者（TraceStore 通过它收集数据）
_span_sinks: List[Callable[["Span"], None]] = []


def register_span_sink(sink: Callable[["Span"], None]) -> None:
    """注册 span 结束回调"""
    if sink not in _span_sinks:
        _span_sinks.append(sink)


def unregister_span_sink(sink: Callable[["Span"], None]) -> None:
    """注销 span 结束回调"""
    if sink in _span_sinks:
        _span_sinks.remove(sink)


@dataclass
class Span:
    """单个追踪跨度"""

    name: str
    trace_id: str = field(default_factory=get_trace_id)
    parent_span_id: Optional[str] = None
    span_id: str = field(default_factory=new_span_id)
    attributes: Dict[str, Any] = field(default_factory=dict)
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    error: Optional[str] = None
    events: List[Dict[str, Any]] = field(default_factory=list)

    def set_attribute(self, key: str, value: Any) -> None:
        """设置属性（自动脱敏）"""
        self.attributes[key] = sanitize_observability_value(key, value)

    def set_attributes(self, values: Dict[str, Any]) -> None:
        for key, value in values.items():
            self.set_attribute(key, value)

    def add_event(self, event_type: str, **metadata: Any) -> None:
        """在 span 上挂一个子事件（用于记录流式增量等）"""
        self.events.append(
            {
                "event_type": event_type,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "metadata": {
                    k: sanitize_observability_value(k, v) for k, v in metadata.items()
                },
            }
        )

    def set_error(self, error_message: str) -> None:
        """标记 span 为错误"""
        self.error = sanitize_observability_value("error", error_message)
        self.set_attribute("error", True)

    @property
    def duration_ms(self) -> float:
        if self.start_time is None or self.end_time is None:
            return 0.0
        return (self.end_time - self.start_time) * 1000

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "name": self.name,
            "duration_ms": round(self.duration_ms, 2),
            "attributes": self.attributes,
            "events": self.events,
            "error": self.error,
            "start_time": (
                datetime.fromtimestamp(self.start_time).isoformat()
                if self.start_time
                else None
            ),
            "end_time": (
                datetime.fromtimestamp(self.end_time).isoformat()
                if self.end_time
                else None
            ),
        }

    # ---------- 上下文管理器 ----------

    def __enter__(self) -> "Span":
        self.start_time = time.time()
        _current_span_var.set(self)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        self.end_time = time.time()
        if exc_type is not None:
            self.set_error(str(exc_val) or str(exc_type.__name__))
        _current_span_var.set(None)
        for sink in list(_span_sinks):
            try:
                sink(self)
            except Exception:  # 追踪失败绝不影响主流程
                pass
        return False


@contextmanager
def trace_span(
    name: str,
    trace_id: Optional[str] = None,
    parent_id: Optional[str] = None,
) -> Iterator[Span]:
    """追踪跨度上下文管理器

    用法::

        with trace_span("tool.execute") as span:
            span.set_attribute("tool_name", "预约挂号")
            ...
    """
    current = _current_span_var.get()
    if parent_id is None and current is not None:
        parent_id = current.span_id

    span = Span(name=name, trace_id=trace_id or get_trace_id(), parent_span_id=parent_id)
    with span:
        yield span
