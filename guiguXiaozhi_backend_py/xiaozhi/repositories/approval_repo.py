"""审批请求仓储 —— Phase 2（审计 P0-02）。

与 SessionRepository 同一套范式：ABC + InMemory（离线单测）+ MySQL（多实例/重启一致）。
关键差异是**消费必须原子**：一次性 token 只能有一个调用方把它从 APPROVED 变成
CONSUMED。InMemory 用锁、MySQL 用带条件的单条 UPDATE + rowcount，避免
「先查后改」在并发下双花（验收用例 3 的重放拒绝靠的就是这里）。
"""

from __future__ import annotations

import json
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional


class ApprovalStatus(str, Enum):
    """审批状态机：PENDING → APPROVED → CONSUMED（一次性），其余为终态。"""

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    CONSUMED = "CONSUMED"
    REVOKED = "REVOKED"


TERMINAL_STATUSES = frozenset(
    {
        ApprovalStatus.REJECTED,
        ApprovalStatus.EXPIRED,
        ApprovalStatus.CONSUMED,
        ApprovalStatus.REVOKED,
    }
)


@dataclass
class ApprovalRecord:
    """一条持久化的审批请求。

    ``canonical_args_hash`` 绑定**规范化后的完整参数**；``args_preview`` 只存脱敏后的
    展示副本，绝不把完整身份证号写进审批表/回显给前端。
    ``token_hash`` 只存一次性 token 的 sha256，明文只在签发时返回一次。
    """

    approval_id: str
    subject_id: str
    session_id: str
    task_id: str
    tool_name: str
    canonical_args_hash: str
    resource_scope: str
    status: ApprovalStatus
    expires_at: datetime
    created_at: datetime
    idempotency_key: str
    args_preview: Dict[str, Any] = field(default_factory=dict)
    token_hash: str = ""
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None
    consumed_at: Optional[datetime] = None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


class ApprovalRepository(ABC):
    @abstractmethod
    def create(self, record: ApprovalRecord) -> None:
        """新建 PENDING 请求"""

    @abstractmethod
    def get(self, approval_id: str) -> Optional[ApprovalRecord]:
        """按 id 读取"""

    @abstractmethod
    def find_active_by_key(self, idempotency_key: str) -> Optional[ApprovalRecord]:
        """按业务幂等键找仍可用的请求（PENDING/APPROVED 且未过期），避免重复弹框"""

    @abstractmethod
    def get_by_token_hash(self, token_hash: str) -> Optional[ApprovalRecord]:
        """只按 token 哈希读取（**不消费**），供 HTTP 层预检重放/过期/越权"""

    @abstractmethod
    def save(self, record: ApprovalRecord) -> None:
        """更新状态/签发信息"""

    @abstractmethod
    def consume_by_token(
        self,
        token_hash: str,
        *,
        now: datetime,
        subject_id: str,
        session_id: str,
        task_id: str,
        tool_name: str,
        canonical_args_hash: str,
    ) -> Optional[ApprovalRecord]:
        """原子消费：全部条件命中才把 APPROVED 改成 CONSUMED 并返回记录。

        任何条件不符（重放、过期、跨用户、跨会话、参数被改）一律返回 None。
        """

    @abstractmethod
    def list_for_subject(self, subject_id: str) -> List[ApprovalRecord]:
        """列出该主体的请求（供确认 UI 与审计）"""


def _matches_consume(
    record: ApprovalRecord,
    *,
    now: datetime,
    subject_id: str,
    session_id: str,
    task_id: str,
    tool_name: str,
    canonical_args_hash: str,
) -> bool:
    return (
        record.status == ApprovalStatus.APPROVED
        and record.expires_at > now
        and record.subject_id == subject_id
        and record.session_id == session_id
        and record.task_id == task_id
        and record.tool_name == tool_name
        and record.canonical_args_hash == canonical_args_hash
    )


class InMemoryApprovalRepository(ApprovalRepository):
    """离线单测与本地演示用（进程内，重启即失效）。"""

    def __init__(self) -> None:
        self._rows: Dict[str, ApprovalRecord] = {}
        self._lock = threading.Lock()

    def create(self, record: ApprovalRecord) -> None:
        with self._lock:
            self._rows[record.approval_id] = record

    def get(self, approval_id: str) -> Optional[ApprovalRecord]:
        with self._lock:
            return self._rows.get(approval_id)

    def find_active_by_key(self, idempotency_key: str) -> Optional[ApprovalRecord]:
        with self._lock:
            for record in self._rows.values():
                if record.idempotency_key != idempotency_key or record.is_terminal:
                    continue
                return record
            return None

    def get_by_token_hash(self, token_hash: str) -> Optional[ApprovalRecord]:
        if not token_hash:
            return None
        with self._lock:
            for record in self._rows.values():
                if record.token_hash == token_hash:
                    return record
            return None

    def save(self, record: ApprovalRecord) -> None:
        with self._lock:
            self._rows[record.approval_id] = record

    def consume_by_token(
        self,
        token_hash: str,
        *,
        now: datetime,
        subject_id: str,
        session_id: str,
        task_id: str,
        tool_name: str,
        canonical_args_hash: str,
    ) -> Optional[ApprovalRecord]:
        with self._lock:
            for record in self._rows.values():
                if record.token_hash != token_hash or not token_hash:
                    continue
                if not _matches_consume(
                    record,
                    now=now,
                    subject_id=subject_id,
                    session_id=session_id,
                    task_id=task_id,
                    tool_name=tool_name,
                    canonical_args_hash=canonical_args_hash,
                ):
                    return None
                record.status = ApprovalStatus.CONSUMED
                record.consumed_at = now
                return record
            return None

    def list_for_subject(self, subject_id: str) -> List[ApprovalRecord]:
        with self._lock:
            return [r for r in self._rows.values() if r.subject_id == subject_id]


_MYSQL_COLUMNS = (
    "approval_id, subject_id, session_id, task_id, tool_name, canonical_args_hash, "
    "args_preview, resource_scope, status, expires_at, created_at, idempotency_key, "
    "token_hash, approved_by, approved_at, consumed_at"
)


class MySQLApprovalRepository(ApprovalRepository):
    """MySQL 实现：审批状态跨进程/重启一致（验收用例 5）。"""

    def __init__(
        self, host: str, port: int, user: str, password: str, database: str
    ) -> None:
        self._config = {
            "host": host,
            "port": port,
            "user": user,
            "password": password,
            "database": database,
        }

    def _connect(self):
        import mysql.connector

        return mysql.connector.connect(**self._config)

    def ensure_table(self) -> None:
        """组合根在审批开启时调用；失败直接抛出，绝不静默降级。"""
        conn = self._connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "CREATE TABLE IF NOT EXISTS approval_requests ("
                "  approval_id VARCHAR(128) NOT NULL PRIMARY KEY,"
                "  subject_id VARCHAR(64) NOT NULL,"
                "  session_id VARCHAR(128) NOT NULL,"
                "  task_id VARCHAR(191) NOT NULL,"
                "  tool_name VARCHAR(128) NOT NULL,"
                "  canonical_args_hash CHAR(64) NOT NULL,"
                "  args_preview TEXT NOT NULL,"
                "  resource_scope VARCHAR(191) NOT NULL,"
                "  status VARCHAR(16) NOT NULL,"
                "  expires_at DATETIME NOT NULL,"
                "  created_at DATETIME NOT NULL,"
                "  idempotency_key VARCHAR(191) NOT NULL,"
                "  token_hash CHAR(64) NOT NULL DEFAULT '',"
                "  approved_by VARCHAR(64) DEFAULT NULL,"
                "  approved_at DATETIME DEFAULT NULL,"
                "  consumed_at DATETIME DEFAULT NULL,"
                "  UNIQUE KEY uk_idempotency (idempotency_key),"
                "  INDEX idx_subject_status (subject_id, status),"
                "  INDEX idx_token (token_hash)"
                ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
            )
            conn.commit()
            cursor.close()
        finally:
            conn.close()

    @staticmethod
    def _row_to_record(row: Dict[str, Any]) -> ApprovalRecord:
        preview = row.get("args_preview") or "{}"
        if isinstance(preview, (bytes, bytearray)):
            preview = preview.decode("utf-8")
        return ApprovalRecord(
            approval_id=row["approval_id"],
            subject_id=row["subject_id"],
            session_id=row["session_id"],
            task_id=row["task_id"],
            tool_name=row["tool_name"],
            canonical_args_hash=row["canonical_args_hash"],
            args_preview=json.loads(preview) if preview else {},
            resource_scope=row["resource_scope"],
            status=ApprovalStatus(row["status"]),
            expires_at=row["expires_at"],
            created_at=row["created_at"],
            idempotency_key=row["idempotency_key"],
            token_hash=row.get("token_hash") or "",
            approved_by=row.get("approved_by"),
            approved_at=row.get("approved_at"),
            consumed_at=row.get("consumed_at"),
        )

    def _fetch_one(self, where: str, params: tuple) -> Optional[ApprovalRecord]:
        conn = self._connect()
        try:
            cursor = conn.cursor(dictionary=True)
            cursor.execute(
                f"SELECT {_MYSQL_COLUMNS} FROM approval_requests WHERE {where} LIMIT 1",
                params,
            )
            row = cursor.fetchone()
            cursor.close()
            return self._row_to_record(row) if row else None
        finally:
            conn.close()

    def create(self, record: ApprovalRecord) -> None:
        conn = self._connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                f"INSERT INTO approval_requests ({_MYSQL_COLUMNS}) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    record.approval_id,
                    record.subject_id,
                    record.session_id,
                    record.task_id,
                    record.tool_name,
                    record.canonical_args_hash,
                    json.dumps(record.args_preview, ensure_ascii=False),
                    record.resource_scope,
                    record.status.value,
                    record.expires_at,
                    record.created_at,
                    record.idempotency_key,
                    record.token_hash,
                    record.approved_by,
                    record.approved_at,
                    record.consumed_at,
                ),
            )
            conn.commit()
            cursor.close()
        finally:
            conn.close()

    def get(self, approval_id: str) -> Optional[ApprovalRecord]:
        return self._fetch_one("approval_id=%s", (approval_id,))

    def get_by_token_hash(self, token_hash: str) -> Optional[ApprovalRecord]:
        if not token_hash:
            return None
        return self._fetch_one("token_hash=%s", (token_hash,))

    def find_active_by_key(self, idempotency_key: str) -> Optional[ApprovalRecord]:
        conn = self._connect()
        try:
            cursor = conn.cursor(dictionary=True)
            cursor.execute(
                f"SELECT {_MYSQL_COLUMNS} FROM approval_requests "
                "WHERE idempotency_key=%s AND status IN ('PENDING','APPROVED') "
                "AND expires_at > NOW() LIMIT 1",
                (idempotency_key,),
            )
            row = cursor.fetchone()
            cursor.close()
            return self._row_to_record(row) if row else None
        finally:
            conn.close()

    def save(self, record: ApprovalRecord) -> None:
        conn = self._connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE approval_requests SET status=%s, token_hash=%s, approved_by=%s, "
                "approved_at=%s, consumed_at=%s, args_preview=%s WHERE approval_id=%s",
                (
                    record.status.value,
                    record.token_hash,
                    record.approved_by,
                    record.approved_at,
                    record.consumed_at,
                    json.dumps(record.args_preview, ensure_ascii=False),
                    record.approval_id,
                ),
            )
            conn.commit()
            cursor.close()
        finally:
            conn.close()

    def consume_by_token(
        self,
        token_hash: str,
        *,
        now: datetime,
        subject_id: str,
        session_id: str,
        task_id: str,
        tool_name: str,
        canonical_args_hash: str,
    ) -> Optional[ApprovalRecord]:
        if not token_hash:
            return None
        conn = self._connect()
        try:
            cursor = conn.cursor()
            # 单条条件 UPDATE：并发下只有一个请求能拿到 rowcount=1
            cursor.execute(
                "UPDATE approval_requests SET status='CONSUMED', consumed_at=%s "
                "WHERE token_hash=%s AND status='APPROVED' AND expires_at > %s "
                "AND subject_id=%s AND session_id=%s AND task_id=%s "
                "AND tool_name=%s AND canonical_args_hash=%s",
                (
                    now,
                    token_hash,
                    now,
                    subject_id,
                    session_id,
                    task_id,
                    tool_name,
                    canonical_args_hash,
                ),
            )
            affected = cursor.rowcount
            conn.commit()
            cursor.close()
            if affected != 1:
                return None
            return self._fetch_one("token_hash=%s AND status='CONSUMED'", (token_hash,))
        finally:
            conn.close()

    def list_for_subject(self, subject_id: str) -> List[ApprovalRecord]:
        conn = self._connect()
        try:
            cursor = conn.cursor(dictionary=True)
            cursor.execute(
                f"SELECT {_MYSQL_COLUMNS} FROM approval_requests "
                "WHERE subject_id=%s ORDER BY created_at DESC LIMIT 50",
                (subject_id,),
            )
            rows = cursor.fetchall()
            cursor.close()
            return [self._row_to_record(r) for r in rows]
        finally:
            conn.close()
