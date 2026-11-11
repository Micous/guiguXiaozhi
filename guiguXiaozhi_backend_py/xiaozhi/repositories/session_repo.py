"""会话归属仓储 —— ``session_id`` → ``owner_subject_id``。

Phase 1 之前 ``memoryId`` 由客户端自报，等于把会话主键交给访客：
知道别人的 memoryId 就能读历史、接着对话、甚至操作其预约。
现在 session_id 由服务端签发（``secrets.token_urlsafe``，不可枚举），
归属关系落库，跨用户访问在路由层一律 403。

与 appointment_repo 不同，这里 **不吞异常**：认证开启后存储故障必须
快速失败，否则归属校验会退化成「查不到 → 默认放行」或伪装成空历史。
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Optional


def now_str() -> str:
    return datetime.now().isoformat(sep=" ", timespec="seconds")


@dataclass(frozen=True)
class SessionRecord:
    session_id: str
    owner_subject_id: str
    created_at: str


class SessionRepository(ABC):
    @abstractmethod
    def create(self, record: SessionRecord) -> None:
        """登记一条服务端签发的会话"""

    @abstractmethod
    def get_owner(self, session_id: str) -> Optional[str]:
        """查归属；None 表示会话不存在"""

    @abstractmethod
    def delete(self, session_id: str) -> bool:
        """删除会话（预留给登出/回收）"""


class InMemorySessionRepository(SessionRepository):
    """离线单测与本地演示用（进程内，重启即失效）。"""

    def __init__(self) -> None:
        self._rows: Dict[str, SessionRecord] = {}
        self._lock = threading.Lock()

    def create(self, record: SessionRecord) -> None:
        with self._lock:
            self._rows[record.session_id] = record

    def get_owner(self, session_id: str) -> Optional[str]:
        with self._lock:
            row = self._rows.get(session_id)
            return row.owner_subject_id if row else None

    def delete(self, session_id: str) -> bool:
        with self._lock:
            return self._rows.pop(session_id, None) is not None


class MySQLSessionRepository(SessionRepository):
    """MySQL 实现（多实例共享归属关系，重启不丢）。"""

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
        """组合根在认证开启时调用；失败直接抛出，绝不静默降级。"""
        conn = self._connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "CREATE TABLE IF NOT EXISTS agent_sessions ("
                "  session_id VARCHAR(128) NOT NULL PRIMARY KEY,"
                "  owner_subject_id VARCHAR(64) NOT NULL,"
                "  created_at DATETIME NOT NULL,"
                "  INDEX idx_owner (owner_subject_id)"
                ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
            )
            conn.commit()
            cursor.close()
        finally:
            conn.close()

    def create(self, record: SessionRecord) -> None:
        conn = self._connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO agent_sessions (session_id, owner_subject_id, created_at) "
                "VALUES (%s, %s, %s)",
                (record.session_id, record.owner_subject_id, record.created_at),
            )
            conn.commit()
            cursor.close()
        finally:
            conn.close()

    def get_owner(self, session_id: str) -> Optional[str]:
        conn = self._connect()
        try:
            cursor = conn.cursor(dictionary=True)
            cursor.execute(
                "SELECT owner_subject_id FROM agent_sessions WHERE session_id=%s LIMIT 1",
                (session_id,),
            )
            row = cursor.fetchone()
            cursor.close()
            return row["owner_subject_id"] if row else None
        finally:
            conn.close()

    def delete(self, session_id: str) -> bool:
        conn = self._connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM agent_sessions WHERE session_id=%s", (session_id,)
            )
            affected = cursor.rowcount
            conn.commit()
            cursor.close()
            return affected > 0
        finally:
            conn.close()
