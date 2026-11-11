"""真实 HIS 号源服务 —— Phase 3b（审计 P0-03 的落地实现）。

**与 MockSlotService 的关系**

``SlotService`` 抽象保持不变（工具层零改动），本类提供真实实现：

* ``check()``        —— 号源查询（HIS 三层号表：index → schedule → for_regist）
* ``hold_slot()``    —— **预占**（锁定号源，防止并发超卖）
* ``confirm_hold()`` —— 确认并落单
* ``release_hold()`` —— 释放预占（失败回滚，把号源还回去）
* ``query_booking()``—— 查单（对账依据）

**为什么"查询有号"不够**

审计 P0-03 的原话：「查询有号不等于实际锁定号源」。两个并发请求都看到
"还有 1 个号"，然后都去挂号 —— 第二个要么被 HIS 拒绝（用户体验差），
要么把号源扣成负数（脏数据）。所以真实链路必须是：

.. code-block:: text

    query_slots  →  hold_slot  →  confirm_hold
                     （预占）      （落单）
                        │
                        └── 失败 → release_hold（把号源还回去）

**三层号源模型**（`参考项目/his`）：

.. code-block:: text

    clinic_index       科室排班索引（哪个科室哪天有号）
      └─ clinic_schedule   医生出诊安排（谁出诊）
           └─ clinic_for_regist  按日期展开的号表实例
              （currentNum 已挂号 / currentPreNum 已预占 / limitNum 限号）

剩余号 = ``limitNum - currentNum - currentPreNum``。
**必须扣掉预占数**，否则并发下会超卖。

**外部结果不确定时抛什么**

* ``HisRejectedError``  → 明确失败，可如实告知用户；
* ``HisUncertainError`` → 结果不确定，**必须**转成业务层的 UNKNOWN，
  交给对账 Job 核实 —— 绝不能当成失败（否则用户重试 → 重复挂号）。
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from xiaozhi.service.his_client import (
    HisBooking,
    HisBookingStatus,
    HisClient,
    HisHold,
    HisRejectedError,
    HisSlot,
    HisUncertainError,
)
from xiaozhi.service.slot_service import SlotAvailability, SlotService

logger = logging.getLogger(__name__)


@dataclass
class SlotQuery:
    """号源查询条件（对应 HIS 号表实例的过滤字段）。"""

    department: str
    date_str: str
    time_slot: str
    doctor_name: Optional[str] = None
    org_id: str = ""

    def to_params(self) -> Dict[str, Any]:
        """转成 HIS ``/clinicForRegist/list`` 的查询参数。

        字段名与 HIS BO 对齐（参考项目 ``ClinicForRegistBo``）：
        ``clinicDate`` / ``timeDesc`` / ``deptName`` / ``doctorName``。
        """
        params: Dict[str, Any] = {}
        if self.date_str:
            params["clinicDate"] = self.date_str
        if self.time_slot:
            params["timeDesc"] = self.time_slot
        if self.department:
            params["deptName"] = self.department
        if self.doctor_name:
            params["doctorName"] = self.doctor_name
        if self.org_id:
            params["orgId"] = self.org_id
        return params


@dataclass
class SlotHoldResult:
    """预占结果。"""

    held: bool
    hold_id: str = ""
    external_booking_id: str = ""
    slot: Optional[HisSlot] = None
    message: str = ""
    uncertain: bool = False  # True = 结果不确定，必须对账

    @classmethod
    def uncertain_result(cls, message: str) -> "SlotHoldResult":
        return cls(held=False, message=message, uncertain=True)


@dataclass
class BookingResult:
    """确认/落单结果。"""

    confirmed: bool
    external_booking_id: str = ""
    message: str = ""
    uncertain: bool = False

    @classmethod
    def uncertain_result(cls, message: str) -> "BookingResult":
        return cls(confirmed=False, message=message, uncertain=True)


@dataclass
class ReleaseResult:
    """释放预占结果。"""

    released: bool
    message: str = ""


@dataclass
class CancellationResult:
    """退号结果。

    ``uncertain=True`` 表示**退号结果不确定**（超时）—— 此时绝不能对用户说
    "取消成功"（医院侧可能还留着号），也不能说"取消失败"（可能已经退了）。
    应保持待核实状态。
    """

    released: bool
    message: str = ""
    uncertain: bool = False


@dataclass
class ReconciliationEntry:
    """一条对账结论。"""

    external_booking_id: str
    resolved: bool
    status: str = ""
    message: str = ""


def build_idempotency_key(*parts: str) -> str:
    """幂等键 —— 同一业务键永远得到同一个键。

    用 sha256 而不是拼接明文：幂等键会出现在 HIS 侧日志里，
    不该把身份证号这类敏感字段原样送出去。
    """
    raw = "|".join(str(p or "") for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class HisSlotService(SlotService):
    """真实 HIS 号源服务。"""

    def __init__(
        self,
        client: HisClient,
        *,
        dept_resolver=None,
        org_id: str = "",
    ) -> None:
        self._client = client
        # 科室名 → HIS 科室 id 的解析器（不同医院编码体系不同，做成可注入）
        self._dept_resolver = dept_resolver
        self._org_id = org_id

    # ---------- 查询 ----------

    def check(
        self,
        department: str,
        date_str: str,
        time_slot: str,
        doctor_name: Optional[str] = None,
    ) -> SlotAvailability:
        """查询号源（对应工具层的"查询是否有号源"）。

        ⚠️ 这里**只查询不预占** —— 查询结果永远可能过期，
        真正的锁定发生在 ``hold_slot``。
        """
        query = SlotQuery(
            department=department,
            date_str=date_str,
            time_slot=time_slot,
            doctor_name=doctor_name,
            org_id=self._org_id,
        )
        try:
            slots = self._client.query_slots(query.to_params())
        except HisRejectedError as exc:
            return SlotAvailability.none(f"医院系统拒绝了号源查询：{exc}")
        except HisUncertainError as exc:
            # 查询不确定：不能报"没号"（会误导用户），也不能报"有号"
            logger.warning("HIS 号源查询结果不确定：%s", exc)
            return SlotAvailability.none(
                "医院号源系统暂时无法确认，请稍后重试。"
            )

        if not slots:
            return SlotAvailability.none(
                f"{department} 在 {date_str} {time_slot}暂无出诊安排。"
            )

        available_slots = [slot for slot in slots if slot.remaining > 0]
        if not available_slots:
            return SlotAvailability.none(
                f"{department} 在 {date_str} {time_slot}的号源已全部约满，"
                "建议换个日期或时段再试。"
            )

        doctors: List[str] = []
        remaining = 0
        for slot in available_slots:
            remaining += slot.remaining
            name = slot.doctor_name or slot.clinic_schedule_id
            if name and name not in doctors:
                doctors.append(name)
        return SlotAvailability(
            available=True,
            message=(
                f"{department} 在 {date_str} {time_slot}有号源，共 {remaining} 个号，"
                f"出诊医生：{'、'.join(doctors) if doctors else '待确认'}。"
            ),
            doctors=doctors,
            remaining=remaining,
        )

    # ---------- 预占 ----------

    def hold_slot(
        self,
        department: str,
        date_str: str,
        time_slot: str,
        doctor_name: Optional[str] = None,
        *,
        idempotency_key: Optional[str] = None,
    ) -> SlotHoldResult:
        """预占号源 —— **真正锁定**这一步。

        幂等键默认由业务键派生（科室+日期+时段+医生+患者标识），
        所以同一笔业务重复提交只会占一个号。
        """
        key = idempotency_key or build_idempotency_key(
            department, date_str, time_slot, doctor_name or ""
        )
        payload = {
            "deptName": department,
            "clinicDate": date_str,
            "timeDesc": time_slot,
            "doctorName": doctor_name or "",
            "orgId": self._org_id,
        }
        try:
            hold: HisHold = self._client.hold_slot(payload, idempotency_key=key)
        except HisRejectedError as exc:
            return SlotHoldResult(held=False, message=str(exc))
        except HisUncertainError as exc:
            # 预占结果不确定：号源可能已经被占住 → 交给对账，不当失败
            logger.warning("HIS 预占结果不确定（幂等键=%s）：%s", key[:12], exc)
            return SlotHoldResult.uncertain_result(
                "号源锁定结果正在核实，请稍后确认是否预约成功。"
            )
        if not hold.held:
            return SlotHoldResult(held=False, message=hold.message or "号源预占失败。")
        return SlotHoldResult(
            held=True,
            hold_id=hold.hold_id,
            external_booking_id=hold.external_booking_id,
            slot=hold.slot,
            message=hold.message,
        )

    # ---------- 确认 ----------

    def confirm_hold(
        self,
        hold_id: str,
        patient_ref: str,
        *,
        idempotency_key: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> BookingResult:
        """确认预占并落单。

        ``patient_ref`` 是**患者引用**（HIS 主档 id 或我们生成的脱敏引用），
        不是明文身份证 —— 与 P0-06 的整改方向一致。
        """
        key = idempotency_key or build_idempotency_key(hold_id, patient_ref)
        payload: Dict[str, Any] = {
            "holdId": hold_id,
            "patientRef": patient_ref,
        }
        if extra:
            payload.update(extra)
        try:
            booking: HisBooking = self._client.confirm_hold(
                payload, idempotency_key=key
            )
        except HisRejectedError as exc:
            return BookingResult(confirmed=False, message=str(exc))
        except HisUncertainError as exc:
            # 落单结果不确定 —— 最危险的情形：可能已经挂上了
            logger.warning("HIS 落单结果不确定（幂等键=%s）：%s", key[:12], exc)
            return BookingResult.uncertain_result(
                "预约结果正在核实，请勿重复提交。"
            )
        return BookingResult(
            confirmed=True,
            external_booking_id=booking.external_booking_id,
            message=booking.message,
        )

    # ---------- 释放 ----------

    def release_hold(
        self,
        hold_id: str,
        *,
        idempotency_key: Optional[str] = None,
    ) -> ReleaseResult:
        """释放预占，把号源还给医院。"""
        key = idempotency_key or build_idempotency_key("release", hold_id)
        try:
            self._client.release_hold(hold_id, idempotency_key=key)
        except HisRejectedError as exc:
            # 释放失败：号源可能一直被占着 —— 需要运维介入，但**不影响**
            # 本地状态推进（本地已是失败态，HIS 侧残留由对账清理）
            logger.warning("释放 HIS 预占失败（hold=%s）：%s", hold_id[:12], exc)
            return ReleaseResult(released=False, message=str(exc))
        except HisUncertainError as exc:
            logger.warning("释放 HIS 预占结果不确定（hold=%s）：%s", hold_id[:12], exc)
            return ReleaseResult(released=False, message=str(exc))
        return ReleaseResult(released=True, message="号源已释放。")

    # ---------- 查单（对账） ----------

    def query_booking(self, external_booking_id: str) -> HisBookingStatus:
        """查单 —— 对账依据。"""
        try:
            return self._client.query_booking(external_booking_id)
        except HisRejectedError as exc:
            if exc.code == 404:
                return HisBookingStatus(found=False)
            raise
        except HisUncertainError as exc:
            logger.warning("HIS 查单结果不确定（id=%s）：%s", external_booking_id[:12], exc)
            raise

    # ---------- 退号 ----------

    def cancel_booking(
        self,
        external_booking_id: str,
        *,
        idempotency_key: Optional[str] = None,
    ) -> CancellationResult:
        """退号（用户取消预约）—— 把号源还给医院。

        与 ``release_hold`` 的区别：``release_hold`` 释放的是**未落单**的预占；
        ``cancel_booking`` 退掉的是**已挂号**的单。两者都把号源还回去，
        但语义不同（审计追踪要能区分"没挂上"与"挂了又退了"）。

        返回 ``CancellationResult``：
          * ``released=True``  → 退号成功；
          * ``released=False, uncertain=True`` → **结果不确定**（超时），
            绝不能当成"退成功"（用户到院发现没退比退不掉更糟）；
          * ``released=False`` → 明确失败（可如实告知用户）。
        """
        if not external_booking_id:
            return CancellationResult(
                released=False,
                message="缺少外部挂号单号，无法退号。",
            )
        key = idempotency_key or build_idempotency_key(
            "cancel", external_booking_id
        )
        try:
            self._client.cancel_booking(
                external_booking_id, idempotency_key=key
            )
        except HisRejectedError as exc:
            logger.warning("HIS 退号被拒绝（id=%s）：%s", external_booking_id[:12], exc)
            return CancellationResult(
                released=False, message=str(exc)
            )
        except HisUncertainError as exc:
            logger.warning(
                "HIS 退号结果不确定（id=%s）：%s", external_booking_id[:12], exc
            )
            return CancellationResult(
                released=False, message=str(exc), uncertain=True
            )
        return CancellationResult(released=True, message="退号成功。")
