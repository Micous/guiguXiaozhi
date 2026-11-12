"""UNKNOWN 对账 Job —— Phase 3b（审计 P0-03 的收尾动作）。

**为什么需要对账**

审计 P0-03 的风险原文：

    外部 HIS 成功但本地超时，或本地成功但外部失败时，结果可能不确定。

真实链路里"扣号源成功、落单失败/超时"是**真实存在的中间态**
（参考项目的 HIS 本身就是「先扣号源再落单」，用 Seata 保证一致）。
我们这边调用 HIS 落单时如果超时，就**不知道** HIS 到底挂上了没有：

* HIS 挂上了 → 我们说"失败" → 用户重试 → **重复挂号**；
* HIS 没挂上 → 我们说"成功" → 用户白跑一趟。

所以超时必须落 ``UNKNOWN``（"结果核实中"），由本 Job **查 HIS 的业务事实**
来收敛：

.. code-block:: text

    UNKNOWN ──查单──> HIS 有此单且状态正常  → CONFIRMED
                 ├─ HIS 有此单但已取消       → CANCELLED
                 └─ HIS 无此单（404）        → FAILED（确实没挂上）

**三条纪律**

1. **只收敛有据可查的**：查不到外部单号（本地没有 external_booking_id）
   → 不动，保持 UNKNOWN（不能凭"查不到"就判失败）。
2. **查单本身不确定**（HIS 又超时了）→ 保持 UNKNOWN，attempts+1；
   连续失败达阈值 → 告警（需要人工介入），**但不改变状态**。
3. **可重入**：同一批记录重复跑不会产生副作用（状态迁移是幂等的，
   乐观锁保证并发跑不会互相覆盖）。

**运行方式**：定时任务（cron / k8s CronJob）调用 ``ReconcileJob.run(limit)``。
也可以在每次预约请求收尾时顺手跑一小批（低频），但**不要**在请求里同步等
对账结果 —— 用户等不起。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from xiaozhi.repositories.appointment_repo import (
    Appointment,
    AppointmentRepository,
    AppointmentStatus,
)
from xiaozhi.repositories.errors import InvalidState, NotFound, RepositoryError
from xiaozhi.service.his_client import (
    HisBookingStatus,
    HisRejectedError,
    HisUncertainError,
)

logger = logging.getLogger(__name__)

#: HIS 侧挂号单状态 → 本地状态。不同医院编码不同，用**包含匹配**，
#: 命中任一关键词即认为该状态（保守：宁可多留一轮 UNKNOWN，
#: 也不要误判成 CONFIRMED）。
_CONFIRMED_MARKERS = ("REGISTER", "SUCCESS", "FINISH", "COMPLETE", "已挂号")
_CANCELLED_MARKERS = ("CANCEL", "REVOKE", "REFUND", "已取消", "退号")


@dataclass
class ReconcileEntry:
    """单条对账结论。"""

    appointment_id: int
    resolved: bool
    from_status: str = ""
    to_status: str = ""
    message: str = ""
    #: 跳过（无据可查 / 需人工介入）—— 状态未变，但语义不同于"仍不确定"
    skipped: bool = False


@dataclass
class ReconcileReport:
    """一批对账的汇总。"""

    scanned: int = 0
    resolved: List[ReconcileEntry] = field(default_factory=list)
    unresolved: List[ReconcileEntry] = field(default_factory=list)
    skipped: List[ReconcileEntry] = field(default_factory=list)
    errored: List[ReconcileEntry] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {
            "scanned": self.scanned,
            "resolved_count": len(self.resolved),
            "unresolved_count": len(self.unresolved),
            "skipped_count": len(self.skipped),
            "errored_count": len(self.errored),
            "resolved": [
                {
                    "appointment_id": e.appointment_id,
                    "from_status": e.from_status,
                    "to_status": e.to_status,
                    "message": e.message,
                }
                for e in self.resolved
            ],
            "unresolved": [
                {"appointment_id": e.appointment_id, "message": e.message}
                for e in self.unresolved
            ],
        }


class ReconcileJob:
    """UNKNOWN 对账 Job。"""

    def __init__(
        self,
        repository: AppointmentRepository,
        slot_service,
        *,
        max_attempts: int = 5,
    ) -> None:
        self._repository = repository
        self._slot_service = slot_service
        self._max_attempts = max(1, int(max_attempts))

    def run(self, limit: int = 20) -> ReconcileReport:
        """扫描并对账一批 UNKNOWN 记录。"""
        report = ReconcileReport()
        try:
            pending = self._repository.list_by_status(
                AppointmentStatus.UNKNOWN, limit=limit
            )
        except RepositoryError as exc:
            logger.warning("对账扫描失败（存储不可用）：%s", exc)
            return report

        report.scanned = len(pending)
        for appointment in pending:
            entry = self._reconcile_one(appointment)
            if entry.resolved:
                report.resolved.append(entry)
            elif entry.skipped:
                # 跳过的判定必须在 unresolved 之前 —— "跳过"也是 UNKNOWN 状态，
                # 但语义不同（无据可查 / 需人工介入），不能混进 unresolved
                report.skipped.append(entry)
            elif entry.to_status == AppointmentStatus.UNKNOWN.value:
                report.unresolved.append(entry)
            else:
                report.errored.append(entry)
        if report.resolved or report.unresolved:
            logger.info(
                "对账完成：扫描 %d 条，收敛 %d 条，仍不确定 %d 条",
                report.scanned,
                len(report.resolved),
                len(report.unresolved),
            )
        return report

    # ---------- 单条 ----------

    def _reconcile_one(self, appointment: Appointment) -> ReconcileEntry:
        entry = ReconcileEntry(
            appointment_id=appointment.id or 0,
            resolved=False,
            from_status=appointment.status.value,
            to_status=appointment.status.value,
        )

        # 纪律 1：没有外部单号 → 原则上无据可查，保持 UNKNOWN。
        # 例外：预占超时时我们把**幂等键**留在 external_hold_id 里
        # （``idem:`` 前缀），HIS 侧存着"幂等键 → 预占 → 挂号单"的关联，
        # 凭它可以查证，避免 UNKNOWN 永远收敛不了（真机演练暴露的死结）。
        by_key = ""
        if not appointment.external_booking_id:
            hold_ref = getattr(appointment, "external_hold_id", "") or ""
            if hold_ref.startswith("idem:") and hasattr(
                self._slot_service, "query_booking_by_idempotency_key"
            ):
                by_key = hold_ref[len("idem:"):]
            else:
                entry.message = "跳过：缺少外部挂号单号，无据可查"
                if hold_ref.startswith("idem:"):
                    entry.message += "（号源服务不支持按幂等键查单）"
                entry.skipped = True
                return entry

        # 纪律 2：尝试次数超阈值 → 告警但不改变状态
        if appointment.reconcile_attempts >= self._max_attempts:
            entry.message = (
                f"跳过：对账连续失败 {appointment.reconcile_attempts} 次，"
                "需人工介入"
            )
            entry.skipped = True
            logger.error(
                "预约 %s 对账连续失败 %d 次，需人工介入",
                appointment.id,
                appointment.reconcile_attempts,
            )
            return entry

        try:
            if by_key:
                status = self._slot_service.query_booking_by_idempotency_key(by_key)
            else:
                status = self._slot_service.query_booking(
                    appointment.external_booking_id
                )
        except HisUncertainError as exc:
            # 查单结果不确定 → 保持 UNKNOWN，attempts+1
            self._bump_attempts(appointment)
            entry.message = f"HIS 查单结果不确定：{exc}"
            return entry
        except HisRejectedError as exc:
            self._bump_attempts(appointment)
            entry.message = f"HIS 查单被拒绝：{exc}"
            return entry
        except Exception as exc:  # 兜底：任何异常都不改变状态
            self._bump_attempts(appointment)
            entry.message = f"查单异常：{exc}"
            logger.warning("预约 %s 对账异常：%s", appointment.id, exc)
            return entry

        target = self._decide(status)
        if target is None:
            # HIS 有单但状态不认识 → 保持 UNKNOWN（保守）
            self._bump_attempts(appointment)
            entry.message = f"HIS 状态未识别：{status.status or '空'}"
            return entry

        try:
            self._repository.update_status(
                appointment.id,
                target,
                expected_version=appointment.version,
                # 按幂等键查到单时，把查到的外部单号补回本地
                # （否则下次取消预约还是退不了号）
                external_booking_id=(
                    status.external_booking_id
                    if by_key and status.found and status.external_booking_id
                    else appointment.external_booking_id
                ),
                reconciled_at=datetime.now(),
                reconcile_attempts=0,
            )
        except (NotFound, InvalidState) as exc:
            entry.message = f"跳过：状态更新失败（{exc}）"
            entry.skipped = True
            return entry
        except RepositoryError as exc:
            self._bump_attempts(appointment)
            entry.message = f"状态更新失败：{exc}"
            return entry

        entry.resolved = True
        entry.to_status = target.value
        entry.message = f"对账收敛：{appointment.status.value} → {target.value}"
        return entry

    # ---------- 决策 ----------

    @staticmethod
    def _decide(status: HisBookingStatus) -> Optional[AppointmentStatus]:
        """查单结果 → 本地目标状态。

        ``None`` = 无法判定（保持 UNKNOWN）。
        """
        if not status.found:
            # HIS 明确没有这条单 → 确实没挂上 → 失败
            return AppointmentStatus.FAILED
        raw = (status.status or "").upper()
        if any(marker in raw for marker in _CANCELLED_MARKERS):
            return AppointmentStatus.CANCELLED
        if any(marker in raw for marker in _CONFIRMED_MARKERS):
            return AppointmentStatus.CONFIRMED
        return None

    def _bump_attempts(self, appointment: Appointment) -> None:
        """对账失败 → attempts+1（不改变状态）。"""
        try:
            self._repository.update_status(
                appointment.id,
                appointment.status,
                expected_version=appointment.version,
                reconcile_attempts=appointment.reconcile_attempts + 1,
            )
        except (NotFound, InvalidState):
            # 版本变了（有人并发改过）→ 下一轮重扫即可
            return
        except RepositoryError as exc:
            logger.warning("更新对账计数失败（预约 %s）：%s", appointment.id, exc)
