"""预约业务服务 —— Phase 3a（路线图 3a：抽出预约状态机）。

**为什么要有这一层**

改造前预约的**业务规则散在工具层**（``tools/appointment_tools.py``）：
查重、写入、查号源、字段错位防护、取消 —— 全挤在一个 ``call()`` 里。
带来的问题：

* 工具层同时管**协议**（入参/出参/给模型的文案）和**业务**（状态流转）；
* 状态流转无处安放 —— 取消是物理 DELETE，"这条预约取消过吗"事后无从回答；
* 业务规则无法被非工具入口复用（未来的 HIS 对账 Job、后台补偿都需要它）；
* 改一条业务规则要动工具层，而工具层是模型交互最敏感的地方。

本服务把**业务**抽出来，工具层退化成"参数校验 + 调服务 + 转述结果"。

**状态机**

.. code-block:: text

              ┌──────────┐
              │ REQUESTED│  已受理
              └────┬─────┘
                   ▼
              ┌──────────┐
              │ HOLDING  │  已预占号源
              └────┬─────┘
       ┌───────────┼───────────┐
       ▼           ▼           ▼
  ┌─────────┐ ┌──────────┐ ┌────────┐
  │CONFIRMED│ │ UNKNOWN  │ │CANCELLED│
  │  已确认  │ │ 结果核实中│ │  已取消 │
  └─────────┘ └────┬─────┘ └────────┘
                   │
                   ▼
             对账 Job（Phase 3b，依赖真实 HIS）

``CONFIRMED`` / ``CANCELLED`` 是终态。``UNKNOWN`` 表示"外部结果不确定"，
**必须**对账而不是当成成功 —— 路线图原则 3「宁可说核实中，不说成功」。

**与外部号源的关系**：本服务只管**本地**状态。真实 HIS 的预占/确认/对账是
Phase 3b 的 ``his_client.py`` + 对账 Job；那时本服务就是它们的状态落点。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from xiaozhi.repositories.appointment_repo import (
    Appointment,
    AppointmentRepository,
    AppointmentStatus,
)
from xiaozhi.repositories.errors import (
    InvalidState,
    NotFound,
    RepositoryError,
    RepositoryUnavailable,
    UniqueConflict,
)
from xiaozhi.service.slot_service import SlotAvailability, SlotService

logger = logging.getLogger(__name__)


class BookingOutcome(str):
    """预约结果类型。"""

    CREATED = "created"
    ALREADY_EXISTS = "already_exists"
    NO_SLOT = "no_slot"
    REPOSITORY_UNAVAILABLE = "repository_unavailable"
    FAILED = "failed"
    # Phase 4：结果未核实（Phase 3b 真实 HIS 的"扣号源成功但落单结果未知"
    # 对账语义）。Mock 链路暂无生产者；工具层据此返回
    # authoritative_status=UNKNOWN，Response Gate 换固定中性文案。
    UNKNOWN = "unknown"


@dataclass
class BookingResult:
    """一次预约请求的业务结果。

    ``outcome`` 决定工具层怎么转述给用户；
    ``message`` 是**业务事实**（不含模型的自由发挥）。
    """

    outcome: str
    message: str
    appointment: Optional[Appointment] = None
    availability: Optional[SlotAvailability] = None

    @property
    def succeeded(self) -> bool:
        return self.outcome == BookingOutcome.CREATED


@dataclass
class CancellationResult:
    """一次取消请求的业务结果。"""

    outcome: str
    message: str
    appointment: Optional[Appointment] = None

    @property
    def succeeded(self) -> bool:
        return self.outcome == BookingOutcome.CREATED


class AppointmentService:
    """预约业务服务 —— 承载状态机与业务规则。

    工具层只做：参数校验 → 调本服务 → 把 ``BookingResult`` 转述给模型。
    """

    def __init__(
        self,
        repository: AppointmentRepository,
        slot_service: Optional[SlotService],
    ) -> None:
        self._repository = repository
        self._slot_service = slot_service

    # ---------- 预约 ----------

    def book(
        self,
        *,
        username: str,
        id_card: str,
        department: str,
        date: str,
        time: str,
        doctor_name: Optional[str] = None,
    ) -> BookingResult:
        """创建预约。

        流程：查号源 → 原子"查重 + 写入" → 返回业务事实。
        **不生成给用户看的文案之外的东西** —— 文案由工具层转述。
        """
        # ---- 0. 配置防御：book 依赖号源服务（cancel 路径不依赖，可为 None）----
        if self._slot_service is None:
            raise RuntimeError(
                "AppointmentService.book 需要 slot_service；"
                "仅取消场景请直接调用 cancel()。"
            )

        # ---- 1. 号源 ----
        availability = self._slot_service.check(department, date, time, doctor_name)
        if not availability.available:
            return BookingResult(
                outcome=BookingOutcome.NO_SLOT,
                message=availability.message,
                availability=availability,
            )

        doctor = doctor_name or (availability.doctors[0] if availability.doctors else None)
        appointment = Appointment(
            username=username,
            id_card=id_card,
            department=department,
            date=date,
            time=time,
            doctor_name=doctor,
            status=AppointmentStatus.CONFIRMED,
        )

        # ---- 2. 原子查重 + 写入 ----
        try:
            saved, created = self._repository.create_if_absent(appointment)
        except UniqueConflict:
            # 并发下另一个请求抢先写入 → 业务规则冲突，不是系统故障
            logger.info("预约撞唯一约束（并发重复）：%s", appointment.key())
            return BookingResult(
                outcome=BookingOutcome.ALREADY_EXISTS,
                message="已存在相同的预约记录。",
                appointment=appointment,
            )
        except RepositoryUnavailable as exc:
            logger.warning("预约写入时数据存储不可用：%s", exc)
            return BookingResult(
                outcome=BookingOutcome.REPOSITORY_UNAVAILABLE,
                message="数据存储暂时不可用，请稍后重试。",
            )
        except RepositoryError as exc:
            logger.warning("预约写入失败：%s", exc)
            return BookingResult(
                outcome=BookingOutcome.FAILED,
                message="预约写入失败，请稍后重试。",
            )

        if not created:
            return BookingResult(
                outcome=BookingOutcome.ALREADY_EXISTS,
                message="已存在相同的预约记录。",
                appointment=saved,
            )
        return BookingResult(
            outcome=BookingOutcome.CREATED,
            message="预约成功。",
            appointment=saved,
        )

    # ---------- 取消 ----------

    def cancel(
        self,
        *,
        username: str,
        id_card: str,
        department: str,
        date: str,
        time: str,
    ) -> CancellationResult:
        """取消预约 —— **状态迁移**，保留审计痕迹。

        取消后同一时段可以重新预约（唯一约束只约束未取消的记录）。
        """
        probe = Appointment(
            username=username,
            id_card=id_card,
            department=department,
            date=date,
            time=time,
        )
        try:
            existing = self._repository.find_duplicate(probe)
        except RepositoryUnavailable as exc:
            logger.warning("查询待取消预约时数据存储不可用：%s", exc)
            return CancellationResult(
                outcome=BookingOutcome.REPOSITORY_UNAVAILABLE,
                message="数据存储暂时不可用，请稍后重试。",
            )
        except RepositoryError as exc:
            logger.warning("查询待取消预约失败：%s", exc)
            return CancellationResult(
                outcome=BookingOutcome.FAILED,
                message="查询预约失败，请稍后重试。",
            )

        if existing is None or existing.id is None:
            # find_duplicate 只看未取消记录 —— 这里再按任意状态查一次，
            # 区分"从未约过"（未找到）和"约过但已取消"（无需重复取消）
            already_cancelled = self._find_cancelled(probe)
            if already_cancelled is not None:
                return CancellationResult(
                    outcome=BookingOutcome.FAILED,
                    message="该预约已取消，无需重复取消。",
                    appointment=already_cancelled,
                )
            return CancellationResult(
                outcome=BookingOutcome.FAILED,
                message="未找到可取消的预约记录，请核对姓名、身份证号、科室、日期和时段。",
            )

        try:
            cancelled = self._repository.cancel(existing.id)
        except NotFound:
            return CancellationResult(
                outcome=BookingOutcome.FAILED,
                message="未找到可取消的预约记录，请核对姓名、身份证号、科室、日期和时段。",
            )
        except InvalidState as exc:
            return CancellationResult(
                outcome=BookingOutcome.FAILED,
                message=str(exc),
                appointment=existing,
            )
        except RepositoryUnavailable as exc:
            logger.warning("取消预约时数据存储不可用：%s", exc)
            return CancellationResult(
                outcome=BookingOutcome.REPOSITORY_UNAVAILABLE,
                message="数据存储暂时不可用，请稍后重试。",
            )
        except RepositoryError as exc:
            logger.warning("取消预约失败：%s", exc)
            return CancellationResult(
                outcome=BookingOutcome.FAILED,
                message="取消预约失败，请稍后重试。",
            )

        return CancellationResult(
            outcome=BookingOutcome.CREATED,
            message="取消预约成功。",
            appointment=cancelled,
        )

    def _find_cancelled(self, probe: Appointment) -> Optional[Appointment]:
        """按任意状态查记录，命中且已取消时返回（查询失败时返回 None 不阻断）。"""
        try:
            row = self._repository.find_by_key(probe)
        except RepositoryError as exc:
            logger.warning("查询预约状态失败：%s", exc)
            return None
        if row is not None and row.status == AppointmentStatus.CANCELLED:
            return row
        return None

    # ---------- 查询 ----------

    def find(self, appointment_id: int) -> Optional[Appointment]:
        """按 id 查预约（运维/审计用）。"""
        try:
            for row in self._repository.list_all(limit=1000):
                if row.id == appointment_id:
                    return row
        except RepositoryError as exc:
            logger.warning("查询预约失败：%s", exc)
        return None
