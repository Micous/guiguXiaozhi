"""本地 HIS 替身单测 —— 保证"本地预约挂号流程可用"这件事本身被测试覆盖。

重点验证**号源语义**（这是本地 HIS 替身存在的全部意义）：
  * 预占 → 确认 → 号源真的被扣；
  * 释放 → 号源归还；
  * 退号 → 号源归还；
  * 号满 → 明确失败（不是"永远有号"的假象）；
  * 幂等 → 同一键只处理一次；
  * 状态持久化 → 重启不丢号源状态。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from xiaozhi.service.local_his import (  # noqa: E402
    LocalHisServer,
    LocalHisState,
)


@pytest.fixture
def state():
    """一个排好班的状态（周三/2026-12-09 出诊）。"""
    st = LocalHisState()
    st.add_roster("呼吸内科", "徐国兴", 3)
    return st


def _query(st, date_str="2026-12-09", slot="上午", dept="呼吸内科"):
    slots = st.query_slots(date_str, slot, dept)
    return slots


def test_slots_are_expanded_for_matching_weekday(state):
    """2026-12-09 是周三 → 有号表实例。"""
    slots = _query(state)
    assert len(slots) == 1
    assert slots[0].doctor_name == "徐国兴"
    assert slots[0].dept_name == "呼吸内科"
    assert slots[0].limit_num == 3


def test_no_slots_on_non_matching_weekday(state):
    """2026-12-12 是周六 → 排班不含周六 → 无号。"""
    assert _query(state, "2026-12-12") == []


def test_hold_consumes_pre_num(state):
    """预占 → current_pre_num +1，剩余数减 1。"""
    before = _query(state)[0].remaining
    result, error = state.hold_slot("2026-12-09", "上午", "呼吸内科")
    assert not error
    assert result["holdId"]
    slot = _query(state)[0]
    assert slot.current_pre_num == 1
    assert slot.remaining == before - 1
    assert slot.current_num == 0  # 还没真正挂号


def test_confirm_converts_pre_to_registered(state):
    """确认 → 预占数减、已挂号数加（号真正被扣）。"""
    result, _ = state.hold_slot("2026-12-09", "上午", "呼吸内科")
    confirmed, error = state.confirm_hold(result["holdId"], patient_ref="ref-1")
    assert not error
    assert confirmed["clinicMasterId"] == result["clinicMasterId"]
    slot = _query(state)[0]
    assert slot.current_num == 1
    assert slot.current_pre_num == 0
    assert slot.remaining == slot.limit_num - 1


def test_release_returns_slot(state):
    """释放预占 → 号源归还（预占数减）。"""
    result, _ = state.hold_slot("2026-12-09", "上午", "呼吸内科")
    released, error = state.release_hold(result["holdId"])
    assert released and not error
    slot = _query(state)[0]
    assert slot.current_pre_num == 0
    assert slot.remaining == slot.limit_num


def test_cancel_booking_returns_slot(state):
    """退号 → 已挂号数减，号源归还（用户取消的真实语义）。"""
    result, _ = state.hold_slot("2026-12-09", "上午", "呼吸内科")
    confirmed, _ = state.confirm_hold(result["holdId"])
    slot = _query(state)[0]
    assert slot.current_num == 1

    cancelled, error = state.cancel_booking(confirmed["clinicMasterId"])
    assert cancelled and not error
    slot = _query(state)[0]
    assert slot.current_num == 0
    assert slot.remaining == slot.limit_num


def test_cancel_twice_is_rejected(state):
    """重复退号 → 明确失败（不能把号扣成负数）。"""
    result, _ = state.hold_slot("2026-12-09", "上午", "呼吸内科")
    confirmed, _ = state.confirm_hold(result["holdId"])
    state.cancel_booking(confirmed["clinicMasterId"])
    again, error = state.cancel_booking(confirmed["clinicMasterId"])
    assert not again
    assert "已退号" in error
    assert _query(state)[0].current_num == 0


def test_slots_run_out(state):
    """号源耗尽 → 明确失败（不是"永远有号"的假象）。"""
    limit = _query(state)[0].limit_num
    holds = []
    for _ in range(limit):
        result, error = state.hold_slot("2026-12-09", "上午", "呼吸内科")
        assert not error
        holds.append(result)
    result, error = state.hold_slot("2026-12-09", "上午", "呼吸内科")
    assert result is None
    assert "号源已满" in error
    assert _query(state)[0].remaining == 0


def test_idempotency_key_holds_once(state):
    """同一幂等键 → 只处理一次（号只被占一次）。"""
    first, _ = state.hold_slot("2026-12-09", "上午", "呼吸内科", idempotency_key="k1")
    second, _ = state.hold_slot("2026-12-09", "上午", "呼吸内科", idempotency_key="k1")
    assert first["holdId"] == second["holdId"]
    assert _query(state)[0].current_pre_num == 1


def test_confirm_idempotency(state):
    """确认的幂等键 → 不重复挂号（current_num 不翻倍）。"""
    result, _ = state.hold_slot("2026-12-09", "上午", "呼吸内科")
    state.confirm_hold(result["holdId"], idempotency_key="c1")
    state.confirm_hold(result["holdId"], idempotency_key="c1")
    assert _query(state)[0].current_num == 1


def test_state_persists_across_restart():
    """状态持久化 → 重启后号源状态不丢（本地可用性要求）。"""
    # 用项目内临时目录（沙箱会拦 pytest 的 tmp_path fixture 在 /private/var 下建目录）
    tmp_dir = ROOT / "tests" / "_tmp_local_his"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    state_file = tmp_dir / "local_his.json"
    if state_file.exists():
        state_file.unlink()
    st = LocalHisState()
    st.add_roster("呼吸内科", "徐国兴", 3)
    server = LocalHisServer(st, persist_path=state_file)
    server.start()
    try:
        result, _ = st.hold_slot("2026-12-09", "上午", "呼吸内科")
        st.confirm_hold(result["holdId"])
    finally:
        server.stop()

    assert state_file.exists()
    payload = json.loads(state_file.read_text(encoding="utf-8"))
    assert payload["regist"], "号表实例未持久化"
    assert payload["bookings"], "挂号单未持久化"

    # 模拟重启：新建 state 并从文件恢复
    restored = LocalHisState()
    restored.load_dict(payload)
    slots = restored.query_slots("2026-12-09", "上午", "呼吸内科")
    assert slots[0].current_num == 1, "重启后号源状态丢失"
    state_file.unlink(missing_ok=True)
    try:
        tmp_dir.rmdir()
    except OSError:
        pass


def test_server_exposes_health_and_unified_envelope():
    """本地 HIS 的 HTTP 契约：统一响应包 + 健康检查。"""
    import urllib.request

    st = LocalHisState()
    st.add_roster("呼吸内科", "徐国兴", 3)
    server = LocalHisServer(st)
    url = server.start()
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url + "/health", timeout=5) as response:
            payload = json.loads(response.read().decode())
        assert payload["code"] == 200
        assert payload["data"]["service"] == "local-his"

        with opener.open(
            url + "/clinicForRegist/list?clinicDate=2026-12-09&deptName=%E5%91%BC%E5%90%B8%E5%86%85%E7%A7%91",
            timeout=5,
        ) as response:
            payload = json.loads(response.read().decode())
        assert payload["code"] == 200
        assert payload["data"][0]["doctor_name"] == "徐国兴"
    finally:
        server.stop()


def test_business_failure_uses_code_not_http_status():
    """业务失败：HTTP 200 但 code≠200（对齐 HIS 统一响应包）。"""
    import urllib.error
    import urllib.request

    st = LocalHisState()
    st.add_roster("呼吸内科", "徐国兴", 1)
    server = LocalHisServer(st)
    url = server.start()
    try:
        # 先占满
        st.hold_slot("2026-12-09", "上午", "呼吸内科")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(
            url + "/clinicForRegist/hold",
            data=json.dumps(
                {
                    "clinicDate": "2026-12-09",
                    "timeDesc": "上午",
                    "deptName": "呼吸内科",
                }
            ).encode(),
            method="POST",
        )
        req.add_header("Content-Type", "application/json")
        with opener.open(req, timeout=5) as response:
            payload = json.loads(response.read().decode())
            assert response.status == 200, "业务失败也返回 HTTP 200"
        assert payload["code"] != 200, "业务失败必须用 code 表达"
        assert "号源已满" in payload["msg"]
    finally:
        server.stop()
