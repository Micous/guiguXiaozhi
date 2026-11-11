"""审计日志：权限决策与工具执行单独落盘，不与普通日志混在一起。

对应 PLAN.md「3.4.2 L4 执行+审计」「3.4.5 审计日志」。
所有字段写盘前统一脱敏；``path=None`` 时只保留内存环形缓冲（测试友好）。

Phase 6 增加两件事：

1. **多 sink**：审计条目可以同时写本地 JSONL、MongoDB（集中存储，多实例可查）
   等任意实现 ``write(entry)`` 的 sink。sink 抛异常即视为该条审计**未落地**。
2. **fail-closed**：``fail_closed=True`` 时任一 sink 写失败立即抛
   ``AuditWriteError``，由调用方拒绝执行该次工具调用 —— 「无法留痕的操作不执行」。
   默认 ``False``（开发/内网演示行为零变更，写失败只记 warning）。

   注意语义边界：执行**前**的审计（permission_decision / approval_required）
   失败能让操作不发生；执行**后**的审计（tool_execution）失败时副作用已经发生，
   只能告警 + 计数，属于对账范畴，不能靠 fail-closed 挽回。所以
   fail-closed 保护的是「没有痕迹的写操作」而不是「已发生的写操作」。
"""

from __future__ import annotations

import json
import logging
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Protocol

from xiaozhi.reliability.redaction import sanitize_observability_value

logger = logging.getLogger(__name__)


class AuditWriteError(RuntimeError):
    """审计条目未能落到任何受信 sink（fail-closed 时拒绝执行操作）。"""


class AuditSink(Protocol):
    """审计落地目标。实现方抛异常即表示该条记录没落成。"""

    def write(self, entry: Dict[str, Any]) -> None:  # pragma: no cover - 协议声明
        ...


class FileAuditSink:
    """本地 JSONL（逐条 append）。

    逐条 append + flush 是有意的：审计的价值就在于**崩溃前的最后一条也在盘上**，
    缓冲写会让"进程被 kill 时丢掉最后几条"变成默认行为。
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, entry: Dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")


class MongoAuditSink:
    """MongoDB 集中审计集合（多实例共享，可按 trace_id 查询）。

    Mongo 不可用是**真实故障**：``write`` 会抛 ``AuditWriteError``，
    而不是"落本地文件兜底" —— 审计的集中存储一旦悄悄降级成本地文件，
    运维在集中库里看不到记录，等于没有审计。
    """

    def __init__(
        self,
        uri: str,
        database: str = "chat_memory_db",
        collection: str = "audit_events",
        server_selection_timeout_ms: int = 2000,
    ) -> None:
        self.uri = uri
        self.database = database
        self.collection = collection
        self.timeout_ms = server_selection_timeout_ms
        self._client = None

    @property
    def client(self):
        if self._client is None:
            from pymongo import MongoClient

            self._client = MongoClient(
                self.uri,
                serverSelectionTimeoutMS=self.timeout_ms,
                connectTimeoutMS=self.timeout_ms,
                socketTimeoutMS=self.timeout_ms,
            )
        return self._client

    def ping(self) -> None:
        """启动期探活：连不上就直接抛错，绝不「禁用 + 告警」静默降级。"""
        self.client.admin.command("ping")

    def write(self, entry: Dict[str, Any]) -> None:
        try:
            self.client[self.database][self.collection].insert_one(dict(entry))
        except Exception as exc:  # pymongo 异常族很宽，统一收敛为 AuditWriteError
            raise AuditWriteError(f"Mongo 审计写入失败：{type(exc).__name__}: {exc}") from exc

    def read_recent(self, limit: int = 100) -> List[Dict[str, Any]]:
        cursor = (
            self.client[self.database][self.collection]
            .find({}, {"_id": 0})
            .sort("timestamp", -1)
            .limit(max(0, limit))
        )
        return [dict(item) for item in cursor]


class AuditLogger:
    """JSONL / 多 sink 审计日志"""

    def __init__(
        self,
        path: Optional[str] = None,
        *,
        max_memory: int = 500,
        enabled: bool = True,
        fail_closed: bool = False,
        sinks: Optional[List[AuditSink]] = None,
        on_write_error: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.path = Path(path) if path else None
        self.enabled = enabled
        self.fail_closed = fail_closed
        self._memory: Deque[Dict[str, Any]] = deque(maxlen=max(1, max_memory))
        self._lock = threading.Lock()
        self._write_errors = 0
        self._on_write_error = on_write_error

        # 关闭审计却要求 fail-closed 是自相矛盾的配置：等于「不写审计但要求
        # 审计必须成功」。启动期就炸掉，避免上线后才发现审计形同虚设。
        if self.fail_closed and not self.enabled:
            raise ValueError(
                "audit_fail_closed=true 与 audit_enabled=false 冲突："
                "要求审计不可失败，却又关闭了审计"
            )

        self._sinks: List[AuditSink] = list(sinks or [])
        if self.path and self.enabled:
            self._sinks.insert(0, FileAuditSink(self.path))

    # ---------- 写入 ----------

    @property
    def write_errors(self) -> int:
        return self._write_errors

    def record(self, event_type: str, **fields: Any) -> Dict[str, Any]:
        """写一条审计记录，返回脱敏后的条目。

        ``fail_closed=True`` 时任一 sink 失败即抛 ``AuditWriteError``。
        """
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
            failures: List[str] = []
            for sink in self._sinks:
                try:
                    sink.write(entry)
                except Exception as exc:
                    failures.append(f"{type(sink).__name__}: {exc}")
            if failures:
                self._write_errors += 1
                detail = "; ".join(failures)
                if self._on_write_error is not None:
                    try:
                        self._on_write_error(detail)
                    except Exception:  # 回调自身出错不能掩盖审计失败
                        logger.exception("审计失败回调出错")
                if self.fail_closed:
                    raise AuditWriteError(
                        f"审计条目未能落地（{event_type}）：{detail}"
                    )
                # 非 fail-closed：绝不影响主流程，但必须留痕（否则没人会发现）
                logger.warning("审计写入失败（fail-open）：%s", detail)
        return entry

    def read_recent(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._lock:
            entries = list(self._memory)
        return entries[-max(0, limit) :]

    def clear(self) -> None:
        with self._lock:
            self._memory.clear()

    def close(self) -> None:
        """当前实现逐条 append，无需额外 flush；保留接口便于后续换缓冲写"""
        return None
