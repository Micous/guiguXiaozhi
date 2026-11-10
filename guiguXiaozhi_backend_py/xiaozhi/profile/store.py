"""患者画像结构化存储 —— P2-M2。

MySQL `guiguxiaozhi.user_profile` 表（与预约表同库）：
    user_key      VARCHAR(16)   sha256(身份证号)[:16]，无身份证号不建画像
    fact_key      VARCHAR(32)   事实类型（ALLOWED_KEYS 之一）
    fact_value    TEXT          敏感字段 Fernet 密文，其余明文
    confidence    FLOAT
    source_trace_id VARCHAR(64)
    updated_at    TIMESTAMP     覆盖式更新自动刷新（覆盖即天然遗忘）
    UNIQUE KEY (user_key, fact_key)

合规红线：
  * 身份证号等敏感字段**密文落库**（Fernet，密钥 env PROFILE_SECRET_KEY）；
    密钥缺失 → 抛 ProfileDisabledError，装配层据此整体禁用画像模块并告警；
  * 画像与本表**绝不参与重复挂号判定**（find_duplicate 只认 appointment 表）。
"""

from __future__ import annotations

import hashlib
import logging
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, List, Optional

from xiaozhi.profile.extractor import FactEntry

logger = logging.getLogger(__name__)

# 需要加密存储的敏感事实 key
SENSITIVE_KEYS = frozenset({"id_card"})

# 密钥缺失时抛出 —— 装配层捕获后整体禁用画像模块
class ProfileDisabledError(RuntimeError):
    pass


def derive_user_key(id_card: str) -> str:
    """身份证号 → 稳定匿名主键（不落原始号码做主键）。"""
    return hashlib.sha256(id_card.strip().encode("utf-8")).hexdigest()[:16]


class FactCipher:
    """Fernet 对称加密（敏感字段专用）。"""

    def __init__(self, secret_key: str) -> None:
        if not secret_key:
            raise ProfileDisabledError(
                "PROFILE_SECRET_KEY 未配置——画像模块整体禁用（合规要求："
                "敏感字段必须密文落库，无密钥不得明文存储）"
            )
        from cryptography.fernet import Fernet

        self._fernet = Fernet(secret_key.encode("utf-8"))

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        return self._fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")


@dataclass
class StoredFact:
    """读出的一条画像事实（值为**明文**）"""

    key: str
    value: str
    confidence: float = 1.0
    source_trace_id: str = ""
    updated_at: str = ""


class ProfileRepository(ABC):
    """画像仓储接口（MySQL 真实现 / 内存测试实现共用）"""

    @abstractmethod
    def upsert(self, user_key: str, fact: FactEntry, cipher: Optional[FactCipher] = None) -> None:
        """写入事实（同 (user_key, fact_key) 覆盖旧值 —— 覆盖即遗忘）"""

    @abstractmethod
    def get_all(self, user_key: str, cipher: Optional[FactCipher] = None) -> List[StoredFact]:
        """取该用户的全部事实（值已解密为明文）"""

    @abstractmethod
    def get(self, user_key: str, fact_key: str, cipher: Optional[FactCipher] = None) -> Optional[StoredFact]:
        """按 key 精确取（身份证等精确字段只走这里，不走向量）"""

    @abstractmethod
    def delete(self, user_key: str) -> int:
        """删除该用户全部画像（"忘记我"），返回删除条数"""

    @staticmethod
    def _encode_value(fact: FactEntry, cipher: Optional[FactCipher]) -> str:
        """敏感字段密文落库；无 cipher 时拒绝存敏感字段（合规兜底）。"""
        if fact.key in SENSITIVE_KEYS:
            if cipher is None:
                raise ProfileDisabledError(
                    f"敏感字段 {fact.key} 必须加密存储，但未配置 cipher"
                )
            return cipher.encrypt(fact.value)
        return fact.value

    @staticmethod
    def _decode_value(fact_key: str, stored_value: str, cipher: Optional[FactCipher]) -> str:
        if fact_key in SENSITIVE_KEYS:
            if cipher is None:
                # 读出时没有 cipher：不返回密文也不返回明文，视为该事实不可用
                logger.warning("敏感字段 %s 无 cipher 可解密，跳过", fact_key)
                return ""
            try:
                return cipher.decrypt(stored_value)
            except Exception:
                logger.warning("敏感字段 %s 解密失败，跳过", fact_key)
                return ""
        return stored_value


class MySQLProfileRepository(ProfileRepository):
    """MySQL 实现（guiguxiaozhi.user_profile，启动时自动建表）"""

    def __init__(self, host: str, port: int, user: str, password: str, database: str) -> None:
        self._config = {
            "host": host,
            "port": port,
            "user": user,
            "password": password,
            "database": database,
        }
        self._lock = threading.Lock()
        self._ensure_table()

    def _connect(self):
        import mysql.connector

        return mysql.connector.connect(**self._config)

    def _ensure_table(self) -> None:
        conn = self._connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS user_profile (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    user_key VARCHAR(16) NOT NULL,
                    fact_key VARCHAR(32) NOT NULL,
                    fact_value TEXT NOT NULL,
                    confidence FLOAT NOT NULL DEFAULT 1.0,
                    source_trace_id VARCHAR(64) NOT NULL DEFAULT '',
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                        ON UPDATE CURRENT_TIMESTAMP,
                    UNIQUE KEY uk_user_fact (user_key, fact_key),
                    KEY idx_user (user_key)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            conn.commit()
        finally:
            conn.close()

    def upsert(self, user_key: str, fact: FactEntry, cipher: Optional[FactCipher] = None) -> None:
        value = self._encode_value(fact, cipher)
        with self._lock:
            conn = self._connect()
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO user_profile "
                    "(user_key, fact_key, fact_value, confidence, source_trace_id) "
                    "VALUES (%s, %s, %s, %s, %s) "
                    "ON DUPLICATE KEY UPDATE fact_value=VALUES(fact_value), "
                    "confidence=VALUES(confidence), source_trace_id=VALUES(source_trace_id)",
                    (user_key, fact.key, value, fact.confidence, fact.source_trace_id),
                )
                conn.commit()
            finally:
                conn.close()

    def get_all(self, user_key: str, cipher: Optional[FactCipher] = None) -> List[StoredFact]:
        try:
            conn = self._connect()
        except Exception as exc:
            logger.warning("画像库连接失败：%s", exc)
            return []
        try:
            cursor = conn.cursor(dictionary=True)
            cursor.execute(
                "SELECT fact_key, fact_value, confidence, source_trace_id, updated_at "
                "FROM user_profile WHERE user_key=%s",
                (user_key,),
            )
            rows = cursor.fetchall()
        finally:
            conn.close()
        facts = []
        for row in rows:
            value = self._decode_value(row["fact_key"], row["fact_value"], cipher)
            if not value:
                continue
            facts.append(
                StoredFact(
                    key=row["fact_key"],
                    value=value,
                    confidence=row.get("confidence") or 1.0,
                    source_trace_id=row.get("source_trace_id") or "",
                    updated_at=str(row.get("updated_at") or ""),
                )
            )
        return facts

    def get(self, user_key: str, fact_key: str, cipher: Optional[FactCipher] = None) -> Optional[StoredFact]:
        for fact in self.get_all(user_key, cipher):
            if fact.key == fact_key:
                return fact
        return None

    def delete(self, user_key: str) -> int:
        with self._lock:
            conn = self._connect()
            try:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM user_profile WHERE user_key=%s", (user_key,))
                conn.commit()
                return cursor.rowcount
            finally:
                conn.close()


class InMemoryProfileRepository(ProfileRepository):
    """内存实现（测试 / MySQL 不可用时的降级兜底）"""

    def __init__(self) -> None:
        self._data: Dict[str, Dict[str, StoredFact]] = {}
        self._lock = threading.Lock()

    def upsert(self, user_key: str, fact: FactEntry, cipher: Optional[FactCipher] = None) -> None:
        stored = StoredFact(
            key=fact.key,
            value=self._encode_value(fact, cipher),
            confidence=fact.confidence,
            source_trace_id=fact.source_trace_id,
            updated_at=fact.ts,
        )
        with self._lock:
            self._data.setdefault(user_key, {})[fact.key] = stored

    def get_all(self, user_key: str, cipher: Optional[FactCipher] = None) -> List[StoredFact]:
        with self._lock:
            items = list(self._data.get(user_key, {}).values())
        facts = []
        for stored in items:
            value = self._decode_value(stored.key, stored.value, cipher)
            if not value:
                continue
            facts.append(
                StoredFact(
                    key=stored.key,
                    value=value,
                    confidence=stored.confidence,
                    source_trace_id=stored.source_trace_id,
                    updated_at=stored.updated_at,
                )
            )
        return facts

    def get(self, user_key: str, fact_key: str, cipher: Optional[FactCipher] = None) -> Optional[StoredFact]:
        for fact in self.get_all(user_key, cipher):
            if fact.key == fact_key:
                return fact
        return None

    def delete(self, user_key: str) -> int:
        with self._lock:
            return len(self._data.pop(user_key, {}))
