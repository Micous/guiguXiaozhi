"""请求级运行上下文 —— 一次 ``AgentEngine.run()`` 的生命周期与预算单一所有者。

对应《主体Agent生产化架构最终整改方案》Phase 1 / 批次 B1：

* ``deadline_monotonic`` 由入口创建一次，后续检索、模型、工具、持久化都读取
  **同一份剩余预算**，不允许每个阶段重新计时；
* ``RunContext`` 承载一次请求的身份与运行信息，``TurnState`` 仍只承载本轮
  跨阶段业务状态，两者不合并；
* 取消只表示「调用方不再等待」，**不表示远端写操作已取消**。写工具一旦可能
  已经提交，必须由业务侧保留 ``STARTED`` 并转 ``UNKNOWN`` 待对账，不能因为
  这里被取消就认定副作用没发生，更不能自动重试。

本模块是纯数据 + 纯函数，不 import 任何基础设施 Adapter，也不持有事件循环资源。
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

__all__ = ["CancellationScope", "RunContext", "ExecutionBudget"]


class CancellationScope:
    """记录一次 Run 的取消事实（幂等）。

    第一次 ``cancel()`` 生效并锁住原因，后续调用不覆盖 —— 异常链里常常会有
    第二次、第三次取消，保留**最初**的原因才对排障有意义。
    """

    __slots__ = ("_reason", "_cancelled_at")

    def __init__(self) -> None:
        self._reason: Optional[str] = None
        self._cancelled_at: Optional[float] = None

    @property
    def cancelled(self) -> bool:
        return self._reason is not None

    @property
    def reason(self) -> Optional[str]:
        return self._reason

    @property
    def cancelled_at(self) -> Optional[float]:
        return self._cancelled_at

    def cancel(self, reason: str) -> None:
        """标记取消；重复调用不会改写原因。"""
        if self._reason is None:
            self._reason = reason
            self._cancelled_at = time.monotonic()

    def snapshot(self) -> Dict[str, Any]:
        return {"cancelled": self.cancelled, "reason": self._reason}


@dataclass(frozen=True)
class RunContext:
    """一次请求不可变的身份与运行信息。

    ``request_id`` 与 ``trace_id`` 各自独立：前者标识一次 HTTP 请求，后者是
    可观测性链路 ID；在 B1 之前的兼容映射里二者曾由 ``trace_id`` 兼任。

    ``deadline_monotonic`` 是**绝对**时刻（``monotonic`` 基准），不是时长；
    ``timeout_seconds=None`` 表示不限时，用 ``inf`` 表达，保证 ``expired``
    恒为 False，不需要在调用点到处写 ``if timeout is not None``。
    """

    run_id: str
    trace_id: str
    request_id: str
    session_id: str
    subject_id: str
    started_monotonic: float
    deadline_monotonic: float
    cancellation: CancellationScope

    @classmethod
    def create(
        cls,
        *,
        trace_id: str,
        session_id: str,
        subject_id: str = "anonymous",
        timeout_seconds: Optional[float] = None,
        now: Optional[float] = None,
    ) -> "RunContext":
        """在请求入口创建一次；``now`` 仅供测试注入确定性时钟。"""
        started = time.monotonic() if now is None else now
        timeout = float("inf") if not timeout_seconds else float(timeout_seconds)
        if timeout <= 0:
            raise ValueError("timeout_seconds 必须为正数或 None")
        return cls(
            run_id=f"run-{secrets.token_hex(12)}",
            trace_id=trace_id,
            request_id=f"req-{secrets.token_hex(12)}",
            session_id=session_id,
            subject_id=subject_id,
            started_monotonic=started,
            deadline_monotonic=started + timeout,
            cancellation=CancellationScope(),
        )

    @property
    def timeout_seconds(self) -> float:
        return self.deadline_monotonic - self.started_monotonic

    @property
    def elapsed_seconds(self) -> float:
        return max(0.0, time.monotonic() - self.started_monotonic)

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.deadline_monotonic - time.monotonic())

    @property
    def expired(self) -> bool:
        """与改造前 ``monotonic() - started > request_timeout`` 严格等价。

        刻意用 ``> 0`` 而不是 ``>= 0``：B1 是行为等价批次，边界等号不能
        顺手改变；``remaining_seconds`` 因为 ``max(0, ...)`` 会丢掉负号，
        所以这里直接读未截断的差值。
        """
        return time.monotonic() - self.deadline_monotonic > 0.0

    def as_span_attributes(self) -> Dict[str, Any]:
        """可观测字段；不含任何患者 PII（subject_id 已在上层脱敏口径内）。"""
        return {
            "run_id": self.run_id,
            "request_id": self.request_id,
            "trace_id": self.trace_id,
            "session_id": self.session_id,
            "deadline_seconds": None if self.timeout_seconds == float("inf") else self.timeout_seconds,
        }


class ExecutionBudget:
    """全 Run 唯一的预算读数口。

    各阶段拿到的都是**同一份** deadline 的剩余时间，``remaining_for(phase)``
    里的 ``phase`` 只用于留痕和诊断，不会给该阶段重新开一段独立预算。
    """

    __slots__ = ("_ctx",)

    def __init__(self, ctx: RunContext) -> None:
        self._ctx = ctx

    @property
    def context(self) -> RunContext:
        return self._ctx

    @property
    def time_exceeded(self) -> bool:
        return self._ctx.expired

    def remaining_for(self, phase: str) -> float:
        """返回该阶段可用的剩余秒数；不限时返回 ``inf``。"""
        if self._ctx.timeout_seconds == float("inf"):
            return float("inf")
        return self._ctx.remaining_seconds

    def snapshot(self) -> Dict[str, Any]:
        timeout = self._ctx.timeout_seconds
        return {
            "run_id": self._ctx.run_id,
            "elapsed_seconds": round(self._ctx.elapsed_seconds, 6),
            "remaining_seconds": None if timeout == float("inf") else round(self._ctx.remaining_seconds, 6),
            "timeout_seconds": None if timeout == float("inf") else timeout,
            "expired": self.time_exceeded,
            "cancellation": self._ctx.cancellation.snapshot(),
        }
