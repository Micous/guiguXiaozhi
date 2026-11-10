"""审计日志：权限决策与工具执行单独落盘，不与普通日志混在一起。

对应 PLAN.md「3.4.2 L4 执行+审计」「3.4.5 审计日志」。
所有字段写盘前统一脱敏；``path=None`` 时只保留内存环形缓冲（测试友好）。
"""

from __future__ import annotations

import json
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

from xiaozhi.reliability.redaction import sanitize_observability_value


class AuditLogger:
    """JSONL 审计日志"""

    def __init__(
        self,
        path: Optional[str] = None,
        *,
        max_memory: int = 500,
        enabled: bool = True,
    ) -> None:
        self.path = Path(path) if path else None
        self.enabled = enabled
        self._memory: Deque[Dict[str, Any]] = deque(maxlen=max(1, max_memory))
        self._lock = threading.Lock()
        if self.path and self.enabled:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, event_type: str, **fields: Any) -> Dict[str, Any]:
        """写一条审计记录，返回脱敏后的条目"""
        entry: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
        }
        for key, value in fields.items():
            entry[key] = sanitize_observability_value(key, value)

        if not self.enabled:
            return entry

        with self._lock:
            self._memory.append(entry)
            if self.path:
                try:
                    with self.path.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
                except OSError:
                    # 审计写盘失败绝不影响主流程
                    pass
        return entry

    def read_recent(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._lock:
            entries = list(self._memory)
        return entries[-max(0, limit):]

    def clear(self) -> None:
        with self._lock:
            self._memory.clear()

    def close(self) -> None:
        """当前实现逐条 append，无需额外 flush；保留接口便于后续换缓冲写"""
        return None
