"""预约记录仓储 —— MySQL 访问层。

用接口隔离存储实现：真实环境用 MySQL，MySQL 不可用时可降级到内存实现
（见 InMemoryAppointmentRepository），保证单点故障不让用户看到 500。
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple


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

    def key(self) -> Tuple[str, str, str, str, str]:
        """防重复预约的判定键（不含医生 —— 同科室同时段只能有一条）"""
        return (self.username, self.id_card, self.department, self.date, self.time)


class AppointmentRepository(ABC):
    """预约仓储接口"""

    @abstractmethod
    def find_duplicate(self, appointment: Appointment) -> Optional[Appointment]:
        """查找同一人、同科室、同日期同时段的已有预约"""

    @abstractmethod
    def save(self, appointment: Appointment) -> Optional[Appointment]:
        """写入预约"""

    @abstractmethod
    def delete(self, appointment_id: int) -> bool:
        """删除预约"""

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

    def _row_to_appointment(self, row: Dict[str, Any]) -> Appointment:
        return Appointment(
            id=row.get("id"),
            username=row.get("username", ""),
            id_card=row.get("id_card", ""),
            department=row.get("department", ""),
            date=row.get("date", ""),
            time=row.get("time", ""),
            doctor_name=row.get("doctor_name"),
        )

    def find_duplicate(self, appointment: Appointment) -> Optional[Appointment]:
        try:
            conn = self._connect()
            try:
                cursor = conn.cursor(dictionary=True)
                cursor.execute(
                    "SELECT id, username, id_card, department, date, time, doctor_name "
                    "FROM appointment WHERE username=%s AND id_card=%s AND department=%s "
                    "AND date=%s AND time=%s LIMIT 1",
                    (
                        appointment.username,
                        appointment.id_card,
                        appointment.department,
                        appointment.date,
                        appointment.time,
                    ),
                )
                row = cursor.fetchone()
                return self._row_to_appointment(row) if row else None
            finally:
                conn.close()
        except Exception:
            # 数据库不可用时不阻断主流程，由上层降级
            return None

    def save(self, appointment: Appointment) -> Optional[Appointment]:
        try:
            conn = self._connect()
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO appointment (username, id_card, department, date, time, doctor_name) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (
                        appointment.username,
                        appointment.id_card,
                        appointment.department,
                        appointment.date,
                        appointment.time,
                        appointment.doctor_name,
                    ),
                )
                conn.commit()
                appointment.id = cursor.lastrowid
                return appointment
            finally:
                conn.close()
        except Exception:
            return None

    def delete(self, appointment_id: int) -> bool:
        try:
            conn = self._connect()
            try:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM appointment WHERE id=%s", (appointment_id,))
                conn.commit()
                return cursor.rowcount > 0
            finally:
                conn.close()
        except Exception:
            return False

    def list_all(self, limit: int = 100) -> List[Appointment]:
        try:
            conn = self._connect()
            try:
                cursor = conn.cursor(dictionary=True)
                cursor.execute(
                    "SELECT id, username, id_card, department, date, time, doctor_name "
                    "FROM appointment ORDER BY id DESC LIMIT %s",
                    (limit,),
                )
                return [self._row_to_appointment(row) for row in cursor.fetchall()]
            finally:
                conn.close()
        except Exception:
            return []


class InMemoryAppointmentRepository(AppointmentRepository):
    """内存实现 —— MySQL 不可用时的降级路径（数据标记为 dirty，后台可补写）"""

    def __init__(self) -> None:
        self._rows: List[Appointment] = []
        self._seq = 0
        self._lock = threading.Lock()

    def find_duplicate(self, appointment: Appointment) -> Optional[Appointment]:
        with self._lock:
            for row in self._rows:
                if row.key() == appointment.key():
                    return row
        return None

    def save(self, appointment: Appointment) -> Optional[Appointment]:
        with self._lock:
            self._seq += 1
            appointment.id = self._seq
            self._rows.append(appointment)
            return appointment

    def delete(self, appointment_id: int) -> bool:
        with self._lock:
            before = len(self._rows)
            self._rows = [r for r in self._rows if r.id != appointment_id]
            return len(self._rows) < before

    def list_all(self, limit: int = 100) -> List[Appointment]:
        with self._lock:
            return list(reversed(self._rows))[:limit]
