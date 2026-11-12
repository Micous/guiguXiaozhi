"""「预约永远写不进库」事故的护栏测试（2026-11-12 真机）。

事故链（用户点「确认执行」后看到"数据存储暂时不可用，请稍后重试"）：

1. 用户选「呼吸内科 2026-11-14 下午」—— 该时段**没有排班**；
2. 本地 HIS 把"无排班"包成 `code=500`（真实院方也这么干），
   而 `暂无出诊安排` 一条拒绝词都没命中 → 被判成「结果不确定」（UNCERTAIN）；
3. 客户端白重试 2 次 → 仍不确定 → 走 UNKNOWN 分支；
4. UNKNOWN 分支为了留对账凭据，写 `idem:<sha256>` = **69 字符**，
   而列是 `VARCHAR(64)` → `1406 Data too long`；
5. 1406 落进 `mysql.connector.Error` 兜底 → 被翻译成「数据存储暂时不可用」
   → 用户被告知"请稍后重试"，但重试一万次也不会成功。

三处修复各自钉住：
  * 分类：无排班 = 明确拒绝（不再重试、不再 UNKNOWN）；
  * 落库：数据形状错误 ≠ 存储暂时不可用（分类按"能否靠重试恢复"）；
  * 列宽 + 前置校验：`idem:` 凭据装得下，超长给明确原因。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from xiaozhi.repositories.appointment_repo import (  # noqa: E402
    InMemoryAppointmentRepository,
    MySQLAppointmentRepository,
)
from xiaozhi.repositories.errors import (  # noqa: E402
    PersistenceError,
    RepositoryUnavailable,
)
from xiaozhi.service import his_resilience as hr  # noqa: E402
from xiaozhi.service.appointment_service import AppointmentService  # noqa: E402
from xiaozhi.service.his_client import HisSlot  # noqa: E402
from xiaozhi.service.his_slot_service import (  # noqa: E402
    BookingResult,
    ReleaseResult,
    SlotHoldResult,
)
from xiaozhi.service.local_his import LocalHisState  # noqa: E402

VALID_ID = "610121199401032831"


# ============================================================
# 1. HIS 业务码分类：无排班是"明确拒绝"，不是"结果不确定"
# ============================================================


@pytest.mark.parametrize(
    "msg",
    [
        "呼吸内科 在 2026-11-14 下午暂无出诊安排。",
        "呼吸内科 在 2026-11-14 下午无排班",
        "该科室 2026-11-14 下午没有排班",
        "该时段暂无号源",
        "该科室该时段无可预约号源",
    ],
)
def test_no_schedule_is_clear_rejection(msg):
    """院方把"无排班"包成 code=500 —— 仍必须判为 REJECTED。

    判成不确定的代价是实打实的：客户端重试 + 落 UNKNOWN + 用户被
    告知"请勿重复提交"，而事实是"这天下午根本没医生出诊"。
    """
    decision = hr.classify_business_code(500, msg)
    assert decision.kind == hr.KIND_REJECTED
    assert decision.retryable is False
    # 拒绝原因要能原样给用户看（已脱敏），不要换成含糊话术
    assert "排班" in decision.message or "出诊" in decision.message or "号源" in decision.message


def test_real_fault_still_uncertain():
    """反向护栏：真·系统故障必须仍是 UNCERTAIN。

    这一条防止"为了让无排班判成拒绝"而把 500 一刀切成拒绝 ——
    那样超时/内部错误会被当明确失败，用户重试就会重复挂号。
    """
    for msg in ("系统内部错误", "数据库连接失败", "网关超时", "服务不可用"):
        decision = hr.classify_business_code(500, msg)
        assert decision.kind == hr.KIND_UNCERTAIN, msg
        assert decision.retryable is True


# ============================================================
# 2. 落库异常分类：数据形状错 vs 存储暂时不可用
# ============================================================


def _repo() -> MySQLAppointmentRepository:
    """只构造不连接 —— 本节测的是异常翻译与前置校验，不需要真库。"""
    return MySQLAppointmentRepository(
        host="127.0.0.1", port=3306, user="u", password="p", database="d"
    )


def test_data_too_long_is_not_repository_unavailable():
    """1406 必须翻成 PersistenceError（重试无用），不能说"存储暂时不可用"。"""
    import mysql.connector

    exc = mysql.connector.errors.DataError(
        "1406 (22001): Data too long for column 'external_hold_id' at row 1"
    )
    translated = MySQLAppointmentRepository._translate(exc, "写入预约")
    assert isinstance(translated, PersistenceError)
    assert not isinstance(translated, RepositoryUnavailable)
    assert "表结构" in str(translated)


def test_operational_error_is_still_repository_unavailable():
    """反向护栏：断连/超时是**真的**可以靠重试恢复，不能误降级成持久化错误。"""
    import mysql.connector

    exc = mysql.connector.errors.OperationalError("2013 Lost connection to MySQL server")
    translated = MySQLAppointmentRepository._translate(exc, "写入预约")
    assert isinstance(translated, RepositoryUnavailable)


def test_idem_prefixed_key_fits_column_width():
    """UNKNOWN 分支写的 `idem:<sha256>` 必须装得进列（69 ≤ 128）。

    这条是事故的正中心：64 位 hexdigest + `idem:` 前缀 = 69 字符，
    旧列宽 VARCHAR(64) 让它 100% 失败。
    """
    idem = "a" * 64  # sha256().hexdigest() 的长度
    hold_ref = f"idem:{idem}"
    assert len(hold_ref) == 69
    assert len(hold_ref) <= MySQLAppointmentRepository.EXTERNAL_REF_MAX_LEN

    repo = _repo()
    from xiaozhi.repositories.appointment_repo import Appointment

    appointment = Appointment(
        username="张三",
        id_card=VALID_ID,
        department="呼吸内科",
        date="2026-11-14",
        time="下午",
        external_hold_id=hold_ref,
    )
    repo._validate_external_refs(appointment)  # 不抛 = 通过


def test_overlong_external_ref_gives_explicit_reason():
    """未来真 HIS 单号超长时，要给出明确原因，而不是让 1406 变成"稍后重试"。"""
    from xiaozhi.repositories.appointment_repo import Appointment

    repo = _repo()
    appointment = Appointment(
        username="张三",
        id_card=VALID_ID,
        department="呼吸内科",
        date="2026-11-14",
        time="下午",
        external_hold_id="x" * (repo.EXTERNAL_REF_MAX_LEN + 1),
    )
    with pytest.raises(PersistenceError) as excinfo:
        repo._validate_external_refs(appointment)
    assert "超长" in str(excinfo.value)
    assert "重试不会自愈" in str(excinfo.value)


# ============================================================
# 3. 本地 HIS 必须回传"实际分配的医生"
# ============================================================


def test_hold_returns_assigned_doctor():
    """用户没点名医生时，HIS 按号池分配 —— 分配结果必须回传。

    不回传的后果：预约记录和回复都写"医生：未指定"，
    用户当场追问"怎么没给我安排一个医生"。
    """
    state = LocalHisState()
    state.add_roster("呼吸内科", "徐国兴", 3)
    result, error = state.hold_slot("2026-12-09", "上午", "呼吸内科")
    assert not error
    assert result["doctorName"] == "徐国兴"
    assert result["deptName"] == "呼吸内科"
    assert result["clinicDate"] == "2026-12-09"
    assert result["timeDesc"] == "上午"


class _FakeHisSlotService:
    """支持预占语义的最小替身（带 HIS 分配的医生）。"""

    patient_master_enabled = False

    def __init__(self, doctor: str = "徐国兴"):
        self.doctor = doctor

    def check(self, department, date_str, time_slot, doctor_name=None):
        from xiaozhi.service.slot_service import SlotAvailability

        return SlotAvailability(
            available=True, message="有号源", doctors=[self.doctor], remaining=3
        )

    def hold_slot(self, department, date_str, time_slot, doctor_name=None, **kwargs):
        return SlotHoldResult(
            held=True,
            hold_id="hold-1",
            slot=HisSlot(
                clinic_for_regist_id="cfr-1",
                clinic_date=date_str,
                time_desc=time_slot,
                limit_num=3,
                doctor_name=self.doctor,
                dept_name=department,
            ),
            message="ok",
        )

    def confirm_hold(self, hold_id, patient_ref, **kwargs):
        return BookingResult(confirmed=True, external_booking_id="reg-1", message="ok")

    def release_hold(self, hold_id, **kwargs):
        return ReleaseResult(released=True, message="ok")


def test_service_persists_doctor_assigned_by_his():
    """服务层要把 HIS 分配的医生写进预约记录（不是一律 "未指定"）。"""
    repository = InMemoryAppointmentRepository()
    service = AppointmentService(
        repository, _FakeHisSlotService("徐国兴"), patient_ref_salt="salt"
    )
    result = service.book(
        username="张三",
        id_card=VALID_ID,
        department="呼吸内科",
        date="2026-11-14",
        time="下午",
    )
    assert result.appointment is not None
    assert result.appointment.doctor_name == "徐国兴"
    assert result.appointment.external_hold_id == "hold-1"
    assert result.appointment.status.value == "CONFIRMED"


def test_user_specified_doctor_wins():
    """用户点名了医生，就不能被 HIS 的号池分配覆盖。"""
    repository = InMemoryAppointmentRepository()
    service = AppointmentService(
        repository, _FakeHisSlotService("徐国兴"), patient_ref_salt="salt"
    )
    result = service.book(
        username="李四",
        id_card=VALID_ID,
        department="呼吸内科",
        date="2026-11-14",
        time="下午",
        doctor_name="王笠",
    )
    assert result.appointment.doctor_name == "王笠"
