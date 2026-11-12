"""本地 HIS 替身边界**真机**演练 —— 真实 HTTP 服务 + 真实客户端，不用 fake。

用法::

    # 起两个独立进程（18082 正常 TTL / 18083 短 TTL 用于验证过期回收）
    ./.venv/bin/python -m xiaozhi.service.local_his 18082 &
    LOCAL_HIS_HOLD_TTL=1 ./.venv/bin/python -m xiaozhi.service.local_his 18083 &
    ./.venv/bin/python scripts/local_his_fault_drill.py

覆盖的边界（本地替身 ``slot_service_impl=local_his`` 是 8081 当前模式，
这些坑在沙箱里补过、替身里没补就会在真实链路上复现）：

========================  ================================================
场景                        期望
========================  ================================================
正常全流程                  查号源 → 预占 → 确认 → 查单 → 退号 全通
hold/confirm 幂等重放       同键返回首次结果，号源只扣一次
重复释放（同键）            两次都成功，不报"预占不存在或已释放"
重复退号（同键）            两次都成功，不报"该挂号单已退号"
重复退号（无键）            第二次明确拒绝（严格语义保留）
byKey 查单                  hold 键 / confirm 键都能查到挂号单
byKey 未知键                404 → found=False（对账可收敛为 FAILED）
timeout 故障                业务已提交但响应丢失 → HisUncertainError
timeout 后重放              同键重放拿到首次结果，不重复占号
http500 故障                业务已提交但返回 500 → HisUncertainError
http500 后重放              同键重放成功
请求体超限                  413 → HisRejectedError（明确拒绝，非 UNKNOWN）
非法 Content-Length         413 → HisRejectedError
畸形 JSON                   明确业务失败，服务端不崩
连接拒绝                    HisUnavailableError（**不是** UNKNOWN）
预占 TTL 过期                号源自动归还号池
TTL 过期后确认               明确拒绝"预占不存在或已过期"
========================  ================================================

**为什么必须真机**：本地替身的边界全在 HTTP 层（Content-Length、响应
帧、幂等头、socket 超时），用 mock 断言测不出"服务端不读 body 就返 413"
或"业务已提交却断了连接"这类真实协议行为。
"""

from __future__ import annotations

import argparse
import http.client
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Callable, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from xiaozhi.service.his_client import (  # noqa: E402
    HisClient,
    HisError,
    HisRejectedError,
    HisUnavailableError,
    HisUncertainError,
)
from xiaozhi.service.his_resilience import RetryPolicy  # noqa: E402

DEPT = "呼吸内科"
CLINIC_DATE = "2026-12-09"  # 周三，本地替身默认排班日
TIME_DESC = "上午"


def make_client(
    base: str,
    *,
    fault: str = "",
    timeout: float = 0.6,
    attempts: int = 1,
) -> HisClient:
    headers = {"X-His-Fault": fault} if fault else {}
    return HisClient(
        base,
        token="local-his",
        timeout=timeout,
        headers=headers,
        retry_policy=RetryPolicy(max_attempts=attempts),
    )


def hold_payload() -> dict:
    return {
        "clinicDate": CLINIC_DATE,
        "timeDesc": TIME_DESC,
        "deptName": DEPT,
    }


def query_params() -> dict:
    """本地替身的号表按日期展开 —— 不带 clinicDate 查不到任何号源。"""
    return {
        "clinicDate": CLINIC_DATE,
        "timeDesc": TIME_DESC,
        "deptName": DEPT,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="本地 HIS 替身边界真机演练")
    parser.add_argument("--base", default="http://127.0.0.1:18082")
    parser.add_argument("--ttl-base", default="http://127.0.0.1:18083")
    args = parser.parse_args()
    base, ttl_base = args.base, args.ttl_base

    rows: List[Tuple[str, str, str, float, bool]] = []

    def record(label: str, expect: str, fn: Callable[[], str]) -> None:
        started = time.monotonic()
        try:
            got = fn()
        except HisError as exc:
            got = type(exc).__name__
        except Exception as exc:  # 未分类异常 = 演练失败
            got = f"UNEXPECTED:{type(exc).__name__}:{exc}"
        elapsed = time.monotonic() - started
        ok = got == expect
        rows.append((label, expect, got, elapsed, ok))

    def unique(prefix: str = "k") -> str:
        return f"{prefix}-{uuid.uuid4().hex[:10]}"

    # ---------- 1. 正常全流程 ----------
    def full_flow() -> str:
        cli = make_client(base)
        slots = cli.query_slots(query_params())
        assert slots and sum(s.remaining for s in slots) > 0, "号源为空"
        hold = cli.hold_slot(hold_payload(), idempotency_key=unique("hold"))
        assert hold.held and hold.hold_id, "预占失败"
        booking = cli.confirm_hold(
            {"holdId": hold.hold_id, "patientRef": "ref-drill"},
            idempotency_key=unique("conf"),
        )
        assert booking.confirmed and booking.external_booking_id, "落单失败"
        found = cli.query_booking(booking.external_booking_id)
        assert found.found, "查单失败"
        cli.cancel_booking(
            booking.external_booking_id, idempotency_key=unique("can")
        )
        return "OK"

    record("正常全流程（查→占→落→查→退）", "OK", full_flow)

    # ---------- 2. hold / confirm 幂等重放 ----------
    def hold_replay() -> str:
        cli = make_client(base)
        key = unique("hold")
        first = cli.hold_slot(hold_payload(), idempotency_key=key)
        second = cli.hold_slot(hold_payload(), idempotency_key=key)
        assert first.hold_id == second.hold_id, "同键产生了两个预占"
        cli.release_hold(first.hold_id, idempotency_key=unique("rel"))
        return "OK"

    record("hold 同幂等键只占一次", "OK", hold_replay)

    def confirm_replay() -> str:
        cli = make_client(base)
        hold = cli.hold_slot(hold_payload(), idempotency_key=unique("hold"))
        key = unique("conf")
        first = cli.confirm_hold(
            {"holdId": hold.hold_id}, idempotency_key=key
        )
        second = cli.confirm_hold(
            {"holdId": hold.hold_id}, idempotency_key=key
        )
        assert first.external_booking_id == second.external_booking_id, "重复落单"
        cli.cancel_booking(
            first.external_booking_id, idempotency_key=unique("can")
        )
        return "OK"

    record("confirm 同幂等键只落一单", "OK", confirm_replay)

    # ---------- 3. release / cancel 幂等 ----------
    def release_replay() -> str:
        cli = make_client(base)
        hold = cli.hold_slot(hold_payload(), idempotency_key=unique("hold"))
        key = unique("rel")
        assert cli.release_hold(hold.hold_id, idempotency_key=key), "首次释放失败"
        assert cli.release_hold(hold.hold_id, idempotency_key=key), "重放被误判失败"
        return "OK"

    record("重复释放（同键）幂等成功", "OK", release_replay)

    def cancel_replay() -> str:
        cli = make_client(base)
        hold = cli.hold_slot(hold_payload(), idempotency_key=unique("hold"))
        booking = cli.confirm_hold(
            {"holdId": hold.hold_id}, idempotency_key=unique("conf")
        )
        key = unique("can")
        assert cli.cancel_booking(booking.external_booking_id, idempotency_key=key)
        assert cli.cancel_booking(booking.external_booking_id, idempotency_key=key)
        return "OK"

    record("重复退号（同键）幂等成功", "OK", cancel_replay)

    def raw_cancel_twice() -> str:
        """裸 HTTP 不带幂等头 → 服务端严格语义：第二次明确拒绝。

        注意：走 ``HisClient`` 时写操作会**自动派生**稳定幂等键，所以
        "无键"这条边界只有裸 HTTP 才测得到（这本身也是韧性层的行为）。
        """
        cli = make_client(base)
        hold = cli.hold_slot(hold_payload(), idempotency_key=unique("hold"))
        booking = cli.confirm_hold(
            {"holdId": hold.hold_id}, idempotency_key=unique("conf")
        )
        body = json.dumps({"clinicMasterId": booking.external_booking_id}).encode()

        def raw_cancel() -> dict:
            # BaseHTTPRequestHandler 默认 HTTP/1.0，每次请求新建连接
            conn = http.client.HTTPConnection(
                "127.0.0.1", _port(base), timeout=3
            )
            try:
                conn.request(
                    "POST", "/currentRegister/cancel", body=body,
                    headers={"Content-Type": "application/json"},
                )
                return json.loads(conn.getresponse().read().decode())
            finally:
                conn.close()

        first = raw_cancel()
        assert first.get("code") == 200, "首次退号失败"
        second = raw_cancel()
        assert second.get("code") != 200, "无键重复退号应明确拒绝"
        assert "已退号" in second.get("msg", ""), second
        return "OK"

    record("重复退号（裸 HTTP 无键）明确拒绝", "OK", raw_cancel_twice)

    # ---------- 4. byKey 查单（对账死结） ----------
    def by_key_hold() -> str:
        cli = make_client(base)
        key = unique("hold")
        hold = cli.hold_slot(hold_payload(), idempotency_key=key)
        cli.confirm_hold(
            {"holdId": hold.hold_id}, idempotency_key=unique("conf")
        )
        found = cli.query_booking_by_idempotency_key(key)
        assert found.found and found.status == "REGISTERED", "hold 键查不到挂号单"
        return "OK"

    record("byKey 用 hold 键查到挂号单", "OK", by_key_hold)

    def by_key_confirm() -> str:
        cli = make_client(base)
        hold = cli.hold_slot(hold_payload(), idempotency_key=unique("hold"))
        key = unique("conf")
        cli.confirm_hold({"holdId": hold.hold_id}, idempotency_key=key)
        found = cli.query_booking_by_idempotency_key(key)
        assert found.found, "confirm 键查不到挂号单"
        return "OK"

    record("byKey 用 confirm 键查到挂号单", "OK", by_key_confirm)

    def by_key_unknown() -> str:
        cli = make_client(base)
        found = cli.query_booking_by_idempotency_key(unique("never"))
        assert not found.found, "未知键不该查到单"
        return "OK"

    record("byKey 未知键 → found=False", "OK", by_key_unknown)

    # ---------- 5. 业务已提交但响应丢失 ----------
    def timeout_then_replay() -> str:
        fault_cli = make_client(base, fault="timeout", timeout=0.5)
        key = unique("hold")
        try:
            fault_cli.hold_slot(hold_payload(), idempotency_key=key)
            return "NO_ERROR"
        except HisUncertainError:
            pass
        # 网络恢复后，用同一业务动作键重放 → 拿到首次结果
        replay = make_client(base).hold_slot(
            hold_payload(), idempotency_key=key
        )
        assert replay.held and replay.hold_id, "重放没拿到首次预占结果"
        return "OK"

    record("timeout 故障 → UNKNOWN", "OK", lambda: _expect(
        "HisUncertainError",
        lambda: make_client(base, fault="timeout", timeout=0.5).hold_slot(
            hold_payload(), idempotency_key=unique("hold")
        ),
    ))
    record("timeout 后同键重放收敛", "OK", timeout_then_replay)

    def http500_then_replay() -> str:
        key = unique("hold")
        try:
            make_client(base, fault="http500", timeout=1.5).hold_slot(
                hold_payload(), idempotency_key=key
            )
            return "NO_ERROR"
        except HisUncertainError:
            pass
        replay = make_client(base).hold_slot(
            hold_payload(), idempotency_key=key
        )
        assert replay.held, "500 后重放没拿到首次结果"
        return "OK"

    record("http500 故障 → UNKNOWN", "OK", lambda: _expect(
        "HisUncertainError",
        lambda: make_client(base, fault="http500", timeout=1.5).hold_slot(
            hold_payload(), idempotency_key=unique("hold")
        ),
    ))
    record("http500 后同键重放收敛", "OK", http500_then_replay)

    # ---------- 6. HTTP 协议边界 ----------
    def oversize_body() -> str:
        cli = make_client(base, timeout=2.0)
        # 300KB 远超 256KB 上限
        cli.request(
            "POST", "/clinicForRegist/hold",
            body={"deptName": "x" * (300 * 1024)},
            idempotency_key=unique("big"),
        )
        return "NO_ERROR"

    record("请求体超限 → 413 明确拒绝", "HisRejectedError", oversize_body)

    def bad_content_length() -> str:
        conn = http.client.HTTPConnection("127.0.0.1", _port(base), timeout=3)
        conn.putrequest("POST", "/clinicForRegist/hold")
        conn.putheader("Content-Length", "not-a-number")
        conn.putheader("Content-Type", "application/json")
        conn.endheaders()
        conn.send(b"{}")
        resp = conn.getresponse()
        code = resp.status
        resp.read()
        conn.close()
        assert code == 413, f"期望 413，实际 {code}"
        return "OK"

    record("非法 Content-Length → 413", "OK", bad_content_length)

    def malformed_json() -> str:
        conn = http.client.HTTPConnection("127.0.0.1", _port(base), timeout=3)
        body = b"{not-json"
        conn.request(
            "POST", "/clinicForRegist/hold", body=body,
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        code = resp.status
        payload = json.loads(resp.read().decode("utf-8"))
        conn.close()
        assert code == 200 and payload.get("code") != 200, (
            "畸形 JSON 应解析为空体后明确业务拒绝，而不是 500"
        )
        return "OK"

    record("畸形 JSON → 明确业务失败（不崩）", "OK", malformed_json)

    def connection_refused() -> str:
        cli = make_client("http://127.0.0.1:1", timeout=0.5)
        cli.query_slots(query_params())
        return "NO_ERROR"

    record("连接拒绝 → 明确不可用", "HisUnavailableError", connection_refused)

    # ---------- 7. 预占 TTL（短 TTL 服务） ----------
    def ttl_expiry() -> str:
        cli = make_client(ttl_base, timeout=2.0)
        before = sum(s.remaining for s in cli.query_slots(query_params()))
        cli.hold_slot(hold_payload(), idempotency_key=unique("ttl"))
        time.sleep(1.4)  # TTL=1s，等它过期
        after = sum(s.remaining for s in cli.query_slots(query_params()))
        assert after == before, f"号源未归还：{before} → {after}"
        return "OK"

    record("预占 TTL 过期 → 号源归还", "OK", ttl_expiry)

    def confirm_after_ttl() -> str:
        cli = make_client(ttl_base, timeout=2.0)
        hold = cli.hold_slot(hold_payload(), idempotency_key=unique("ttl"))
        time.sleep(1.4)
        cli.confirm_hold(
            {"holdId": hold.hold_id}, idempotency_key=unique("conf")
        )
        return "NO_ERROR"

    record(
        "TTL 过期后确认 → 明确拒绝", "HisRejectedError", confirm_after_ttl
    )

    # ---------- 汇总 ----------
    width = max(len(r[0]) for r in rows)
    passed = 0
    print("\n=== 本地 HIS 替身边界真机演练 ===")
    for label, expect, got, elapsed, ok in rows:
        flag = "PASS" if ok else "FAIL"
        passed += int(ok)
        print(
            f"[{flag}] {label:<{width}}  "
            f"expect={expect:<22} got={got:<28} {elapsed:5.2f}s"
        )
    print(f"\n{passed}/{len(rows)} passed")
    return 0 if passed == len(rows) else 1


def _expect(name: str, fn: Callable[[], object]) -> str:
    """断言抛出指定 HisError；未抛或抛错类型都算演练失败。"""
    try:
        fn()
    except HisError as exc:
        return "OK" if type(exc).__name__ == name else type(exc).__name__
    return "NO_ERROR"


def _port(base: str) -> int:
    return int(base.rsplit(":", 1)[-1])


if __name__ == "__main__":
    raise SystemExit(main())
