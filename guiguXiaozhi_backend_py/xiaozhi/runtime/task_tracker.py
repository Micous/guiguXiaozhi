"""任务生命周期追踪器 —— Phase 5（审计 P1-02）。

引擎与 ``TaskRepository`` 之间的薄封装。**核心原则：追踪失败绝不打断对话**。

理由：任务持久化是**可观测性**，不是业务依赖。审计 P1-02 要解决的是
"进程退出后无从回答这个任务执行到哪一步、副作用发生了没有"，
不是"因为追踪库挂了就不许用户聊天"。因此本模块的所有方法：
  * 捕获仓储异常 → 记 warning → 返回空结果；
  * 绝不向引擎抛异常。

副作用判定（``side_effect_committed``）：
    只有**写操作工具**（预约/取消）成功执行后才置位。查询类工具
    成功不算副作用（没有改变外部事实）。

崩溃恢复配合（验收 2）：
    工具执行**前**先落 STARTED；进程在工具执行中途退出时，
    恢复扫描会发现 STARTED 记录并标记为 UNKNOWN ——
    此时**必须先查业务事实**（预约表的唯一约束 + 原子查重写入兜底），
    不可盲目重试，否则会重复挂号。
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from xiaozhi.repositories.task_repo import (
    AgentExecution,
    AgentTask,
    TaskRepository,
    ToolExecution,
    hash_args,
    new_id,
    now_str,
)
from xiaozhi.runtime.write_tools import WRITE_TOOL_NAMES

logger = logging.getLogger(__name__)


class ToolStartStatus(str, enum.Enum):
    """``try_tool_started`` 的三态结果（B3-2，D1/D2 决策落点）。

    旧版 ``tool_started`` 把「未启用」和「落库失败」都吞成 ``None``，
    调用方无法区分「规则 8 不适用」和「关键记录不可写、必须拒绝写操作」。
    """

    RECORDED = "RECORDED"  # STARTED 已持久化，可以进入外部副作用阶段
    DISABLED = "DISABLED"  # 追踪未启用（task_tracker=None 或本轮未登记）
    FAILED = "FAILED"  # STARTED 落库失败：写操作必须被拒绝（方案 §4 规则 8）


@dataclass(frozen=True)
class ToolStartResult:
    """一次「工具执行前落 STARTED」的三态结果。"""

    status: ToolStartStatus
    tool_execution_id: Optional[str] = None


class TaskTracker:
    """把一次请求映射成可持久化的任务/执行/工具执行链。"""

    def __init__(self, repository: TaskRepository, *, model_id: str = "") -> None:
        self._repo = repository
        self._model_id = model_id
        self.task_id: Optional[str] = None
        self.execution_id: Optional[str] = None

    # ---- 生命周期 ----

    def begin(
        self,
        *,
        subject_id: str,
        session_id: str,
        idempotency_key: Optional[str] = None,
        status: str = "RUNNING",
    ) -> Tuple[Optional[str], Optional[str]]:
        """登记任务与执行尝试；返回 (task_id, execution_id)。

        幂等键重复时返回**已存在**的任务 id（验收 5：同一幂等键只产生
        一个逻辑任务）。
        """
        try:
            task = AgentTask(
                task_id=new_id(),
                subject_id=subject_id or "anonymous",
                session_id=session_id or "default",
                status=status,
                idempotency_key=idempotency_key,
            )
            created = self._repo.create_task(task)
            self.task_id = created.task_id
            execution = AgentExecution(
                execution_id=new_id(),
                task_id=created.task_id,
                attempt=1,
                model_id=self._model_id,
                status="RUNNING",
            )
            self._repo.create_execution(execution)
            self.execution_id = execution.execution_id
            self._event("task_started", {"task_id": created.task_id})
            return self.task_id, self.execution_id
        except Exception as exc:  # 追踪失败不打断对话
            logger.warning("任务登记失败（忽略，不影响对话）: %s: %s", type(exc).__name__, exc)
            return None, None

    def finish(self, status: str = "SUCCEEDED") -> None:
        """收尾：execution 与 task 终态化。"""
        if not self.task_id:
            return
        try:
            if self.execution_id:
                self._repo.finish_execution(self.execution_id, status)
            self._repo.finish_task(self.task_id, status)
            self._event("task_finished", {"status": status})
        except Exception as exc:
            logger.warning("任务收尾失败（忽略）: %s: %s", type(exc).__name__, exc)

    # ---- 工具执行 ----

    def try_tool_started(
        self, tool_call_id: str, tool_name: str, args: Dict[str, Any]
    ) -> ToolStartResult:
        """工具执行**前**落 STARTED 的三态入口（B3-2 主 API）。

        * ``RECORDED``：已持久化，返回 ``tool_execution_id``；
        * ``DISABLED``：追踪未启用（未装配或本轮未登记）——D1 决策 A：
          灰度关闭时写操作照常放行，事件序列与关闭前完全一致；
        * ``FAILED``：落库失败 —— 写操作必须被调用方拒绝（规则 8），
          只读操作放行但必须记指标与告警（D2 决策）。

        本方法**永不抛异常**（追踪失败不打断对话），失败语义由状态表达。
        """
        if not self.task_id or not self.execution_id:
            return ToolStartResult(ToolStartStatus.DISABLED)
        try:
            record = ToolExecution(
                tool_execution_id=new_id(),
                execution_id=self.execution_id,
                task_id=self.task_id,
                tool_call_id=str(tool_call_id),
                tool_name=str(tool_name),
                args_hash=hash_args(args or {}),
                status="STARTED",
                started_at=now_str(),
            )
            self._repo.record_tool_started(record)
            return ToolStartResult(
                ToolStartStatus.RECORDED, record.tool_execution_id
            )
        except Exception as exc:
            logger.warning(
                "工具执行前落库失败 tool=%s call=%s: %s: %s",
                tool_name,
                tool_call_id,
                type(exc).__name__,
                exc,
            )
            return ToolStartResult(ToolStartStatus.FAILED)

    def tool_started(
        self, tool_call_id: str, tool_name: str, args: Dict[str, Any]
    ) -> Optional[str]:
        """旧版兼容入口：仅返回 ``tool_execution_id``（RECORDED）或 ``None``。

        DISABLED 与 FAILED 都映射为 ``None`` —— 需要区分两者时必须改用
        :meth:`try_tool_started`（B3-2 之后所有执行链路均已迁移）。
        """
        result = self.try_tool_started(tool_call_id, tool_name, args)
        return result.tool_execution_id

    def tool_finished(
        self,
        tool_execution_id: Optional[str],
        tool_name: str,
        *,
        success: bool,
    ) -> None:
        """工具执行后落终态；写工具成功 → side_effect_committed=True。"""
        if not tool_execution_id:
            return
        try:
            committed = bool(success and tool_name in WRITE_TOOL_NAMES)
            status = "SUCCEEDED" if success else "FAILED"
            self._repo.record_tool_finished(
                tool_execution_id, status, side_effect_committed=committed
            )
            self._event(
                "tool_finished",
                {"tool": tool_name, "status": status, "side_effect_committed": committed},
            )
        except Exception as exc:
            logger.warning("工具执行后落库失败（忽略）tool=%s: %s", tool_name, exc)

    # ---- 事件 ----

    def _event(self, event_type: str, payload: Optional[Dict] = None) -> None:
        if not self.task_id:
            return
        try:
            self._repo.append_event(self.task_id, event_type, payload)
        except Exception as exc:
            logger.warning("任务事件落库失败（忽略）: %s: %s", type(exc).__name__, exc)

    def event(self, event_type: str, payload: Optional[Dict] = None) -> None:
        """对外暴露的事件记录入口。"""
        self._event(event_type, payload)
