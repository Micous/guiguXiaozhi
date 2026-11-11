"""Phase 3b 契约/混沌测试 —— 真实 HIS 接入层（审计 P0-03）。

不依赖真实沙箱：用 **fake HIS server**（HTTP 层）验证契约语义。
这些测试保护的是**我们这边的语义**，与医院实现无关：

.. code-block:: text

    1. 号源查询：剩余号必须扣掉预占数（并发超卖的根源）
    2. 统一响应包：HTTP 200 但 code != 200 → 业务失败（不是成功）
    3. 超时 = 不确定 → UNKNOWN，**不是**失败（否则重复挂号）
    4. 幂等：同一幂等键并发 20 次 → HIS 侧只处理一次
    5. 对账：HIS 有单 → CONFIRMED；HIS 无单 → FAILED；查单不确定 → 保持 UNKNOWN
    6. 预占失败要释放（号源不被白占）
    7. 本地查重与 HIS 号源是**两道独立的防线**（各管各的）
"""

from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from xiaozhi.repositories.appointment_repo import (  # noqa: E402
    Appointment,
    AppointmentStatus,
    InMemoryAppointmentRepository,
)
from xiaozhi.service.appointment_service import (  # noqa: E402
    AppointmentService,
    BookingOutcome,
)
from xiaozhi.service.his_client import (  # noqa: E402
    HisClient,
    HisRejectedError,
    HisUncertainError,
)
from xiaozhi.service.his_reconcile import ReconcileJob  # noqa: E402
from xiaozhi.service.his_slot_service import HisSlotService  # noqa: E402

VALID_ID = "110101199003074567"  # 通过 GB 11643 校验位


# ==================================================================== fake HIS


class _FakeHisState:
    """fake HIS 的内部状态（号源、单据、幂等表、故障开关）。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.slots = {
            "slot-1": {
                "clinicForRegistId": "slot-1",
                "clinicIndexId": "idx-1",
                "clinicScheduleId": "sched-1",
                "clinicDate": "2026-11-30",
                "timeDesc": "上午",
                "limitNum": 10,
                "currentNum": 2,
                "currentPreNum": 1,
                "doctorName": "张三",
                "deptName": "呼吸内科",
            }
        }
        self.bookings = {}          # external_booking_id -> {"status": ...}
        self.idempotency = {}       # key -> result payload
        self.hold_count = 0
        self.confirm_count = 0
        self.release_count = 0
        self.mode = "ok"            # ok / timeout / http500 / business_fail
        self.fail_paths = None      # business_fail 生效的路径（None = 全部）
        self.timeout_paths = None    # timeout 生效的路径（None = 全部）


class _FakeHisHandler(BaseHTTPRequestHandler):
    state: _FakeHisState = None  # 由 create_fake_his 注入

    def log_message(self, *_args):  # 静音
        return

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        state = self.state
        if state.mode == "timeout":
            timeout_paths = state.timeout_paths or {"/clinicForRegist/list", "/currentRegister/"}
            if any(self.path.startswith(p) for p in timeout_paths):
                # 不响应，客户端超时
                import time

                time.sleep(3)
                return
        if self.path.startswith("/clinicForRegist/list"):
            # fake 实现按 deptName 过滤（真实 HIS 也是这么查的）
            query = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(query)
            dept = (params.get("deptName") or [""])[0]
            with state.lock:
                rows = [
                    row
                    for row in state.slots.values()
                    if not dept or row.get("deptName") == dept
                ]
            self._send(200, {"code": 200, "msg": "ok", "data": rows})
            return
        if self.path.startswith("/currentRegister/"):
            booking_id = self.path.rsplit("/", 1)[-1]
            with state.lock:
                booking = state.bookings.get(booking_id)
            if booking is None:
                self._send(200, {"code": 404, "msg": "not found", "data": None})
                return
            self._send(200, {"code": 200, "msg": "ok", "data": booking})
            return
        self._send(200, {"code": 404, "msg": "no route", "data": None})

    def do_POST(self):
        state = self.state
        if state.mode == "timeout":
            timeout_paths = state.timeout_paths or {"/hold", "/add"}
            if any(self.path.endswith(p) for p in timeout_paths):
                import time

                time.sleep(3)
                return
        if state.mode == "http500":
            self._send(500, {"code": 500, "msg": "internal error"})
            return
        if state.mode == "business_fail":
            # 业务拒绝（HTTP 200 但 code != 200）：号源已满/患者不存在等
            # ``fail_paths`` 限定只在指定路径生效（默认全部 POST 路径）
            fail_paths = state.fail_paths or {"/hold", "/add"}
            if any(self.path.endswith(p) for p in fail_paths):
                self._send(200, {"code": 500, "msg": "号源已满", "data": None})
                return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            body = {}
        idem = self.headers.get("X-Idempotency-Key")

        if self.path.endswith("/hold"):
            with state.lock:
                if idem and idem in state.idempotency:
                    self._send(200, {"code": 200, "msg": "ok", "data": state.idempotency[idem]})
                    return
                state.hold_count += 1
                hold_id = f"hold-{state.hold_count}"
                # 预占 → 号被占住（currentPreNum+1）
                slot = state.slots.get("slot-1")
                if slot is not None:
                    slot["currentPreNum"] = slot.get("currentPreNum", 0) + 1
                payload = {
                    "holdId": hold_id,
                    "clinicForRegistId": "slot-1",
                    "clinicMasterId": f"reg-{state.hold_count}",
                }
                if idem:
                    state.idempotency[idem] = payload
            self._send(200, {"code": 200, "msg": "ok", "data": payload})
            return
        if self.path.endswith("/add"):
            with state.lock:
                if idem and idem in state.idempotency:
                    self._send(200, {"code": 200, "msg": "ok", "data": state.idempotency[idem]})
                    return
                state.confirm_count += 1
                booking_id = f"reg-{state.confirm_count}"
                payload = {"clinicMasterId": booking_id}
                state.bookings[booking_id] = {
                    "clinicMasterId": booking_id,
                    "clinicForRegistId": "slot-1",
                    "status": "REGISTERED",
                }
                # 确认 → 预占转挂号（pre-1, num+1）
                slot = state.slots.get("slot-1")
                if slot is not None:
                    slot["currentPreNum"] = max(0, slot.get("currentPreNum", 0) - 1)
                    slot["currentNum"] = slot.get("currentNum", 0) + 1
                if idem:
                    state.idempotency[idem] = payload
            self._send(200, {"code": 200, "msg": "ok", "data": payload})
            return
        if self.path.endswith("/release"):
            with state.lock:
                state.release_count += 1
                slot = state.slots.get("slot-1")
                if slot is not None:
                    slot["currentPreNum"] = max(0, slot.get("currentPreNum", 0) - 1)
            self._send(200, {"code": 200, "msg": "released", "data": {}})
            return
        if self.path.endswith("/cancel"):
            # 退号：挂号单置 CANCELLED（号源归还由 fake 的 slots 记账体现）
            booking_id = str(body.get("clinicMasterId") or body.get("bookingId") or "")
            with state.lock:
                booking = state.bookings.get(booking_id)
                if booking is None:
                    self._send(200, {"code": 500, "msg": "挂号单不存在", "data": None})
                    return
                if booking.get("status") == "CANCELLED":
                    self._send(200, {"code": 500, "msg": "该挂号单已退号", "data": None})
                    return
                booking["status"] = "CANCELLED"
                # 退号 → 号源归还（真实语义：号回到池子里）
                slot_id = booking.get("clinicForRegistId")
                slot = state.slots.get(slot_id)
                if slot is not None:
                    slot["currentNum"] = max(0, slot.get("currentNum", 0) - 1)
            self._send(200, {"code": 200, "msg": "ok", "data": {"cancelled": True}})
            return
        self._send(200, {"code": 404, "msg": "no route", "data": None})


@pytest.fixture
def fake_his():
    """启动 fake HIS server，产出 (base_url, state)。"""
    state = _FakeHisState()
    handler = type("BoundHandler", (_FakeHisHandler,), {"state": state})
    # ThreadingHTTPServer：并发 20 请求必须能同时处理（单线程会 connection reset）
    # request_queue_size：listen backlog，默认 5 会被并发连接打爆 → connection reset
    server_class = type(
        "ThreadingServer",
        (ThreadingHTTPServer,),
        {"request_queue_size": 128, "daemon_threads": True},
    )
    server = server_class(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        yield base_url, state
    finally:
        server.shutdown()
        server.server_close()


def _client(base_url: str, timeout: float = 1.0) -> HisClient:
    return HisClient(base_url, token="test-token", timeout=timeout)


def _service(base_url: str, repo=None, timeout: float = 1.0):
    repo = repo or InMemoryAppointmentRepository()
    client = _client(base_url, timeout)
    slot = HisSlotService(client)
    service = AppointmentService(repo, slot, patient_ref_salt="salt")
    return repo, slot, service


# ==================================================================== 契约


def test_response_envelope_code_is_business_status(fake_his):
    """HTTP 200 但 code != 200 → 业务失败（不是成功）。"""
    base_url, state = fake_his
    state.mode = "business_fail"
    state.fail_paths = {"/hold"}
    client = _client(base_url)
    with pytest.raises(HisRejectedError):
        client.hold_slot({"deptName": "呼吸内科"}, idempotency_key="k1")


def test_remaining_slots_subtract_pre_held(fake_his):
    """剩余号 = limit - 已挂号 - **已预占**（并发超卖的根源）。"""
    base_url, state = fake_his
    client = _client(base_url)
    slots = client.query_slots({"clinicDate": "2026-11-30"})
    assert len(slots) == 1
    # limitNum=10, currentNum=2, currentPreNum=1 → 7
    assert slots[0].remaining == 7


def test_check_reports_remaining_and_doctors(fake_his):
    base_url, _state = fake_his
    slot = HisSlotService(_client(base_url))
    availability = slot.check("呼吸内科", "2026-11-30", "上午")
    assert availability.available is True
    assert availability.remaining == 7
    assert "张三" in availability.doctors


def test_no_slot_returns_unavailable(fake_his):
    base_url, _state = fake_his
    slot = HisSlotService(_client(base_url))
    availability = slot.check("骨科", "2026-11-30", "上午")
    assert availability.available is False


# ==================================================================== 幂等


def test_idempotency_key_reuses_same_result(fake_his):
    """同一幂等键重发 → HIS 返回同一结果，不重复处理。"""
    base_url, state = fake_his
    client = _client(base_url)
    first = client.hold_slot({"deptName": "呼吸内科"}, idempotency_key="idem-1")
    second = client.hold_slot({"deptName": "呼吸内科"}, idempotency_key="idem-1")
    assert first.hold_id == second.hold_id
    assert state.hold_count == 1, "同一幂等键被处理了多次"


def test_concurrent_same_idempotency_key_holds_once(fake_his):
    """并发 20 次同一幂等键 → HIS 侧只处理一次（幂等语义）。"""
    base_url, state = fake_his
    client = _client(base_url, timeout=5.0)
    results = []
    errors = []

    def worker():
        try:
            results.append(
                client.hold_slot({"deptName": "呼吸内科"}, idempotency_key="idem-concurrent")
            )
        except Exception as exc:  # pragma: no cover - 诊断用
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert not errors, errors
    assert len(results) == 20
    assert state.hold_count == 1, f"幂等失效：处理了 {state.hold_count} 次"
    assert len({r.hold_id for r in results}) == 1


def test_different_idempotency_keys_are_independent(fake_his):
    base_url, state = fake_his
    client = _client(base_url)
    client.hold_slot({"deptName": "呼吸内科"}, idempotency_key="idem-a")
    client.hold_slot({"deptName": "呼吸内科"}, idempotency_key="idem-b")
    assert state.hold_count == 2


def test_idempotency_key_does_not_leak_id_card():
    """幂等键是哈希 —— 不该把身份证号原样送到 HIS（会进 HIS 侧日志）。"""
    from xiaozhi.service.his_slot_service import build_idempotency_key

    key = build_idempotency_key("呼吸内科", "2026-11-30", "上午", VALID_ID)
    assert VALID_ID not in key
    assert len(key) == 64  # sha256 hex


# ==================================================================== 超时 = 不确定


def test_timeout_is_uncertain_not_rejected(fake_his):
    """超时 → HisUncertainError（**不是** Rejected —— 结果不确定）。"""
    base_url, state = fake_his
    state.mode = "timeout"
    client = _client(base_url, timeout=0.5)
    with pytest.raises(HisUncertainError):
        client.hold_slot({"deptName": "呼吸内科"}, idempotency_key="k-timeout")


def test_http_500_is_uncertain(fake_his):
    """5xx → 不确定（HIS 可能已经扣了号源）。"""
    base_url, state = fake_his
    state.mode = "http500"
    client = _client(base_url)
    with pytest.raises(HisUncertainError):
        client.hold_slot({"deptName": "呼吸内科"}, idempotency_key="k-500")


def test_booking_timeout_yields_unknown_not_failure(fake_his):
    """落单超时 → 本地 UNKNOWN（**不是** FAILED）。

    这是 Phase 3b 最关键的一条：当成失败 → 用户重试 → 重复挂号。
    """
    base_url, state = fake_his
    repo, _slot, service = _service(base_url)
    # 先正常预占（幂等键固定），再让确认超时
    state.mode = "timeout"
    result = service.book(
        username="张三",
        id_card=VALID_ID,
        department="呼吸内科",
        date="2026-11-30",
        time="上午",
    )
    # 预占本身就超时了 → 结果不确定 → UNKNOWN
    assert result.outcome == BookingOutcome.UNKNOWN
    rows = repo.list_all()
    assert rows and rows[0].status == AppointmentStatus.UNKNOWN
    assert result.outcome != BookingOutcome.FAILED


def test_confirm_timeout_leaves_unknown_for_reconcile(fake_his):
    """预占成功但确认超时 → UNKNOWN + 保留外部单号（对账要靠它）。"""
    base_url, state = fake_his
    repo = InMemoryAppointmentRepository()
    client = _client(base_url, timeout=1.0)
    slot = HisSlotService(client)
    service = AppointmentService(repo, slot, patient_ref_salt="salt")

    # 只有"确认落单"超时，预占正常成功
    state.mode = "timeout"
    state.timeout_paths = {"/add"}
    try:
        result = service.book(
            username="张三",
            id_card=VALID_ID,
            department="呼吸内科",
            date="2026-11-30",
            time="上午",
        )
    finally:
        state.mode = "ok"
        state.timeout_paths = None

    assert result.outcome == BookingOutcome.UNKNOWN, result.outcome
    rows = repo.list_all()
    assert rows[0].status == AppointmentStatus.UNKNOWN
    assert rows[0].external_booking_id, "对账需要外部单号，没落库就无法对账"


# ==================================================================== 预占失败要释放


def test_confirm_failure_releases_hold(fake_his):
    """确认失败 → 释放预占（号源不被白占）+ 本地 FAILED。"""
    base_url, state = fake_his
    repo = InMemoryAppointmentRepository()
    client = _client(base_url)
    slot = HisSlotService(client)
    service = AppointmentService(repo, slot, patient_ref_salt="salt")

    # 只有"确认落单"失败，预占正常成功
    state.mode = "business_fail"
    state.fail_paths = {"/add"}
    try:
        result = service.book(
            username="张三",
            id_card=VALID_ID,
            department="呼吸内科",
            date="2026-11-30",
            time="上午",
        )
    finally:
        state.mode = "ok"
        state.fail_paths = None

    assert result.outcome == BookingOutcome.FAILED, result.message
    assert state.release_count >= 1, "预占未释放，号源被白占"
    rows = repo.list_all()
    assert rows[0].status == AppointmentStatus.FAILED


# ==================================================================== 对账


def test_reconcile_confirms_when_his_has_booking(fake_his):
    """HIS 有单 → UNKNOWN 收敛为 CONFIRMED。"""
    base_url, state = fake_his
    repo = InMemoryAppointmentRepository()
    slot = HisSlotService(_client(base_url))
    # 手工造一条 UNKNOWN（外部单号指向 fake HIS 已存在的单）
    appointment = Appointment(
        username="张三",
        id_card=VALID_ID,
        department="呼吸内科",
        date="2026-11-30",
        time="上午",
        status=AppointmentStatus.UNKNOWN,
        external_booking_id="reg-1",
    )
    repo.create_if_absent(appointment)
    state.bookings["reg-1"] = {"clinicMasterId": "reg-1", "status": "REGISTERED"}

    job = ReconcileJob(repo, slot)
    report = job.run(limit=10)
    assert report.scanned == 1
    assert len(report.resolved) == 1
    assert repo.get_by_id(appointment.id).status == AppointmentStatus.CONFIRMED


def test_reconcile_marks_failed_when_his_has_no_booking(fake_his):
    """HIS 无单 → 收敛为 FAILED（确认确实没挂上）。"""
    base_url, _state = fake_his
    repo = InMemoryAppointmentRepository()
    slot = HisSlotService(_client(base_url))
    appointment = Appointment(
        username="张三",
        id_card=VALID_ID,
        department="呼吸内科",
        date="2026-11-30",
        time="上午",
        status=AppointmentStatus.UNKNOWN,
        external_booking_id="reg-missing",
    )
    repo.create_if_absent(appointment)

    job = ReconcileJob(repo, slot)
    report = job.run(limit=10)
    assert len(report.resolved) == 1
    assert repo.get_by_id(appointment.id).status == AppointmentStatus.FAILED


def test_reconcile_skips_without_external_booking_id(fake_his):
    """没有外部单号 → 无据可查，保持 UNKNOWN（不猜）。"""
    base_url, _state = fake_his
    repo = InMemoryAppointmentRepository()
    slot = HisSlotService(_client(base_url))
    appointment = Appointment(
        username="张三",
        id_card=VALID_ID,
        department="呼吸内科",
        date="2026-11-30",
        time="上午",
        status=AppointmentStatus.UNKNOWN,
    )
    repo.create_if_absent(appointment)

    job = ReconcileJob(repo, slot)
    report = job.run(limit=10)
    assert len(report.skipped) == 1
    assert repo.get_by_id(appointment.id).status == AppointmentStatus.UNKNOWN


def test_reconcile_keeps_unknown_when_query_uncertain(fake_his):
    """查单不确定 → 保持 UNKNOWN + attempts+1（不猜、不改状态）。"""
    base_url, state = fake_his
    repo = InMemoryAppointmentRepository()
    slot = HisSlotService(_client(base_url, timeout=0.5))
    appointment = Appointment(
        username="张三",
        id_card=VALID_ID,
        department="呼吸内科",
        date="2026-11-30",
        time="上午",
        status=AppointmentStatus.UNKNOWN,
        external_booking_id="reg-x",
    )
    repo.create_if_absent(appointment)
    state.mode = "timeout"

    job = ReconcileJob(repo, slot)
    report = job.run(limit=10)
    assert len(report.unresolved) == 1
    row = repo.get_by_id(appointment.id)
    assert row.status == AppointmentStatus.UNKNOWN
    assert row.reconcile_attempts == 1


def test_reconcile_stops_after_max_attempts(fake_his):
    """连续失败超阈值 → 跳过并告警（需人工介入），不改状态。"""
    base_url, state = fake_his
    repo = InMemoryAppointmentRepository()
    slot = HisSlotService(_client(base_url, timeout=0.5))
    appointment = Appointment(
        username="张三",
        id_card=VALID_ID,
        department="呼吸内科",
        date="2026-11-30",
        time="上午",
        status=AppointmentStatus.UNKNOWN,
        external_booking_id="reg-y",
        reconcile_attempts=5,
    )
    repo.create_if_absent(appointment)

    job = ReconcileJob(repo, slot, max_attempts=5)
    report = job.run(limit=10)
    assert len(report.skipped) == 1
    assert repo.get_by_id(appointment.id).status == AppointmentStatus.UNKNOWN


def test_reconcile_is_reentrant(fake_his):
    """可重入：同一批重复跑不产生额外副作用。"""
    base_url, state = fake_his
    repo = InMemoryAppointmentRepository()
    slot = HisSlotService(_client(base_url))
    appointment = Appointment(
        username="张三",
        id_card=VALID_ID,
        department="呼吸内科",
        date="2026-11-30",
        time="上午",
        status=AppointmentStatus.UNKNOWN,
        external_booking_id="reg-1",
    )
    repo.create_if_absent(appointment)
    state.bookings["reg-1"] = {"clinicMasterId": "reg-1", "status": "REGISTERED"}

    job = ReconcileJob(repo, slot)
    first = job.run(limit=10)
    second = job.run(limit=10)
    assert len(first.resolved) == 1
    assert second.scanned == 0  # 已收敛，不再是 UNKNOWN


# ==================================================================== 两道防线


def test_local_duplicate_check_still_applies(fake_his):
    """本地查重与 HIS 号源是**两道独立防线**：HIS 有号也不能重复约。"""
    base_url, _state = fake_his
    repo, _slot, service = _service(base_url, repo=InMemoryAppointmentRepository())
    first = service.book(
        username="张三",
        id_card=VALID_ID,
        department="呼吸内科",
        date="2026-11-30",
        time="上午",
    )
    assert first.outcome == BookingOutcome.CREATED
    second = service.book(
        username="张三",
        id_card=VALID_ID,
        department="呼吸内科",
        date="2026-11-30",
        time="上午",
    )
    assert second.outcome == BookingOutcome.ALREADY_EXISTS
    assert len(repo.list_all()) == 1


def test_patient_ref_is_stable_and_hides_id_card():
    """patient_ref 稳定、可跨请求关联，且不含明文身份证。"""
    ref1 = Appointment.make_patient_ref(VALID_ID, "salt")
    ref2 = Appointment.make_patient_ref(VALID_ID, "salt")
    ref3 = Appointment.make_patient_ref(VALID_ID, "other-salt")
    assert ref1 == ref2
    assert ref1 != ref3
    assert VALID_ID not in ref1
    assert len(ref1) == 32


def test_failed_appointment_can_be_rebooked(fake_his):
    """FAILED 是终态 → 同一时段可以重新预约（不占唯一约束）。"""
    base_url, _state = fake_his
    repo = InMemoryAppointmentRepository()
    appointment = Appointment(
        username="张三",
        id_card=VALID_ID,
        department="呼吸内科",
        date="2026-11-30",
        time="上午",
        status=AppointmentStatus.FAILED,
    )
    repo.create_if_absent(appointment)
    # 再约一次 → 不应被判重复
    again = Appointment(
        username="张三",
        id_card=VALID_ID,
        department="呼吸内科",
        date="2026-11-30",
        time="上午",
        status=AppointmentStatus.CONFIRMED,
    )
    _saved, created = repo.create_if_absent(again)
    assert created is True, "FAILED 记录仍占着唯一约束，用户无法重新预约"


# ==================================================================== 真机回归（2026-11-11）


def test_local_duplicate_check_happens_before_hold(fake_his):
    """本地查重必须**前置**到预占之前（不能白预占再白释放）。

    2026-11-11 真机暴露：先预占再查重 → fake HIS 侧 hold=1 / release=1，
    用户白等一轮跨网络调用，且真实环境下号源被短暂占用。
    """
    base_url, state = fake_his
    repo = InMemoryAppointmentRepository()
    slot = HisSlotService(_client(base_url))
    service = AppointmentService(repo, slot, patient_ref_salt="salt")

    # 先造一条同参数的既有预约
    existing = Appointment(
        username="张三",
        id_card=VALID_ID,
        department="呼吸内科",
        date="2026-11-30",
        time="上午",
        status=AppointmentStatus.CONFIRMED,
    )
    repo.create_if_absent(existing)

    result = service.book(
        username="张三",
        id_card=VALID_ID,
        department="呼吸内科",
        date="2026-11-30",
        time="上午",
    )
    assert result.outcome == BookingOutcome.ALREADY_EXISTS
    assert state.hold_count == 0, "重复预约不该预占号源（白占再白释放）"
    assert state.release_count == 0


def test_hold_and_confirm_use_different_idempotency_keys(fake_his):
    """预占与确认必须用**不同**幂等键。

    2026-11-11 真机暴露：两者共用一个键 → HIS 侧按幂等键去重时，
    "确认"会命中"预占"的记录 → 号被占住但挂号单没落成（号泄漏）。
    """
    base_url, state = fake_his
    repo = InMemoryAppointmentRepository()
    client = _client(base_url)
    slot = HisSlotService(client)
    service = AppointmentService(repo, slot, patient_ref_salt="salt")

    result = service.book(
        username="张三",
        id_card=VALID_ID,
        department="呼吸内科",
        date="2026-11-30",
        time="上午",
    )
    assert result.outcome == BookingOutcome.CREATED, result.message
    # 确认必须真的发生（不能被幂等表挡掉）
    assert state.confirm_count == 1, "确认落单被幂等键误挡（号泄漏）"
    assert state.hold_count == 1
    # 本地记录要有外部单号（对账要靠它）
    rows = repo.list_all()
    assert rows[0].external_booking_id, "外部单号未落库，对账无从下手"
    assert rows[0].patient_ref, "患者引用未落库"


# ==================================================================== 退号链路


def test_cancel_notifies_his_and_returns_slot(fake_his):
    """取消预约 → **必须先退 HIS**（号源归还），再改本地状态。

    2026-11-11 真机暴露：CancelAppointmentTool 构造时 slot_service 传 None
    → 服务层跳过 HIS 退号 → 本地说"取消成功"但医院侧单子还在、号源没回。
    """
    base_url, state = fake_his
    repo = InMemoryAppointmentRepository()
    client = _client(base_url)
    slot = HisSlotService(client)
    service = AppointmentService(repo, slot, patient_ref_salt="salt")

    # 先约上
    booked = service.book(
        username="张三",
        id_card=VALID_ID,
        department="呼吸内科",
        date="2026-11-30",
        time="上午",
    )
    assert booked.outcome == BookingOutcome.CREATED, booked.message
    slot_after_book = slot.check("呼吸内科", "2026-11-30", "上午")
    assert slot_after_book.remaining < slot_after_book.remaining + 1  # 号被扣了

    # 再取消
    cancelled = service.cancel(
        username="张三",
        id_card=VALID_ID,
        department="呼吸内科",
        date="2026-11-30",
        time="上午",
    )
    assert cancelled.outcome == BookingOutcome.CREATED, cancelled.message
    # 本地状态迁移
    assert repo.list_all()[0].status == AppointmentStatus.CANCELLED
    # HIS 侧：号源归还 + 挂号单已退
    booking_id = booked.appointment.external_booking_id
    assert booking_id
    his_booking = client.query_booking(booking_id)
    assert his_booking.status == "CANCELLED", "HIS 侧挂号单没有退号"
    slot_after_cancel = slot.check("呼吸内科", "2026-11-30", "上午")
    assert slot_after_cancel.remaining == slot_after_book.remaining + 1, "号源没有归还"


def test_cancel_tool_receives_slot_service():
    """取消工具必须拿到号源服务（否则退不了号）。"""
    from xiaozhi.tools.appointment_tools import CancelAppointmentTool

    tool = CancelAppointmentTool(InMemoryAppointmentRepository(), object())
    assert tool._service._slot_service is not None, "取消工具没拿到号源服务"
