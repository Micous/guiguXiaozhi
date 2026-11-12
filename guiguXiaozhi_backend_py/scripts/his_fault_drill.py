"""HIS 边界**真机**演练 —— 不启 fake server，直接打运行中的 HIS 沙箱。

用法::

    # 先起沙箱（Docker MySQL + HTTP 服务）
    docker compose -f his_sandbox/docker-compose.yml up -d
    ./.venv/bin/python -m his_sandbox.seed --reset
    ./.venv/bin/python -m his_sandbox.server --no-seed

    # 再跑演练
    ./.venv/bin/python scripts/his_fault_drill.py

覆盖的边界（每条都对应一个真实的线上事故形态）：

======================  ================================================
故障                      期望
======================  ================================================
正常基线                  成功
timeout                   HisUncertainError（提交后断响应 → UNKNOWN）
http500                   HisUncertainError
system_busy(code=503)     HisUncertainError（**不能**误判为明确失败）
business_fail(号源已满)   HisRejectedError（明确失败，不重试）
http429                   HisUnavailableError（确定没被处理）
auth_fail(code=401)       HisAuthError
malformed_json            HisUncertainError（协议违规不是"没号"）
oversize                  HisUncertainError（响应体超上限）
redirect                  HisRejectedError（不跟随重定向）
connection_reset          HisUncertainError（半路断连）
slow_drip                 HisUncertainError（总时限拦住慢吐字节）
flaky                     成功（退避重试把抖动收敛掉）
timeout_once              成功（同幂等键重放，拿到首次结果）
pii_leak                  异常消息/详情不含身份证与 token
connection_refused        HisUnavailableError（**不是** UNKNOWN）
circuit_open              HisCircuitOpenError（快速失败，不发请求）
byKey 查单                无单号也能靠幂等键判断有没有落单
重复退号/重复释放          幂等返回成功，不报"已退号/已释放"
======================  ================================================

**为什么不用 pytest fake server**：fake 是"我们以为 HIS 会怎么错"，
真机沙箱走的是完整 HTTP + MySQL 事务链路，能暴露代理、重定向、连接半开、
响应体分块、幂等表并发这些协议层的真实问题。
"""

from __future__ import annotations

import argparse
import sys
import time
import uuid
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from xiaozhi.repositories.appointment_repo import Appointment  # noqa: E402
from xiaozhi.service.his_client import (  # noqa: E402
    HisAuthError,
    HisCircuitOpenError,
    HisClient,
    HisError,
    HisRejectedError,
    HisUnavailableError,
    HisUncertainError,
)
from xiaozhi.service.his_resilience import CircuitBreaker, RetryPolicy  # noqa: E402

VALID_ID = "110101199003074567"  # 通过 GB 11643 校验位
DEPT = "呼吸内科"
CLINIC_DATE = "2026-11-13"
TIME_DESC = "上午"


def make_client(
    base_url: str,
    token: str,
    fault: Optional[str] = None,
    *,
    timeout: float = 0.5,
    attempts: int = 2,
    breaker: Optional[CircuitBreaker] = None,
) -> HisClient:
    headers = {"X-His-Fault": fault} if fault else {}
    return HisClient(
        base_url,
        token=token,
        timeout=timeout,
        headers=headers,
        retry_policy=RetryPolicy(max_attempts=attempts),
        breaker=breaker,
    )


def reset_faults(base_url: str, token: str) -> None:
    """重置沙箱里的 flaky / timeout_once 命中计数，保证演练可重复。"""
    HisClient(base_url, token=token, timeout=2.0).request(
        "POST", "/_admin/reset-fault", body={}
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="HIS 边界真机演练")
    parser.add_argument("--base", default="http://127.0.0.1:18081")
    parser.add_argument("--token", default="his-sandbox-token")
    args = parser.parse_args()
    base, token = args.base, args.token

    rows: List[Tuple[str, str, str, str, float, bool]] = []

    def record(
        label: str,
        expect: str,
        fn: Callable[[], str],
        *,
        note: str = "",
    ) -> str:
        started = time.monotonic()
        try:
            got = fn()
        except HisError as exc:
            got = type(exc).__name__
            if label.startswith("PII"):
                got += f" detail={exc.detail[:70]}"
        except Exception as exc:  # 未分类异常 = 演练失败
            got = f"UNEXPECTED:{type(exc).__name__}:{exc}"
        elapsed = time.monotonic() - started
        ok = got == expect or (expect == "OK" and got == "OK")
        rows.append((label, expect, got, note, elapsed, ok))
        return got

    def probe(label: str, fault: Optional[str], expect: str, **kw) -> str:
        cli = make_client(base, token, fault, **kw)

        def action() -> str:
            cli.query_slots({"deptName": DEPT})
            return "OK"

        return record(label, expect, action)

    # ---------- 0. 服务可达性 ----------
    try:
        reset_faults(base, token)
    except Exception as exc:
        print(f"无法连接 HIS 沙箱 {base}：{exc}")
        print("请先启动：docker compose -f his_sandbox/docker-compose.yml up -d "
              "&& python -m his_sandbox.server --no-seed")
        return 2

    # ---------- 1. 错误分类矩阵 ----------
    probe("基线-正常查询", None, "OK")
    probe("超时-提交后不响应", "timeout", "HisUncertainError")
    probe("HTTP 5xx", "http500", "HisUncertainError")
    probe("业务码503-数据库超时", "system_busy", "HisUncertainError")
    probe("业务拒绝-号源已满", "business_fail", "HisRejectedError")
    probe("HTTP 429 限流", "http429", "HisUnavailableError")
    probe("业务码401-令牌过期", "auth_fail", "HisAuthError")
    probe("畸形响应-HTML", "malformed_json", "HisUncertainError")
    probe("响应体超1MB", "oversize", "HisUncertainError")
    probe("302 重定向", "redirect", "HisRejectedError")
    probe("连接中途断开", "connection_reset", "HisUncertainError")
    probe("慢吐字节-总时限", "slow_drip", "HisUncertainError", timeout=0.4, attempts=2)

    # ---------- 2. 网络波动的收敛 ----------
    reset_faults(base, token)
    probe("抖动-前2次500后恢复", "flaky", "OK", attempts=3)

    reset_faults(base, token)
    key = f"drill-timeout-once-{uuid.uuid4().hex[:8]}"

    def replay_after_timeout() -> str:
        cli = make_client(base, token, "timeout_once", timeout=0.4, attempts=2)
        hold = cli.hold_slot(
            {"deptName": DEPT, "clinicDate": CLINIC_DATE, "timeDesc": TIME_DESC},
            idempotency_key=key,
        )
        return "OK" if hold.held else "HELD_FALSE"

    record("丢一次响应-同键重放", "OK", replay_after_timeout,
           note="HIS 侧只预占一次，重试拿到首次结果")

    # ---------- 3. 确定没送达 ≠ 结果不确定 ----------
    def connection_refused() -> str:
        cli = HisClient(
            "http://127.0.0.1:19999", token=token, timeout=0.3,
            retry_policy=RetryPolicy(max_attempts=1),
        )
        cli.query_slots({"deptName": DEPT})
        return "OK"

    record("连接被拒绝-端口无人监听", "HisUnavailableError", connection_refused)

    # ---------- 4. 熔断 ----------
    def circuit_open() -> str:
        breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=60.0)
        for _ in range(5):
            try:
                probe_client = make_client(
                    base, token, "http500", timeout=0.3, attempts=1,
                    breaker=breaker,
                )
                probe_client.query_slots({"deptName": DEPT})
            except HisError:
                pass
        cli = make_client(base, token, None, breaker=breaker)
        cli.query_slots({"deptName": DEPT})
        return "OK"

    record("连续故障-熔断快速失败", "HisCircuitOpenError", circuit_open,
           note="OPEN 期间请求未发出，不制造 UNKNOWN")

    # ---------- 5. PII 与凭据不外泄 ----------
    def pii_case() -> str:
        cli = make_client(base, token, "pii_leak", timeout=1.0, attempts=1)
        try:
            cli.query_slots({"deptName": DEPT})
        except HisError as exc:
            message, detail = str(exc), exc.detail
            assert VALID_ID not in message, "用户可见消息泄漏身份证号"
            assert VALID_ID not in detail, "日志详情未脱敏"
            assert "abc123secret" not in detail, "日志详情泄漏 token"
            return "OK"
        return "NO_ERROR"

    record("PII-错误正文脱敏", "OK", pii_case)

    # ---------- 6. 幂等键查单（对账死结） ----------
    def by_key_reconcile() -> str:
        cli = make_client(base, token, None, timeout=3.0)
        idem = f"drill-bykey-{uuid.uuid4().hex[:8]}"
        local_ref = Appointment.make_patient_ref(VALID_ID, "salt")
        pat = cli.create_patient(
            {"idNo": VALID_ID, "name": "张三", "patientRef": local_ref},
            idempotency_key=idem + "-pat",
        )
        # 患者主档引用必须**以 HIS 返回值为准**：本地 sha256 派生的 32 位
        # patient_ref 与院方主档编码体系不同，直接拿去落单会被"患者主档
        # 不存在"拒绝（真机联调暴露的对接缺陷）。
        patient_ref = str(pat.get("patientRef") or local_ref)
        hold = cli.hold_slot(
            {"deptName": DEPT, "clinicDate": CLINIC_DATE, "timeDesc": TIME_DESC},
            idempotency_key=idem,
        )
        before = cli.query_booking_by_idempotency_key(idem)
        if before.found:
            return "HOLD_SHOULD_NOT_HAVE_BOOKING"
        cli.confirm_hold(
            {"holdId": hold.hold_id, "patientRef": patient_ref},
            idempotency_key=idem + "-confirm",
        )
        after = cli.query_booking_by_idempotency_key(idem)
        if not after.found or after.status != "REGISTERED":
            return f"BAD_STATUS:{after.found}/{after.status}"
        return "OK"

    record("按幂等键查单-无单号可对账", "OK", by_key_reconcile)

    # ---------- 7. 重复退号 / 重复释放要幂等 ----------
    def idempotent_writes() -> str:
        cli = make_client(base, token, None, timeout=3.0)
        suffix = uuid.uuid4().hex[:8]
        local_ref = Appointment.make_patient_ref(VALID_ID, "salt")
        pat = cli.create_patient(
            {"idNo": VALID_ID, "name": "张三", "patientRef": local_ref},
            idempotency_key=f"pat-{suffix}",
        )
        patient_ref = str(pat.get("patientRef") or local_ref)
        # 释放：同一个不存在的 hold 重复两次，都应视为"已释放"
        for _ in range(2):
            cli.release_hold(f"hold-{suffix}", idempotency_key=f"rel-{suffix}")
        # 退号：同一单重复两次，第二次不能报"该挂号单已退号"
        hold = cli.hold_slot(
            {"deptName": DEPT, "clinicDate": CLINIC_DATE, "timeDesc": TIME_DESC},
            idempotency_key=f"hold-{suffix}",
        )
        cli.confirm_hold(
            {"holdId": hold.hold_id, "patientRef": patient_ref},
            idempotency_key=f"conf-{suffix}",
        )
        for _ in range(2):
            cli.cancel_booking(
                hold.external_booking_id, idempotency_key=f"cancel-{suffix}"
            )
        return "OK"

    record("重复释放/重复退号幂等", "OK", idempotent_writes)

    # ---------- 汇总 ----------
    print()
    print(f"{'边界场景':<28} {'期望':<24} {'实际':<24} {'耗时':>6}  结果")
    print("-" * 100)
    for label, expect, got, note, elapsed, ok in rows:
        flag = "PASS" if ok else "FAIL"
        print(f"{label:<28} {expect:<24} {got[:24]:<24} {elapsed:>5.2f}s  {flag}")
        if note and not ok:
            print(f"    ↳ {note}")
    passed = sum(1 for r in rows if r[5])
    print("-" * 100)
    print(f"真机演练：{passed}/{len(rows)} 通过（目标 {base}）")
    if passed != len(rows):
        print("\n失败项说明：")
        for label, expect, got, note, _, ok in rows:
            if not ok:
                print(f"  - {label}: 期望 {expect}，实际 {got}" + (f"（{note}）" if note else ""))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
