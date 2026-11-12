"""审批弹窗前的号源预检（2026-11-12 真机）。

事故现场：用户要「呼吸内科 2026-11-14 下午」（周六，本院不排班），
流程却一路走到底 —— 弹确认窗 → 用户核对 → 点确认 → 执行 → 才被告知
"暂无出诊安排"。确认窗是"我要挂这个号"的承诺，让人为注定失败的参数
点确认，等于白花一次一次性 token，体验上就是"拖到审批才拒"。

口径与科室守卫一致：**只拦模型改不了的事实**。号源竞争、幂等命中这类
会变的东西不在预检里拦 —— 最终锁定仍由 ``book()`` 负责。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from xiaozhi.repositories.appointment_repo import (  # noqa: E402
    InMemoryAppointmentRepository,
)
from xiaozhi.service.slot_service import SlotAvailability  # noqa: E402
from xiaozhi.tools.appointment_tools import BookAppointmentTool  # noqa: E402

VALID_ID = "610121199401032831"


class FakeSlotService:
    """按 (日期, 时段) 决定有没有号；``raise_on_check`` 模拟号源系统故障。"""

    def __init__(self, available=None, *, raise_on_check: bool = False):
        self.available = available or {}
        self.raise_on_check = raise_on_check
        self.calls = []

    def check(self, department, date_str, time_slot, doctor_name=None):
        self.calls.append((date_str, time_slot))
        if self.raise_on_check:
            raise RuntimeError("HIS 不可用")
        if self.available.get((date_str, time_slot)):
            return SlotAvailability(
                available=True,
                message=f"{department} {date_str} {time_slot}有号源",
                doctors=["徐国兴"],
                remaining=5,
            )
        return SlotAvailability.none(
            f"{department} 在 {date_str} {time_slot}暂无出诊安排。"
        )

    def hold_slot(self, *args, **kwargs):  # 让 _supports_hold 成立
        raise AssertionError("预检不应该预占号源")

    def confirm_hold(self, *args, **kwargs):
        raise AssertionError("预检不应该确认落单")

    def release_hold(self, *args, **kwargs):
        raise AssertionError("预检不应该释放号源")


def make_tool(slot_service) -> BookAppointmentTool:
    return BookAppointmentTool(
        InMemoryAppointmentRepository(), slot_service, doctor_service=None
    )


ARGS = {
    "username": "王新勃",
    "id_card": VALID_ID,
    "department": "呼吸内科",
    "date": "2026-11-14",
    "time": "下午",
}


def test_no_slot_blocks_before_approval():
    """没号的时段 → 不生成 PENDING（不弹确认窗），并给出替代时段。"""
    tool = make_tool(FakeSlotService({("2026-11-16", "下午"): True}))
    error = tool.prevalidate_args(dict(ARGS))
    assert error, "没有号源时必须拦下来"
    assert "暂无出诊安排" in error
    # 替代建议要具体到"哪天哪个时段"，否则用户还是不知道该约哪天
    assert "2026-11-16 下午" in error


def test_precheck_never_reserves_slot():
    """预检只查不占 —— 中途失败（hold 被调用）会直接抛 AssertionError。"""
    tool = make_tool(FakeSlotService({}))
    tool.prevalidate_args(dict(ARGS))  # 不抛 = 没有预占


def test_available_slot_passes_through():
    """有号 → 放行，交给正常审批流程。"""
    tool = make_tool(FakeSlotService({("2026-11-14", "下午"): True}))
    assert tool.prevalidate_args(dict(ARGS)) == ""


def test_slot_service_failure_passes_through():
    """号源系统故障 ≠ 没号：预检必须放行，不能把故障说成"没号"。"""
    tool = make_tool(FakeSlotService(raise_on_check=True))
    assert tool.prevalidate_args(dict(ARGS)) == ""


def test_incomplete_args_pass_through():
    """缺字段/格式错交给工具层（它会告诉模型怎么改），预检不越权。"""
    tool = make_tool(FakeSlotService({}))
    args = dict(ARGS)
    args.pop("id_card")
    assert tool.prevalidate_args(args) == ""


def test_suggest_alternatives_lists_nearest_slots():
    """替代建议：按天递推，取最近的可约时段。"""
    service = make_tool(
        FakeSlotService({("2026-11-16", "上午"): True, ("2026-11-17", "下午"): True})
    )._service
    tips = service.suggest_alternatives(
        department="呼吸内科", date="2026-11-14", time="下午"
    )
    assert tips[0][0] == "2026-11-16"
    assert len(tips) == 2


def test_suggest_alternatives_prefers_requested_slot():
    """同一时段（用户原本要的下午）优先于"换个时段"。"""
    service = make_tool(
        FakeSlotService(
            {
                ("2026-11-16", "上午"): True,
                ("2026-11-16", "下午"): True,
            }
        )
    )._service
    tips = service.suggest_alternatives(
        department="呼吸内科", date="2026-11-14", time="下午", limit=1
    )
    assert tips == [("2026-11-16", "下午")]


def test_suggest_alternatives_degrades_silently():
    """建议查询失败就返回空（调用方降级为通用提示），绝不抛。"""
    service = make_tool(FakeSlotService(raise_on_check=True))._service
    assert service.suggest_alternatives(
        department="呼吸内科", date="2026-11-14", time="下午"
    ) == []
    assert service.suggest_alternatives(
        department="呼吸内科", date="不是日期", time="下午"
    ) == []
