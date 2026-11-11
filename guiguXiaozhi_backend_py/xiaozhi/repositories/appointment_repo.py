"""预约记录仓储 —— MySQL 访问层（Phase 3a 重写，审计 P0-04）。

用接口隔离存储实现：真实环境用 MySQL，MySQL 不可用时可降级到内存实现
（见 InMemoryAppointmentRepository），保证单点故障不让用户看到 500。

**Phase 3a 的变化（对照旧版）**

============================  ==========================================
旧版（`9822856` 起）             本版
============================  ==========================================
4 处 ``except Exception``       按 `repositories/errors.py` 分类抛异常
静默 ``return None``/``False``   基础设施故障 → ``RepositoryUnavailable``
find_duplicate + save 两次连接  ``create_if_absent`` 收敛到**同一事务**
无唯一索引                     ``uk_appointment_active`` 唯一约束兜底
无状态字段                     ``status``（取消走状态迁移，保留痕迹）
无乐观锁                       ``version`` 字段
取消 = 物理 DELETE              状态迁移为 ``CANCELLED``（可审计）
============================  ==========================================

**事务边界为什么重要**

旧版的"查重 → 写入"是两次独立连接、两次独立事务：

.. code-block:: text

    T1: SELECT ... WHERE 同一患者/科室/日期/时段   → 没有
    T2: INSERT ...                                  → 写入成功

    T1 与 T2 之间，另一个请求完成了同样的 T1+T2 → **两条重复预约**

工具层的 ``find_duplicate`` 只是**缓解**，不是修复 —— 竞态窗口真实存在。
唯一约束（``uk_appointment_active``）是最后一道防线；
而 ``create_if_absent`` 把查重与写入收进**同一个事务**，
让"检查 + 写入"具备原子性。
"""

from __future__ import annotations

import hashlib
import logging
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from xiaozhi.repositories.errors import (
    NotFound,
    PersistenceError,
    RepositoryUnavailable,
    UniqueConflict,
)

logger = logging.getLogger(__name__)


class AppointmentStatus(str, Enum):
    """预约状态机。

    ``REQUESTED → HOLDING → CONFIRMED``：请求 → 预占 → 确认；
    ``UNKNOWN``：结果核实中（外部号源超时等，需要对账）；
    ``CANCELLED``：已取消（**不物理删除**，保留审计痕迹）。
    """

    REQUESTED = "REQUESTED"
    HOLDING = "HOLDING"
    CONFIRMED = "CONFIRMED"
    UNKNOWN = "UNKNOWN"
    CANCELLED = "CANCELLED"


#: 可以被取消的状态（已取消/未确认的不能再取消）
CANCELLABLE_STATUSES = (
    AppointmentStatus.REQUESTED,
    AppointmentStatus.HOLDING,
    AppointmentStatus.CONFIRMED,
    AppointmentStatus.UNKNOWN,
)


@dataclass
class Appointment:
    """预约记录"""

    id: Optional[int] = None
    username: str = ""
    id_card: str = ""
    department: str = ""
    date: str = ""
    time: str = ""
    doctor_name: Optional[str] = None
    # ---- Phase 3a 新增 ----
    status: AppointmentStatus = AppointmentStatus.CONFIRMED
    version: int = 1

    def key(self) -> Tuple[str, str, str, str, str]:
        """防重复预约的判定键（不含医生 —— 同科室同时段只能有一条）

        **这五元组必须与数据库唯一约束 ``uk_appointment_active`` 的前五列
        完全一致** —— 约束比业务键更严会拒绝合法业务（2026-11-11 踩过：
        约束少了 username，把两个不同患者误判为重复，迁移直接失败）。
        """
        return (self.username, self.id_card, self.department, self.date, self.time)

    @staticmethod
    def idempotency_key(
        username: str,
        id_card: str,
        department: str,
        date: str,
        time: str,
    ) -> str:
        """业务幂等键（同一组预约信息 → 同一个键）。

        与 ``key()`` 的区别：这里做**哈希**，可直接作为短字符串落库/索引。
        """
        raw = "|".join((username, id_card, department, date, time))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:64]


class AppointmentRepository(ABC):
    """预约仓储接口"""

    @abstractmethod
    def find_duplicate(self, appointment: Appointment) -> Optional[Appointment]:
        """查找同一人、同科室、同日期同时段的**未取消**预约。

        找不到返回 ``None``；基础设施故障抛 ``RepositoryUnavailable``
        （**不再静默返回 None** —— 那会让查重形同虚设）。
        """

    @abstractmethod
    def find_by_key(self, appointment: Appointment) -> Optional[Appointment]:
        """按业务键查预约（**任意状态**，含 CANCELLED）。

        与 ``find_duplicate`` 的区别：``find_duplicate`` 只看未取消记录
        （防重复预约语义）；本方法用于区分"从未约过"和"约过但已取消"，
        让重复取消能给出准确文案而非笼统的"未找到"。
        """

    @abstractmethod
    def save(self, appointment: Appointment) -> Appointment:
        """写入预约。

        成功返回带 ``id`` 的记录；唯一约束冲突抛 ``UniqueConflict``；
        基础设施故障抛 ``RepositoryUnavailable``。
        """

    @abstractmethod
    def create_if_absent(self, appointment: Appointment) -> Tuple[Appointment, bool]:
        """**原子**地"查重 + 写入"，返回 (记录, 是否新建)。

        这是 ``find_duplicate`` + ``save`` 的事务化替代：
        查重与写入在**同一事务**内完成，消除竞态窗口。

        * 已存在 → 返回既有记录 + ``False``（不重复写）；
        * 不存在 → 写入并返回 ``True``；
        * 并发下另一个请求先写入 → 唯一约束冲突 → 读回既有记录 + ``False``。
        """

    @abstractmethod
    def cancel(self, appointment_id: int) -> Appointment:
        """取消预约 —— **状态迁移**，不物理删除（保留审计痕迹）。

        记录不存在抛 ``NotFound``；已取消抛 ``InvalidState``。
        """

    @abstractmethod
    def delete(self, appointment_id: int) -> bool:
        """物理删除（仅运维/数据清理用；业务上的取消请走 ``cancel``）。"""

    @abstractmethod
    def list_all(self, limit: int = 100) -> List[Appointment]:
        """列出预约（运维/审计用）"""


class MySQLAppointmentRepository(AppointmentRepository):
    """MySQL 实现（与 Java 版共用同一个 guiguxiaozhi 库与表）"""

    def __init__(self, host: str, port: int, user: str, password: str, database: str) -> None:
        self._config = {
            "host": host,
            "port": port,
            "user": user,
            "password": password,
            "database": database,
        }
        self._lock = threading.Lock()

    def _connect(self):
        import mysql.connector

        return mysql.connector.connect(**self._config)

    # ---- 异常翻译 ----

    @staticmethod
    def _translate(exc: Exception, action: str) -> Exception:
        """把底层异常翻译成仓储域异常。

        ``mysql.connector`` 的异常类型不稳定（连接错误、完整性错误、
        编程错误混在一起），在这里统一分类，调用方只需处理仓储域异常。
        """
        import mysql.connector

        if isinstance(exc, mysql.connector.IntegrityError):
            # 唯一约束冲突 → 业务规则冲突（重复预约）
            if "Duplicate entry" in str(exc):
                return UniqueConflict(
                    f"{action}失败：已存在相同的预约记录（唯一约束冲突）"
                )
            return PersistenceError(f"{action}失败：数据完整性约束不满足（{exc}）")
        if isinstance(
            exc,
            (
                mysql.connector.Error,
                mysql.connector.errors.OperationalError,
                mysql.connector.errors.InterfaceError,
                mysql.connector.errors.DatabaseError,
            ),
        ):
            return RepositoryUnavailable(f"{action}失败：数据存储暂时不可用（{exc}）")
        return PersistenceError(f"{action}失败：{exc}")

    def _row_to_appointment(self, row: Dict[str, Any]) -> Appointment:
        raw_status = row.get("status") or AppointmentStatus.CONFIRMED.value
        try:
            status = AppointmentStatus(raw_status)
        except ValueError:
            # 库里出现了未知状态（老数据/人工改过）→ 归为 CONFIRMED 保持可读
            logger.warning("未知的预约状态，归为 CONFIRMED：%r", raw_status)
            status = AppointmentStatus.CONFIRMED
        return Appointment(
            id=row.get("id"),
            username=row.get("username", ""),
            id_card=row.get("id_card", ""),
            department=row.get("department", ""),
            date=row.get("date", ""),
            time=row.get("time", ""),
            doctor_name=row.get("doctor_name"),
            status=status,
            version=int(row.get("version") or 1),
        )

    _SELECT_COLUMNS = (
        "id, username, id_card, department, date, time, doctor_name, status, version"
    )

    def find_duplicate(self, appointment: Appointment) -> Optional[Appointment]:
        conn = self._connect()
        try:
            cursor = conn.cursor(dictionary=True)
            cursor.execute(
                f"SELECT {self._SELECT_COLUMNS} FROM appointment "
                "WHERE username=%s AND id_card=%s AND department=%s "
                "AND date=%s AND time=%s AND status <> 'CANCELLED' LIMIT 1",
                (
                    appointment.username,
                    appointment.id_card,
                    appointment.department,
                    appointment.date,
                    appointment.time,
                ),
            )
            row = cursor.fetchone()
            cursor.close()
            return self._row_to_appointment(row) if row else None
        except Exception as exc:
            raise self._translate(exc, "查询预约") from exc
        finally:
            conn.close()

    def find_by_key(self, appointment: Appointment) -> Optional[Appointment]:
        conn = self._connect()
        try:
            cursor = conn.cursor(dictionary=True)
            cursor.execute(
                f"SELECT {self._SELECT_COLUMNS} FROM appointment "
                "WHERE username=%s AND id_card=%s AND department=%s "
                "AND date=%s AND time=%s ORDER BY id DESC LIMIT 1",
                (
                    appointment.username,
                    appointment.id_card,
                    appointment.department,
                    appointment.date,
                    appointment.time,
                ),
            )
            row = cursor.fetchone()
            cursor.close()
            return self._row_to_appointment(row) if row else None
        except Exception as exc:
            raise self._translate(exc, "查询预约") from exc
        finally:
            conn.close()

    def save(self, appointment: Appointment) -> Appointment:
        conn = self._connect()
        try:
            cursor = conn.cursor()
            now = datetime.now()
            cursor.execute(
                "INSERT INTO appointment (username, id_card, department, date, time, "
                "doctor_name, status, version, created_at, updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    appointment.username,
                    appointment.id_card,
                    appointment.department,
                    appointment.date,
                    appointment.time,
                    appointment.doctor_name,
                    appointment.status.value,
                    appointment.version,
                    now,
                    now,
                ),
            )
            conn.commit()
            appointment.id = cursor.lastrowid
            cursor.close()
            return appointment
        except Exception as exc:
            conn.rollback()
            raise self._translate(exc, "写入预约") from exc
        finally:
            conn.close()

    def create_if_absent(self, appointment: Appointment) -> Tuple[Appointment, bool]:
        """原子"查重 + 写入"。

        事务内先查（命中就返回既有记录），否则插入；
        插入时若撞唯一约束（并发下另一个请求抢先写入），
        **回滚并读回既有记录** —— 返回 (既有记录, False)，不报错。
        """
        conn = self._connect()
        try:
            cursor = conn.cursor(dictionary=True)
            # 同一事务内查重
            cursor.execute(
                f"SELECT {self._SELECT_COLUMNS} FROM appointment "
                "WHERE username=%s AND id_card=%s AND department=%s "
                "AND date=%s AND time=%s AND status <> 'CANCELLED' LIMIT 1 "
                "FOR UPDATE",
                (
                    appointment.username,
                    appointment.id_card,
                    appointment.department,
                    appointment.date,
                    appointment.time,
                ),
            )
            row = cursor.fetchone()
            if row is not None:
                conn.commit()
                cursor.close()
                return self._row_to_appointment(row), False

            now = datetime.now()
            cursor.execute(
                "INSERT INTO appointment (username, id_card, department, date, time, "
                "doctor_name, status, version, created_at, updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    appointment.username,
                    appointment.id_card,
                    appointment.department,
                    appointment.date,
                    appointment.time,
                    appointment.doctor_name,
                    appointment.status.value,
                    appointment.version,
                    now,
                    now,
                ),
            )
            conn.commit()
            appointment.id = cursor.lastrowid
            cursor.close()
            return appointment, True
        except Exception as exc:
            conn.rollback()
            translated = self._translate(exc, "写入预约")
            if isinstance(translated, UniqueConflict):
                # 并发下另一个请求抢先写入 → 读回既有记录，不当成错误
                existing = self.find_duplicate(appointment)
                if existing is not None:
                    logger.info("并发写入撞唯一约束，返回既有记录：%s", appointment.key())
                    return existing, False
            raise translated from exc
        finally:
            conn.close()

    def cancel(self, appointment_id: int) -> Appointment:
        """状态迁移为 CANCELLED（不物理删除）。"""
        conn = self._connect()
        try:
            cursor = conn.cursor(dictionary=True)
            cursor.execute(
                f"SELECT {self._SELECT_COLUMNS} FROM appointment WHERE id=%s",
                (appointment_id,),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.close()
                raise NotFound(f"预约记录不存在（id={appointment_id}）")
            current = self._row_to_appointment(row)
            if current.status == AppointmentStatus.CANCELLED:
                cursor.close()
                from xiaozhi.repositories.errors import InvalidState

                raise InvalidState("该预约已取消，无需重复取消")
            cursor.execute(
                "UPDATE appointment SET status='CANCELLED', version=version+1, "
                "updated_at=NOW() WHERE id=%s",
                (appointment_id,),
            )
            conn.commit()
            cursor.close()
            current.status = AppointmentStatus.CANCELLED
            current.version += 1
            return current
        except (NotFound,):
            raise
        except Exception as exc:
            conn.rollback()
            raise self._translate(exc, "取消预约") from exc
        finally:
            conn.close()

    def delete(self, appointment_id: int) -> bool:
        """物理删除（仅运维/数据清理用）。"""
        conn = self._connect()
        try:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM appointment WHERE id=%s", (appointment_id,))
            conn.commit()
            deleted = cursor.rowcount > 0
            cursor.close()
            return deleted
        except Exception as exc:
            conn.rollback()
            raise self._translate(exc, "删除预约") from exc
        finally:
            conn.close()

    def list_all(self, limit: int = 100) -> List[Appointment]:
        conn = self._connect()
        try:
            cursor = conn.cursor(dictionary=True)
            cursor.execute(
                f"SELECT {self._SELECT_COLUMNS} FROM appointment ORDER BY id DESC LIMIT %s",
                (limit,),
            )
            rows = cursor.fetchall()
            cursor.close()
            return [self._row_to_appointment(row) for row in rows]
        except Exception as exc:
            raise self._translate(exc, "查询预约列表") from exc
        finally:
            conn.close()


class InMemoryAppointmentRepository(AppointmentRepository):
    """内存实现 —— MySQL 不可用时的降级路径（数据标记为 dirty，后台可补写）"""

    def __init__(self) -> None:
        self._rows: List[Appointment] = []
        self._seq = 0
        self._lock = threading.Lock()

    def find_duplicate(self, appointment: Appointment) -> Optional[Appointment]:
        with self._lock:
            for row in self._rows:
                if (
                    row.key() == appointment.key()
                    and row.status != AppointmentStatus.CANCELLED
                ):
                    return row
        return None

    def find_by_key(self, appointment: Appointment) -> Optional[Appointment]:
        with self._lock:
            for row in reversed(self._rows):
                if row.key() == appointment.key():
                    return row
        return None

    def save(self, appointment: Appointment) -> Appointment:
        with self._lock:
            if self.find_duplicate(appointment) is not None:
                raise UniqueConflict("已存在相同的预约记录")
            self._seq += 1
            appointment.id = self._seq
            self._rows.append(appointment)
            return appointment

    def create_if_absent(self, appointment: Appointment) -> Tuple[Appointment, bool]:
        with self._lock:
            for row in self._rows:
                if (
                    row.key() == appointment.key()
                    and row.status != AppointmentStatus.CANCELLED
                ):
                    return row, False
            self._seq += 1
            appointment.id = self._seq
            self._rows.append(appointment)
            return appointment, True

    def cancel(self, appointment_id: int) -> Appointment:
        with self._lock:
            for row in self._rows:
                if row.id == appointment_id:
                    if row.status == AppointmentStatus.CANCELLED:
                        from xiaozhi.repositories.errors import InvalidState

                        raise InvalidState("该预约已取消，无需重复取消")
                    row.status = AppointmentStatus.CANCELLED
                    row.version += 1
                    return row
        raise NotFound(f"预约记录不存在（id={appointment_id}）")

    def delete(self, appointment_id: int) -> bool:
        with self._lock:
            before = len(self._rows)
            self._rows = [r for r in self._rows if r.id != appointment_id]
            return len(self._rows) < before

    def list_all(self, limit: int = 100) -> List[Appointment]:
        with self._lock:
            return list(reversed(self._rows))[:limit]
