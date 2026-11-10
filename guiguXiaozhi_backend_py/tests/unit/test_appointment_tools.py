"""预约工具层单测 —— 参数校验、字段错位防护、失败兜底。

覆盖 2026-11-10 真实联调发现的两个实打实的问题：
  1. 模型把**医生名**填进 username（患者姓名）→ 用医生名建了患者预约记录；
  2. 号源查不到时必须如实失败，不能给出"成功"式的返回值。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from xiaozhi.repositories.appointment_repo import Appointment  # noqa: E402
from xiaozhi.service.doctor_info import DoctorInfo  # noqa: E402
from xiaozhi.service.slot_service import SlotAvailability  # noqa: E402
from xiaozhi.tools.appointment_tools import BookAppointmentTool  # noqa: E402

VALID_ID = "110101199005054322"


class FakeDoctorService:
    def __init__(self, roster=None):
        self.roster = roster or {
            "神经内科": [
                DoctorInfo("王笠", "神经内科", "主任医师", "脑血管病"),
                DoctorInfo("崔丽英", "神经内科", "主任医师", "癫痫"),
            ]
        }

    def list_departments(self):
        return list(self.roster)

    def list_doctors(self, department):
        return list(self.roster.get(department, []))

    def find_doctor(self, department, name):
        for doctor in self.list_doctors(department):
            if doctor.name == name:
                return doctor
        return None


class FakeSlotService:
    def __init__(self, available=True, doctors=("王笠",)):
        self.available = available
        self.doctors = list(doctors)
        self.calls = []

    def check(self, department, date_str, time_slot, doctor_name=None):
        self.calls.append((department, date_str, time_slot, doctor_name))
        if not self.available:
            return SlotAvailability.none("号源已满")
        return SlotAvailability(
            available=True, message="有号源", doctors=self.doctors, remaining=3
        )


class FakeRepository:
    def __init__(self):
        self.saved = []

    def find_duplicate(self, appointment):
        return None

    def save(self, appointment):
        self.saved.append(appointment)
        return appointment

    def delete(self, appointment_id):
        return True

    def list_all(self, limit=100):
        return list(self.saved)


def make_tool(available=True, doctors=("王笠",)):
    repo = FakeRepository()
    slot = FakeSlotService(available=available, doctors=doctors)
    tool = BookAppointmentTool(repo, slot, FakeDoctorService())
    return tool, repo, slot


def test_normal_booking_succeeds():
    tool, repo, _slot = make_tool()
    result = await_call(
        tool,
        {
            "username": "张三",
            "id_card": VALID_ID,
            "department": "神经内科",
            "date": "2026-11-20",
            "time": "上午",
            "doctor_name": "王笠",
        },
    )
    assert result.success
    assert repo.saved[0].username == "张三"
    assert repo.saved[0].doctor_name == "王笠"


def test_doctor_name_in_username_is_rejected():
    """模型把医生名填进患者姓名 → 必须拒绝，不能建记录。

    2026-11-10 真实事故：用户给的是医生名，患者姓名还没给，
    小模型把"王新勃"填进 username → 用医生名建了一条患者预约记录。
    """
    tool, repo, _slot = make_tool()
    result = await_call(
        tool,
        {
            "username": "王笠",
            "id_card": VALID_ID,
            "department": "神经内科",
            "date": "2026-11-20",
            "time": "上午",
        },
    )
    assert not result.success
    assert result.error_type == "ValidationError"
    assert "医生" in result.content
    assert not repo.saved  # 绝不能落库


def test_patient_name_same_as_doctor_is_allowed_when_doctor_given():
    """用户明确指定了医生，且患者姓名恰好也是该医生 → 不误伤"""
    tool, repo, _slot = make_tool()
    result = await_call(
        tool,
        {
            "username": "王笠",
            "id_card": VALID_ID,
            "department": "神经内科",
            "date": "2026-11-20",
            "time": "上午",
            "doctor_name": "崔丽英",
        },
    )
    assert result.success
    assert repo.saved[0].username == "王笠"
    assert repo.saved[0].doctor_name == "崔丽英"


def test_no_slot_returns_failure_not_success():
    tool, repo, _slot = make_tool(available=False)
    result = await_call(
        tool,
        {
            "username": "张三",
            "id_card": VALID_ID,
            "department": "神经内科",
            "date": "2026-11-20",
            "time": "上午",
        },
    )
    assert not result.success
    assert result.error_type == "NoSlotAvailable"
    assert not repo.saved


def test_invalid_id_card_is_rejected_before_write():
    tool, repo, _slot = make_tool()
    result = await_call(
        tool,
        {
            "username": "张三",
            "id_card": "123456789012345678",  # 校验位不对
            "department": "神经内科",
            "date": "2026-11-20",
            "time": "上午",
        },
    )
    assert not result.success
    assert result.error_type == "ValidationError"
    assert not repo.saved


def test_chinese_argument_keys_are_normalized():
    """模型用中文键名（"患者姓名"…）也要能正确落库"""
    tool, repo, _slot = make_tool()
    result = await_call(
        tool,
        {
            "患者姓名": "张三",
            "身份证号": VALID_ID,
            "预约科室": "神经内科",
            "预约日期": "2026-11-20",
            "预约时段": "上午",
            "预约医生": "王笠",
        },
    )
    assert result.success
    assert repo.saved[0].username == "张三"
    assert repo.saved[0].doctor_name == "王笠"


def await_call(tool, params):
    """工具是 async 的，这里用 asyncio.run 同步驱动"""
    return asyncio.run(tool.call(params))
