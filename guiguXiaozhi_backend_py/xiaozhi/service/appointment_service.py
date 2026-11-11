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

import hashlib
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


def _his_idempotency_key(
    operation: str,
    username: str,
    id_card: str,
    department: str,
    date: str,
    time: str,
) -> str:
    """HIS 幂等键 —— **按操作区分**。

    2026-11-11 真机暴露的设计缺陷：hold（预占）与 confirm（落单）
    最初共用同一个业务幂等键。HIS 侧按幂等键去重时，"确认"会命中
    "预占"的那条记录 → 号被占住但挂号单没落成（号泄漏）。

    修正：键里带操作前缀（``hold`` / ``confirm`` / ``release``），
    同一操作的重试仍然幂等，不同操作互不干扰。
    """
    return hashlib.sha256(
        "|".join(
            (operation, username, id_card, department, date, time)
        ).encode("utf-8")
    ).hexdigest()


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

    **真实 HIS 的两段式流程**（Phase 3b）

    号源服务支持预占语义（``HisSlotService``）时走：

        hold_slot（预占）→ 落库 HOLDING → confirm_hold（落单）
                                            ├─ 成功 → CONFIRMED
                                            └─ 不确定 → UNKNOWN（等对账）
                                            └─ 失败 → release_hold + FAILED

    号源服务只支持查询（``MockSlotService``）时保持一期行为：
    查号源 → 原子写入 CONFIRMED。**两条路径对工具层完全一致**，
    工具层只看 ``BookingOutcome``。
    """

    def __init__(
        self,
        repository: AppointmentRepository,
        slot_service: Optional[SlotService],
        *,
        patient_ref_salt: str = "",
    ) -> None:
        self._repository = repository
        self._slot_service = slot_service
        self._patient_ref_salt = patient_ref_salt

    def _supports_hold(self) -> bool:
        """号源服务是否支持预占/确认语义（真实 HIS）。"""
        return hasattr(self._slot_service, "hold_slot") and hasattr(
            self._slot_service, "confirm_hold"
        )

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

        # ---- 0.5 真实 HIS 走两段式（预占 → 落单）----
        # Mock 号源没有预占语义，仍走下面的一步式流程。
        if self._supports_hold():
            return self._book_with_hold(
                username=username,
                id_card=id_card,
                department=department,
                date=date,
                time=time,
                doctor_name=doctor_name,
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

    # ---------- 真实 HIS 两段式（Phase 3b） ----------

    def _book_with_hold(
        self,
        *,
        username: str,
        id_card: str,
        department: str,
        date: str,
        time: str,
        doctor_name: Optional[str] = None,
    ) -> BookingResult:
        """真实 HIS 预约：预占 → 落库 HOLDING → 确认落单。

        为什么必须两段式：审计 P0-03 的原话「查询有号不等于实际锁定号源」。
        两个并发请求都看到"还有 1 个号"就会都去挂号。

        状态推进：

        .. code-block:: text

            预占成功 → 落库 HOLDING → 确认成功 → CONFIRMED
                                     ├─ 确认不确定 → UNKNOWN（等对账 Job）
                                     ├─ 确认失败   → 释放预占 + FAILED
                                     └─ 落库失败   → 释放预占 + FAILED
        """
        patient_ref = Appointment.make_patient_ref(
            id_card, self._patient_ref_salt
        )

        # ---- 0.5 本地查重**前置**到预占之前 ----
        # 2026-11-11 真机暴露：先预占再查重，会"白预占再白释放"
        # （fake HIS 侧 hold=1 / release=1，用户白等一轮远端调用）。
        # 本地查重是**廉价**的（一次索引查询），远端预占是**昂贵**的
        # （跨网络、占真实号源）—— 便宜的检查必须先做。
        #
        # ⚠️ 这里仍有竞态（两个请求同时查到"无重复"→ 都去预占），
        # 但那由两道防线兜住：①HIS 侧预占原子性（第二个预占会失败）；
        # ②落库时 create_if_absent 的原子查重（唯一约束）。
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
            logger.warning("预约查重时数据存储不可用：%s", exc)
            return BookingResult(
                outcome=BookingOutcome.REPOSITORY_UNAVAILABLE,
                message="数据存储暂时不可用，请稍后重试。",
            )
        except RepositoryError as exc:
            logger.warning("预约查重失败：%s", exc)
            return BookingResult(
                outcome=BookingOutcome.FAILED,
                message="查询预约失败，请稍后重试。",
            )
        if existing is not None:
            return BookingResult(
                outcome=BookingOutcome.ALREADY_EXISTS,
                message="已存在相同的预约记录。",
                appointment=existing,
            )

        # ---- 1. 预占号源（真正锁定）----
        # ⚠️ 幂等键必须**按操作区分**：hold 与 confirm 是两个不同动作，
        # 共用一个键的话，HIS 侧按幂等键去重会让"确认"命中"预占"的记录
        # → 号被占住但单没落成（2026-11-11 真机暴露的设计缺陷）。
        hold = self._slot_service.hold_slot(
            department,
            date,
            time,
            doctor_name,
            idempotency_key=_his_idempotency_key(
                "hold", username, id_card, department, date, time
            ),
        )
        if getattr(hold, "uncertain", False):
            # 预占结果不确定：号源可能已被占住 → UNKNOWN，不能当失败
            logger.warning("HIS 预占结果不确定，进入 UNKNOWN：%s/%s %s", department, date, time)
            pending = self._persist_pending(
                username=username,
                id_card=id_card,
                department=department,
                date=date,
                time=time,
                doctor_name=doctor_name,
                patient_ref=patient_ref,
                status=AppointmentStatus.UNKNOWN,
                external_hold_id=getattr(hold, "hold_id", "") or None,
                external_booking_id=getattr(hold, "external_booking_id", "") or None,
                message="号源锁定结果正在核实，请稍后确认是否预约成功。",
            )
            # 落库成功与否都要报 UNKNOWN —— "结果不确定"是外部事实，
            # 不能因为本地写入成功就变成 CREATED（否则用户以为约上了）
            if pending.outcome in (
                BookingOutcome.CREATED,
                BookingOutcome.ALREADY_EXISTS,
            ):
                return BookingResult(
                    outcome=BookingOutcome.UNKNOWN,
                    message="号源锁定结果正在核实，请稍后确认是否预约成功。",
                    appointment=pending.appointment,
                )
            return pending
        if not hold.held:
            return BookingResult(
                outcome=BookingOutcome.NO_SLOT,
                message=hold.message or "号源预占失败，请换个时段再试。",
            )

        # ---- 2. 落库 HOLDING（先落库再确认：崩了能查到 HOLDING 去对账）----
        persisted = self._persist_pending(
            username=username,
            id_card=id_card,
            department=department,
            date=date,
            time=time,
            doctor_name=doctor_name,
            patient_ref=patient_ref,
            status=AppointmentStatus.HOLDING,
            external_hold_id=hold.hold_id or None,
            external_booking_id=hold.external_booking_id or None,
            message="预约处理中。",
        )
        if persisted.outcome not in (
            BookingOutcome.CREATED,
            BookingOutcome.ALREADY_EXISTS,
        ):
            # 落库失败 → 必须把号源还回去，否则号被白占
            self._release_hold(hold.hold_id)
            return persisted
        if persisted.outcome == BookingOutcome.ALREADY_EXISTS:
            # 本地已有同参数预约 → 不重复占号
            self._release_hold(hold.hold_id)
            return persisted

        appointment = persisted.appointment
        if appointment is None or appointment.id is None:
            self._release_hold(hold.hold_id)
            return BookingResult(
                outcome=BookingOutcome.FAILED,
                message="预约写入失败，请稍后重试。",
            )

        # ---- 3. 确认落单 ----
        confirm = self._slot_service.confirm_hold(
            hold.hold_id,
            patient_ref,
            idempotency_key=_his_idempotency_key(
                "confirm", username, id_card, department, date, time
            ),
        )
        if getattr(confirm, "uncertain", False):
            # 落单结果不确定 —— 最危险：可能已经挂上了。
            # 落 UNKNOWN 等对账，**绝不当失败**（否则用户重试 → 重复挂号）。
            logger.warning("HIS 落单结果不确定，进入 UNKNOWN：预约 id=%s", appointment.id)
            try:
                self._repository.update_status(
                    appointment.id,
                    AppointmentStatus.UNKNOWN,
                    expected_version=appointment.version,
                )
            except RepositoryError as exc:
                logger.warning("写入 UNKNOWN 状态失败（预约 id=%s）：%s", appointment.id, exc)
            return BookingResult(
                outcome=BookingOutcome.UNKNOWN,
                message="预约结果正在核实，请勿重复提交。",
                appointment=appointment,
            )

        if not confirm.confirmed:
            # 确认失败 → 释放预占（把号源还回去），本地收敛为 FAILED
            self._release_hold(hold.hold_id)
            try:
                failed = self._repository.update_status(
                    appointment.id,
                    AppointmentStatus.FAILED,
                    expected_version=appointment.version,
                )
            except RepositoryError as exc:
                logger.warning("写入 FAILED 状态失败（预约 id=%s）：%s", appointment.id, exc)
                failed = appointment
            return BookingResult(
                outcome=BookingOutcome.FAILED,
                message=confirm.message or "预约未成功，请稍后重试。",
                appointment=failed,
            )

        # ---- 4. 确认成功 → CONFIRMED ----
        try:
            confirmed = self._repository.update_status(
                appointment.id,
                AppointmentStatus.CONFIRMED,
                expected_version=appointment.version,
                external_booking_id=confirm.external_booking_id or None,
            )
        except RepositoryError as exc:
            # 落库失败但 HIS 已挂号 —— 保守起见报 UNKNOWN（对账能查出来）
            logger.warning("HIS 已挂号但本地状态更新失败（预约 id=%s）：%s", appointment.id, exc)
            return BookingResult(
                outcome=BookingOutcome.UNKNOWN,
                message="预约结果正在核实，请勿重复提交。",
                appointment=appointment,
            )
        return BookingResult(
            outcome=BookingOutcome.CREATED,
            message="预约成功。",
            appointment=confirmed,
        )

    def _persist_pending(
        self,
        *,
        username: str,
        id_card: str,
        department: str,
        date: str,
        time: str,
        doctor_name: Optional[str],
        patient_ref: str,
        status: AppointmentStatus,
        external_hold_id: Optional[str] = None,
        external_booking_id: Optional[str] = None,
        message: str = "",
    ) -> BookingResult:
        """写入中间态预约（HOLDING / UNKNOWN），返回业务结果。"""
        appointment = Appointment(
            username=username,
            id_card=id_card,
            department=department,
            date=date,
            time=time,
            doctor_name=doctor_name,
            status=status,
            patient_ref=patient_ref,
            external_hold_id=external_hold_id,
            external_booking_id=external_booking_id,
        )
        try:
            saved, created = self._repository.create_if_absent(appointment)
        except UniqueConflict:
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
            message=message,
            appointment=saved,
        )

    def _release_hold(self, hold_id: str) -> None:
        """释放预占（失败回滚）。失败只记日志 —— 号源残留由对账/运维清理。"""
        if not hold_id:
            return
        try:
            result = self._slot_service.release_hold(hold_id)
            if not getattr(result, "released", False):
                logger.warning("释放预占失败（hold=%s）：%s", hold_id[:12], result)
        except Exception as exc:  # 兜底：释放失败不能影响主流程结论
            logger.warning("释放预占异常（hold=%s）：%s", hold_id[:12], exc)

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
