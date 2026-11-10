"""号源服务 —— 对接医院 HIS / 统一挂号平台的扩展点。

**这一层是 Mock**：真实环境要访问医院的排班接口，本地调不通，
所以用 MockSlotService 在本地内存里模拟排班规则（确定性、可复现）。

替换成真实接口只需两步，工具层代码不动：
  1. 新建 HisSlotService implements SlotService，方法里调医院接口；
  2. 在 application.py 里把 MockSlotService 换成你的实现。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, List, Optional

from xiaozhi.service.doctor_info import DoctorInfo
from xiaozhi.service.doctor_service import DoctorService


@dataclass
class SlotAvailability:
    """号源查询结果"""

    available: bool
    message: str
    doctors: List[str] = field(default_factory=list)
    remaining: int = 0

    @classmethod
    def none(cls, message: str) -> "SlotAvailability":
        return cls(available=False, message=message)


class SlotService(ABC):
    """号源查询接口"""

    @abstractmethod
    def check(
        self,
        department: str,
        date_str: str,
        time_slot: str,
        doctor_name: Optional[str] = None,
    ) -> SlotAvailability:
        """查询某科室在某日期、时段的号源"""


class MockSlotService(SlotService):
    """【Mock】本地模拟医院排班

    规则是**确定性**的（同样的入参永远同样的结果），方便联调与回归：
      * 门诊时间为周一至周五，周末不出诊；
      * 同一科室的医生按顺序轮流值上午 / 下午，保证每个时段都有医生；
      * 每位医生当天 1~5 个号，由姓名散列决定；
      * 约 10% 的概率当日号源已约满（覆盖"号源已满"分支）。
    """

    MAX_QUOTA = 5
    FULL_RATIO = 10

    def __init__(self, doctor_service: DoctorService) -> None:
        self.doctor_service = doctor_service

    def check(
        self,
        department: str,
        date_str: str,
        time_slot: str,
        doctor_name: Optional[str] = None,
    ) -> SlotAvailability:
        try:
            target = date.fromisoformat((date_str or "").strip())
        except ValueError:
            return SlotAvailability.none(
                "日期格式无法识别（应为 2026-11-20 这样的格式），请重新提供就诊日期。"
            )

        slot = self._normalize_slot(time_slot)
        if slot is None:
            return SlotAvailability.none("时段只能是“上午”或“下午”，请确认后再预约。")

        if target.weekday() >= 5:
            return SlotAvailability.none(
                f"{department} 在 {date_str} 是周末，门诊不出诊，请改约工作日。"
            )

        doctors = self.doctor_service.list_doctors(department)
        if not doctors:
            return SlotAvailability.none(
                f"本院暂无【{department}】这个科室，请确认科室名称，或让我帮你推荐合适的科室。"
            )

        # 出诊：同一科室按顺序轮流值上午 / 下午
        on_duty: List[DoctorInfo] = []
        for index, doctor in enumerate(doctors):
            if self._is_on_duty(index, len(doctors), slot):
                on_duty.append(doctor)

        if doctor_name and doctor_name.strip():
            on_duty = [d for d in on_duty if d.name in doctor_name]
            if not on_duty:
                return SlotAvailability.none(
                    f"医生【{doctor_name}】在 {date_str} {slot}不出诊，"
                    f"可预约的医生有：{'、'.join(d.name for d in doctors)}。"
                )

        if not on_duty:
            return SlotAvailability.none(
                f"{department} 在 {date_str} {slot}暂无出诊安排。"
            )

        # 逐个医生算剩余号，剔除已约满的
        available: List[str] = []
        remaining = 0
        for doctor in on_duty:
            quota = self._quota_of(doctor, target)
            if quota > 0:
                available.append(doctor.name)
                remaining += quota

        if not available:
            return SlotAvailability.none(
                f"{department} 在 {date_str} {slot}的号源已全部约满，建议换个日期或时段再试。"
            )

        return SlotAvailability(
            available=True,
            message=(
                f"{department} 在 {date_str} {slot}有号源，共 {remaining} 个号，"
                f"出诊医生：{'、'.join(available)}。"
            ),
            doctors=available,
            remaining=remaining,
        )

    # ---------- 内部规则 ----------

    def _is_on_duty(self, index: int, total: int, slot: str) -> bool:
        """上午/下午错开；只有一位医生时两个时段都出诊"""
        if total == 1:
            return True
        morning = index % 2 == 0
        return (slot == "上午") == morning

    def _quota_of(self, doctor: DoctorInfo, target: date) -> int:
        """该医生当天的剩余号数（0 表示已约满）"""
        key = f"{doctor.name}|{doctor.department}|{target.isoformat()}"
        digest = sum(ord(c) * (i + 1) for i, c in enumerate(key))
        if digest % self.FULL_RATIO == 0:
            return 0
        return 1 + digest % self.MAX_QUOTA

    def _normalize_slot(self, time_slot: str) -> Optional[str]:
        if not time_slot:
            return None
        if "上午" in time_slot or "早上" in time_slot:
            return "上午"
        if "下午" in time_slot:
            return "下午"
        return None
