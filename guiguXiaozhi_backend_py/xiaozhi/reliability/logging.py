"""结构化日志（JSON 行）+ 脱敏。

对应 PLAN.md「3.4.5 运维要求」。医疗场景里身份证号属于敏感个人信息，
落日志前统一走 ``redaction.py`` 的规则打码。
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from xiaozhi.reliability.redaction import (
    REDACTED,
    SECRET_PATTERNS,
    sanitize_observability_value,
)

_RESERVED = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "taskName", "message", "asctime",
}
_HANDLER_MARKER = "_xiaozhi_json_handler"


def redact_text(text: str) -> str:
    """只做值形态脱敏，不截断长度（日志正文可能较长）"""
    result = text
    for pattern in SECRET_PATTERNS:
        result = pattern.sub(REDACTED, result)
    return result


class JsonFormatter(logging.Formatter):
    """把一条日志渲染成单行 JSON"""

    def format(self, record: logging.LogRecord) -> str:
        message = redact_text(record.getMessage())
        payload: Dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": message,
        }

        extra: Dict[str, Any] = {}
        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_"):
                continue
            extra[key] = sanitize_observability_value(key, value)
        if extra:
            payload["context"] = extra

        if record.exc_info:
            payload["exception"] = redact_text(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(
    level: str = "INFO",
    *,
    json_output: bool = True,
    stream: Optional[Any] = None,
) -> None:
    """安装日志处理器（幂等：重复调用不会叠加 handler）"""
    root = logging.getLogger()
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))

    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_MARKER, False):
            root.removeHandler(handler)

    handler = logging.StreamHandler(stream or sys.stdout)
    setattr(handler, _HANDLER_MARKER, True)
    if json_output:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
        )
    root.addHandler(handler)


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    *,
    trace_id: str = "",
    **fields: Any,
) -> None:
    """写一条带结构化字段的日志（字段会被脱敏）"""
    logger.log(
        level,
        event,
        extra={"event": event, "trace_id": trace_id, "fields": fields},
    )
