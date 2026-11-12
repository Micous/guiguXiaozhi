"""本地 HIS 替身边界护栏 —— 锁住真机演练暴露的四个服务端缺陷。

真机协议层（413 响应帧、socket 超时、TCP RST）由
``scripts/local_his_fault_drill.py`` 覆盖；本文件只锁**状态机语义**，
保证后续重构不会把这几条悄悄改回去：

1. release / cancel 带幂等键重放必须成功（否则网络抖动会卡死回滚链路）；
2. 不带幂等键的重复退号仍明确拒绝（严格语义，号源不能扣成负数）；
3. 预占 TTL 惰性回收，且旧持久化文件（无 expires_at）重启后补上 TTL；
4. byKey 查单能凭 hold 键 / confirm 键反查挂号单，解开对账死结。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from xiaozhi.service.local_his import (  # noqa: E402
    LocalHisState,
    MAX_BODY_BYTES,
)

DATE = "2026-12-09"  # 周三，默认排班日
DEPT = "呼吸内科"


@pytest.fixture
def state():
    st = LocalHisState()
    st.add_roster(DEPT, "徐国兴", 3)
    return st


def _remaining(st: LocalHisState) -> int:
    return sum(s.remaining for s in st.query_slots(DATE, "上午", DEPT))


# ---------- 1. 幂等 ----------

def test_release_replay_is_idempotent(state):
    """同键重复释放：两次都成功，号只还一次。"""
    hold, _ = state.hold_slot(DATE, "上午", DEPT, idempotency_key="hold-1")
    before = _remaining(state)
    ok1, err1 = state.release_hold(hold["holdId"], idempotency_key="rel-1")
    ok2, err2 = state.release_hold(hold["holdId"], idempotency_key="rel-1")
    assert ok1 and ok2 and not err1 and not err2
    assert _remaining(state) == before + 1, "号源被重复归还"


def test_release_of_unknown_hold_with_key_is_success(state):
    """重放时 hold 已被 TTL 回收：带键视为已解决，不阻断本地回滚。"""
    ok, err = state.release_hold("hold-never", idempotency_key="rel-2")
    assert ok and not err
    assert "release:rel-2" in state.write_results


def test_cancel_replay_is_idempotent(state):
    """同键重复退号：两次都成功，号只还一次。"""
    hold, _ = state.hold_slot(DATE, "上午", DEPT, idempotency_key="hold-1")
    booking, _ = state.confirm_hold(hold["holdId"], idempotency_key="conf-1")
    state.cancel_booking(booking["clinicMasterId"], idempotency_key="can-1")
    after = _remaining(state)
    ok, err = state.cancel_booking(
        booking["clinicMasterId"], idempotency_key="can-1"
    )
    assert ok and not err
    assert _remaining(state) == after, "重复退号又把号还了一次"


def test_cancel_twice_without_key_still_rejected(state):
    """无键重复退号 → 明确失败（号源不能扣成负数）。"""
    hold, _ = state.hold_slot(DATE, "上午", DEPT)
    booking, _ = state.confirm_hold(hold["holdId"])
    state.cancel_booking(booking["clinicMasterId"])
    again, err = state.cancel_booking(booking["clinicMasterId"])
    assert not again and "已退号" in err


def test_write_results_are_scoped_by_action(state):
    """同字符串键但不同动作不串号（release 不能命中 cancel 的缓存）。"""
    hold, _ = state.hold_slot(DATE, "上午", DEPT, idempotency_key="hold-1")
    state.release_hold(hold["holdId"], idempotency_key="same-key")
    # 另一个动作即便用同名键，也必须重新走业务逻辑
    booking, err = state.confirm_hold(hold["holdId"], idempotency_key="same-key")
    assert booking is None and err  # hold 已被释放，明确失败
    assert "confirm:same-key" not in state.idempotency


# ---------- 2. 预占 TTL ----------

def test_expired_hold_returns_slot(state):
    """TTL 到点 → 惰性回收把号还回号池（不等后台定时任务）。"""
    state.hold_ttl_seconds = 0.05
    before = _remaining(state)
    state.hold_slot(DATE, "上午", DEPT, idempotency_key="hold-1")
    assert _remaining(state) == before - 1
    time.sleep(0.08)
    assert _remaining(state) == before, "过期预占没有归还号源"
    assert state.holds == {}


def test_confirm_after_expiry_is_rejected(state):
    """过期预占不能再落单，否则会超卖。"""
    state.hold_ttl_seconds = 0.05
    hold, _ = state.hold_slot(DATE, "上午", DEPT)
    time.sleep(0.08)
    booking, err = state.confirm_hold(hold["holdId"])
    assert booking is None and "过期" in err
    assert _remaining(state) == state.query_slots(DATE, "上午", DEPT)[0].limit_num


def test_legacy_hold_gets_ttl_on_restore(state):
    """旧持久化文件没有 expires_at → 恢复时补 TTL，避免重启后号永久占死。"""
    hold, _ = state.hold_slot(DATE, "上午", DEPT)
    payload = state.to_dict()
    for row in payload["holds"]:
        row.pop("expires_at", None)

    restored = LocalHisState()
    restored.load_dict(payload)
    assert restored.holds[hold["holdId"]].expires_at > time.time()


def test_zero_ttl_means_never_expire(state):
    """TTL=0 是显式的"不过期"开关，不能被当成"立刻过期"。"""
    state.hold_ttl_seconds = 0.0
    hold, _ = state.hold_slot(DATE, "上午", DEPT)
    time.sleep(0.02)
    assert hold["holdId"] in state.holds
    assert state.expire_holds() == 0


# ---------- 3. byKey 查单 ----------

def test_by_key_finds_booking_via_hold_key(state):
    """hold 超时、confirm 已落单：凭 hold 键能查到挂号单 → 对账可收敛。"""
    hold, _ = state.hold_slot(DATE, "上午", DEPT, idempotency_key="hold-key")
    booking, _ = state.confirm_hold(hold["holdId"], idempotency_key="conf-key")
    found = state.query_booking_by_idempotency_key("hold-key")
    assert found is not None
    assert found.clinic_master_id == booking["clinicMasterId"]
    assert found.status == "REGISTERED"


def test_by_key_finds_booking_via_confirm_key(state):
    hold, _ = state.hold_slot(DATE, "上午", DEPT, idempotency_key="hold-key")
    state.confirm_hold(hold["holdId"], idempotency_key="conf-key")
    assert state.query_booking_by_idempotency_key("conf-key") is not None


def test_by_key_without_confirm_returns_none(state):
    """只有预占、没落单 → 查不到挂号单，对账据此收敛为 FAILED。"""
    state.hold_slot(DATE, "上午", DEPT, idempotency_key="hold-only")
    assert state.query_booking_by_idempotency_key("hold-only") is None


def test_by_key_unknown_returns_none(state):
    assert state.query_booking_by_idempotency_key("never") is None
    assert state.query_booking_by_idempotency_key("") is None


# ---------- 4. 持久化 ----------

def test_new_boundary_state_roundtrips(state):
    """幂等结果 / 键→单映射 / TTL 都必须落盘，重启后不丢。"""
    hold, _ = state.hold_slot(DATE, "上午", DEPT, idempotency_key="hold-1")
    booking, _ = state.confirm_hold(hold["holdId"], idempotency_key="conf-1")
    state.cancel_booking(booking["clinicMasterId"], idempotency_key="can-1")
    payload = state.to_dict()

    restored = LocalHisState()
    restored.load_dict(json.loads(json.dumps(payload)))
    assert restored.write_results == state.write_results
    assert restored.booking_by_key == state.booking_by_key
    assert restored.query_booking_by_idempotency_key("hold-1") is not None
    ok, err = restored.cancel_booking(
        booking["clinicMasterId"], idempotency_key="can-1"
    )
    assert ok and not err, "重启后同键重放应仍成功"


def test_body_limit_matches_main_pipeline():
    """本地替身与主管线 BodySizeLimitMiddleware 用同一个上限。"""
    assert MAX_BODY_BYTES == 256 * 1024
