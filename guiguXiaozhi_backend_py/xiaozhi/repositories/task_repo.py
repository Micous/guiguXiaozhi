"""任务生命周期仓储 —— Phase 5（审计 P1-02）。

引擎的轮次/工具次数/失败计数原本都是请求内局部变量，进程退出后
"这个任务执行到哪一步、副作用发生了没有"无从回答。本模块把审计
4.x 的四层数据结构（AgentTask / AgentExecution / ToolExecution /
TaskEvent）落到 MySQL，并提供崩溃恢复扫描。

恢复原则（审计 P1-02 验收 2）：
    工具执行前先落 STARTED；恢复扫描发现 STARTED 且
    ``side_effect_committed=1`` 的记录时**只标记 UNKNOWN 并上报**，
    绝不自动重试 —— 副作用是否真的发生必须先查业务事实
    （预约表有 uk_appointment_active 唯一约束 + create_if_absent
    原子查重写入兜底，重发同参数会得到"已有预约"而不是重复挂号）。

灰度：``task_tracking_enabled=false``（默认）时组合根不装配本仓储，
引擎 ``task_tracker=None``，行为与 Phase 4 完全一致。
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from xiaozhi.repositories.errors import RepositoryUnavailable


def now_str() -> str:
    return datetime.now().isoformat(sep=" ", timespec="seconds")


def _parse_dt(value: str) -> Optional[datetime]:
    """解析时间戳；失败返回 None（视为陈旧）。"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        try:
            return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None


def new_id() -> str:
    return uuid.uuid4().hex


def hash_args(args: Dict) -> str:
    """工具参数哈希（用于恢复时比对"同一件事"）"""
    return hashlib.sha256(
        json.dumps(args, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


@dataclass
class AgentTask:
    task_id: str
    subject_id: str
    session_id: str
    status: str  # RUNNING / WAITING_APPROVAL / SUCCEEDED / FAILED / INTERRUPTED / CANCELLED
    idempotency_key: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""


@dataclass
class AgentExecution:
    execution_id: str
    task_id: str
    attempt: int = 1
    model_id: str = ""
    status: str = "RUNNING"  # RUNNING / SUCCEEDED / FAILED / INTERRUPTED
    started_at: str = ""
    ended_at: Optional[str] = None


@dataclass
class ToolExecution:
    tool_execution_id: str
    execution_id: str
    task_id: str
    tool_call_id: str
    tool_name: str
    args_hash: str
    status: str = "STARTED"  # STARTED / SUCCEEDED / FAILED / UNKNOWN
    side_effect_committed: bool = False
    result_ref: Optional[str] = None
    started_at: str = ""
    ended_at: Optional[str] = None


@dataclass
class TaskEvent:
    event_id: str
    task_id: str
    sequence: int
    event_type: str
    payload_json: str = ""
    created_at: str = ""


@dataclass
class RecoveryReport:
    """恢复扫描结果 —— 调用方据此决定是否查业务事实（绝不自动重试）。"""

    interrupted_executions: List[str] = field(default_factory=list)
    unknown_tool_executions: List[str] = field(default_factory=list)
    cancelled_waiting_tasks: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {
            "interrupted_executions": self.interrupted_executions,
            "unknown_tool_executions": self.unknown_tool_executions,
            "cancelled_waiting_tasks": self.cancelled_waiting_tasks,
        }


class TaskRepository(ABC):
    @abstractmethod
    def create_task(self, task: AgentTask) -> AgentTask:
        """登记任务；幂等键重复时返回**已存在**的任务（验收 5：只产生一个逻辑任务）"""

    @abstractmethod
    def finish_task(self, task_id: str, status: str) -> None:
        """终态化任务（SUCCEEDED / FAILED / CANCELLED）"""

    @abstractmethod
    def create_execution(self, execution: AgentExecution) -> None: ...

    @abstractmethod
    def finish_execution(self, execution_id: str, status: str) -> None: ...

    @abstractmethod
    def record_tool_started(self, record: ToolExecution) -> None:
        """工具执行**前**落 STARTED —— 崩溃恢复时据此判断「可能有副作用」"""

    @abstractmethod
    def record_tool_finished(
        self, tool_execution_id: str, status: str, *, side_effect_committed: bool = False,
        result_ref: Optional[str] = None,
    ) -> None: ...

    @abstractmethod
    def append_event(self, task_id: str, event_type: str, payload: Optional[Dict] = None) -> int:
        """追加事件；返回 task 内单调递增的 sequence"""

    @abstractmethod
    def recover_stale(self, stale_after_seconds: float) -> RecoveryReport:
        """崩溃恢复扫描：

        * RUNNING 且超时无更新的 execution → INTERRUPTED，其 task → FAILED；
        * 仍处 STARTED 的 tool_execution → UNKNOWN（**只标记**，副作用是否
          发生由调用方查业务事实）；
        * WAITING_APPROVAL 且超时的 task → CANCELLED（验收 3：过期关闭）。
        """


class InMemoryTaskRepository(TaskRepository):
    """离线单测用。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tasks: Dict[str, AgentTask] = {}
        self._idem: Dict[str, str] = {}
        self._executions: Dict[str, AgentExecution] = {}
        self._tool_execs: Dict[str, ToolExecution] = {}
        self._events: List[TaskEvent] = []
        self._seq: Dict[str, int] = {}

    def create_task(self, task: AgentTask) -> AgentTask:
        with self._lock:
            if task.idempotency_key and task.idempotency_key in self._idem:
                return self._tasks[self._idem[task.idempotency_key]]
            task.created_at = task.created_at or now_str()
            task.updated_at = task.updated_at or task.created_at
            self._tasks[task.task_id] = task
            if task.idempotency_key:
                self._idem[task.idempotency_key] = task.task_id
            return task

    def finish_task(self, task_id: str, status: str) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task:
                task.status = status
                task.updated_at = now_str()

    def create_execution(self, execution: AgentExecution) -> None:
        with self._lock:
            execution.started_at = execution.started_at or now_str()
            self._executions[execution.execution_id] = execution

    def finish_execution(self, execution_id: str, status: str) -> None:
        with self._lock:
            execution = self._executions.get(execution_id)
            if execution:
                execution.status = status
                execution.ended_at = now_str()

    def record_tool_started(self, record: ToolExecution) -> None:
        with self._lock:
            record.started_at = record.started_at or now_str()
            self._tool_execs[record.tool_execution_id] = record

    def record_tool_finished(
        self, tool_execution_id: str, status: str, *, side_effect_committed: bool = False,
        result_ref: Optional[str] = None,
    ) -> None:
        with self._lock:
            record = self._tool_execs.get(tool_execution_id)
            if record:
                record.status = status
                record.side_effect_committed = side_effect_committed
                record.result_ref = result_ref
                record.ended_at = now_str()

    def append_event(self, task_id: str, event_type: str, payload: Optional[Dict] = None) -> int:
        with self._lock:
            seq = self._seq.get(task_id, 0) + 1
            self._seq[task_id] = seq
            self._events.append(
                TaskEvent(
                    event_id=new_id(),
                    task_id=task_id,
                    sequence=seq,
                    event_type=event_type,
                    payload_json=json.dumps(payload or {}, ensure_ascii=False, default=str),
                    created_at=now_str(),
                )
            )
            return seq

    def recover_stale(self, stale_after_seconds: float) -> RecoveryReport:
        """内存版恢复扫描（语义与 MySQL 版一致）：只标记，绝不重试。"""
        report = RecoveryReport()
        cutoff = datetime.now() - timedelta(seconds=max(0, int(stale_after_seconds)))
        with self._lock:
            for execution in self._executions.values():
                if execution.status != "RUNNING":
                    continue
                started = _parse_dt(execution.started_at)
                if started and started > cutoff:
                    continue
                execution.status = "INTERRUPTED"
                execution.ended_at = now_str()
                report.interrupted_executions.append(execution.execution_id)
                task = self._tasks.get(execution.task_id)
                if task and task.status in ("RUNNING", "WAITING_APPROVAL"):
                    task.status = "FAILED"
                    task.updated_at = now_str()
            for record in self._tool_execs.values():
                if record.status != "STARTED":
                    continue
                started = _parse_dt(record.started_at)
                if started and started > cutoff:
                    continue
                record.status = "UNKNOWN"
                report.unknown_tool_executions.append(record.tool_execution_id)
            for task in self._tasks.values():
                if task.status != "WAITING_APPROVAL":
                    continue
                updated = _parse_dt(task.updated_at)
                if updated and updated > cutoff:
                    continue
                task.status = "CANCELLED"
                task.updated_at = now_str()
                report.cancelled_waiting_tasks.append(task.task_id)
        return report

    # ---- 测试辅助（非 ABC 契约）----
    def get_task(self, task_id: str) -> Optional[AgentTask]:
        return self._tasks.get(task_id)

    def get_execution(self, execution_id: str) -> Optional[AgentExecution]:
        return self._executions.get(execution_id)

    def get_tool_execution(self, tool_execution_id: str) -> Optional[ToolExecution]:
        return self._tool_execs.get(tool_execution_id)

    def events_of(self, task_id: str) -> List[TaskEvent]:
        return [e for e in self._events if e.task_id == task_id]


class MySQLTaskRepository(TaskRepository):
    """MySQL 实现（多实例共享任务状态，重启不丢）。"""

    def __init__(self, host: str, port: int, user: str, password: str, database: str) -> None:
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

    def ensure_tables(self) -> None:
        """组合根在 task_tracking_enabled=true 时调用；失败直接抛出。"""
        conn = self._connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "CREATE TABLE IF NOT EXISTS agent_tasks ("
                "  task_id VARCHAR(64) NOT NULL PRIMARY KEY,"
                "  subject_id VARCHAR(64) NOT NULL,"
                "  session_id VARCHAR(128) NOT NULL,"
                "  status VARCHAR(32) NOT NULL,"
                "  idempotency_key VARCHAR(191) NULL,"
                "  deadline_at DATETIME NULL,"
                "  created_at DATETIME NOT NULL,"
                "  updated_at DATETIME NOT NULL,"
                "  UNIQUE KEY uk_task_idempotency (idempotency_key),"
                "  INDEX idx_task_subject (subject_id),"
                "  INDEX idx_task_status (status)"
                ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
            )
            cursor.execute(
                "CREATE TABLE IF NOT EXISTS agent_executions ("
                "  execution_id VARCHAR(64) NOT NULL PRIMARY KEY,"
                "  task_id VARCHAR(64) NOT NULL,"
                "  attempt INT NOT NULL DEFAULT 1,"
                "  runtime_version VARCHAR(32) NOT NULL DEFAULT '',"
                "  model_id VARCHAR(64) NOT NULL DEFAULT '',"
                "  status VARCHAR(32) NOT NULL,"
                "  started_at DATETIME NOT NULL,"
                "  ended_at DATETIME NULL,"
                "  INDEX idx_exec_task (task_id),"
                "  INDEX idx_exec_status (status)"
                ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
            )
            cursor.execute(
                "CREATE TABLE IF NOT EXISTS tool_executions ("
                "  tool_execution_id VARCHAR(64) NOT NULL PRIMARY KEY,"
                "  execution_id VARCHAR(64) NOT NULL,"
                "  task_id VARCHAR(64) NOT NULL,"
                "  tool_call_id VARCHAR(128) NOT NULL,"
                "  tool_name VARCHAR(128) NOT NULL,"
                "  args_hash VARCHAR(64) NOT NULL,"
                "  status VARCHAR(32) NOT NULL,"
                "  side_effect_committed TINYINT NOT NULL DEFAULT 0,"
                "  result_ref VARCHAR(255) NULL,"
                "  started_at DATETIME NOT NULL,"
                "  ended_at DATETIME NULL,"
                "  INDEX idx_tool_exec_task (task_id),"
                "  INDEX idx_tool_exec_status (status)"
                ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
            )
            cursor.execute(
                "CREATE TABLE IF NOT EXISTS task_events ("
                "  event_id VARCHAR(64) NOT NULL PRIMARY KEY,"
                "  task_id VARCHAR(64) NOT NULL,"
                "  sequence INT NOT NULL,"
                "  event_type VARCHAR(64) NOT NULL,"
                "  payload_json TEXT NULL,"
                "  created_at DATETIME NOT NULL,"
                "  UNIQUE KEY uk_event_sequence (task_id, sequence)"
                ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
            )
            conn.commit()
            cursor.close()
        finally:
            conn.close()

    def create_task(self, task: AgentTask) -> AgentTask:
        conn = self._connect()
        try:
            cursor = conn.cursor()
            task.created_at = task.created_at or now_str()
            task.updated_at = task.updated_at or task.created_at
            try:
                cursor.execute(
                    "INSERT INTO agent_tasks (task_id, subject_id, session_id, status,"
                    " idempotency_key, created_at, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (
                        task.task_id, task.subject_id, task.session_id, task.status,
                        task.idempotency_key, task.created_at, task.updated_at,
                    ),
                )
                conn.commit()
                cursor.close()
                return task
            except Exception:
                conn.rollback()
                cursor.close()
                # 幂等键重复 → 返回已存在的任务（验收 5）
                if task.idempotency_key:
                    cursor2 = conn.cursor(dictionary=True)
                    cursor2.execute(
                        "SELECT task_id, subject_id, session_id, status,"
                        " idempotency_key, created_at, updated_at"
                        " FROM agent_tasks WHERE idempotency_key=%s LIMIT 1",
                        (task.idempotency_key,),
                    )
                    row = cursor2.fetchone()
                    cursor2.close()
                    if row:
                        return AgentTask(
                            task_id=row["task_id"],
                            subject_id=row["subject_id"],
                            session_id=row["session_id"],
                            status=row["status"],
                            idempotency_key=row["idempotency_key"],
                            created_at=str(row["created_at"]),
                            updated_at=str(row["updated_at"]),
                        )
                raise
        finally:
            conn.close()

    def finish_task(self, task_id: str, status: str) -> None:
        conn = self._connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE agent_tasks SET status=%s, updated_at=%s WHERE task_id=%s",
                (status, now_str(), task_id),
            )
            conn.commit()
            cursor.close()
        finally:
            conn.close()

    def create_execution(self, execution: AgentExecution) -> None:
        conn = self._connect()
        try:
            cursor = conn.cursor()
            execution.started_at = execution.started_at or now_str()
            cursor.execute(
                "INSERT INTO agent_executions (execution_id, task_id, attempt,"
                " model_id, status, started_at) VALUES (%s,%s,%s,%s,%s,%s)",
                (
                    execution.execution_id, execution.task_id, execution.attempt,
                    execution.model_id, execution.status, execution.started_at,
                ),
            )
            conn.commit()
            cursor.close()
        finally:
            conn.close()

    def finish_execution(self, execution_id: str, status: str) -> None:
        conn = self._connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE agent_executions SET status=%s, ended_at=%s WHERE execution_id=%s",
                (status, now_str(), execution_id),
            )
            conn.commit()
            cursor.close()
        finally:
            conn.close()

    def record_tool_started(self, record: ToolExecution) -> None:
        conn = self._connect()
        try:
            cursor = conn.cursor()
            record.started_at = record.started_at or now_str()
            cursor.execute(
                "INSERT INTO tool_executions (tool_execution_id, execution_id, task_id,"
                " tool_call_id, tool_name, args_hash, status, started_at)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    record.tool_execution_id, record.execution_id, record.task_id,
                    record.tool_call_id, record.tool_name, record.args_hash,
                    record.status, record.started_at,
                ),
            )
            conn.commit()
            cursor.close()
        finally:
            conn.close()

    def record_tool_finished(
        self, tool_execution_id: str, status: str, *, side_effect_committed: bool = False,
        result_ref: Optional[str] = None,
    ) -> None:
        conn = self._connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE tool_executions SET status=%s, side_effect_committed=%s,"
                " result_ref=%s, ended_at=%s WHERE tool_execution_id=%s",
                (status, 1 if side_effect_committed else 0, result_ref, now_str(), tool_execution_id),
            )
            conn.commit()
            cursor.close()
        finally:
            conn.close()

    def append_event(self, task_id: str, event_type: str, payload: Optional[Dict] = None) -> int:
        conn = self._connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM task_events WHERE task_id=%s",
                (task_id,),
            )
            seq = int(cursor.fetchone()[0])
            cursor.execute(
                "INSERT INTO task_events (event_id, task_id, sequence, event_type,"
                " payload_json, created_at) VALUES (%s,%s,%s,%s,%s,%s)",
                (
                    new_id(), task_id, seq, event_type,
                    json.dumps(payload or {}, ensure_ascii=False, default=str), now_str(),
                ),
            )
            conn.commit()
            cursor.close()
            return seq
        finally:
            conn.close()

    def recover_stale(self, stale_after_seconds: float) -> RecoveryReport:
        """崩溃恢复扫描（只标记，绝不重试）。失败抛 RepositoryUnavailable。"""
        report = RecoveryReport()
        conn = self._connect()
        try:
            cursor = conn.cursor(dictionary=True)
            # 1) 超时 RUNNING executions → INTERRUPTED；其 task → FAILED
            cursor.execute(
                "SELECT execution_id, task_id, started_at FROM agent_executions"
                " WHERE status='RUNNING'"
                " AND started_at < NOW() - INTERVAL %s SECOND",
                (max(1, int(stale_after_seconds)),),
            )
            stale_execs = cursor.fetchall()
            for row in stale_execs:
                cursor2 = conn.cursor()
                cursor2.execute(
                    "UPDATE agent_executions SET status='INTERRUPTED', ended_at=%s"
                    " WHERE execution_id=%s AND status='RUNNING'",
                    (now_str(), row["execution_id"]),
                )
                cursor2.execute(
                    "UPDATE agent_tasks SET status='FAILED', updated_at=%s"
                    " WHERE task_id=%s AND status IN ('RUNNING','WAITING_APPROVAL')",
                    (now_str(), row["task_id"]),
                )
                cursor2.close()
                report.interrupted_executions.append(row["execution_id"])
            # 2) STARTED tool_executions → UNKNOWN（只标记，副作用须查业务事实）
            cursor.execute(
                "SELECT tool_execution_id FROM tool_executions WHERE status='STARTED'"
                " AND started_at < NOW() - INTERVAL %s SECOND",
                (max(1, int(stale_after_seconds)),),
            )
            stale_tools = cursor.fetchall()
            for row in stale_tools:
                cursor2 = conn.cursor()
                cursor2.execute(
                    "UPDATE tool_executions SET status='UNKNOWN'"
                    " WHERE tool_execution_id=%s AND status='STARTED'",
                    (row["tool_execution_id"],),
                )
                cursor2.close()
                report.unknown_tool_executions.append(row["tool_execution_id"])
            # 3) 超时 WAITING_APPROVAL tasks → CANCELLED（过期关闭）
            cursor.execute(
                "SELECT task_id FROM agent_tasks WHERE status='WAITING_APPROVAL'"
                " AND updated_at < NOW() - INTERVAL %s SECOND",
                (max(1, int(stale_after_seconds)),),
            )
            stale_wait = cursor.fetchall()
            for row in stale_wait:
                cursor2 = conn.cursor()
                cursor2.execute(
                    "UPDATE agent_tasks SET status='CANCELLED', updated_at=%s"
                    " WHERE task_id=%s AND status='WAITING_APPROVAL'",
                    (now_str(), row["task_id"]),
                )
                cursor2.close()
                report.cancelled_waiting_tasks.append(row["task_id"])
            conn.commit()
            cursor.close()
            return report
        except Exception as exc:
            raise RepositoryUnavailable(f"恢复扫描失败: {exc}") from exc
        finally:
            conn.close()
