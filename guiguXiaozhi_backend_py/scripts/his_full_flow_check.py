"""HIS 七类接口 + 全流程真机核验（对应 ``docs/ops-runbook.md`` 11.6）。

用法::

    docker compose -f his_sandbox/docker-compose.yml up -d
    ./.venv/bin/python -m his_sandbox.seed --reset
    ./.venv/bin/python -m his_sandbox.server --no-seed
    ./.venv/bin/python scripts/his_full_flow_check.py

**顺序不可换**：建档 → **用 HIS 返回的 patientRef** → 预占 → 落单 →
查单 → 退号。把本项目本地派生的 ``patient_ref`` 直接塞给 ``confirm_hold``
会被 HIS 拒绝「患者主档不存在」—— 这是引用体系不一致（runbook 11.7），
不是接口缺失。演练脚本刻意验证这一点，防止后续误改成"绕开主档"。
"""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path
from typing import Callable, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from xiaozhi.service.his_client import HisClient  # noqa: E402

VALID_ID = "110101199003074567"  # 通过 GB 11643 校验位
DEPT = "呼吸内科"
CLINIC_DATE = "2026-11-13"
TIME_DESC = "上午"


def main() -> int:
    parser = argparse.ArgumentParser(description="HIS 七类接口真机核验")
    parser.add_argument("--base", default="http://127.0.0.1:18081")
    parser.add_argument("--token", default="his-sandbox-token")
    args = parser.parse_args()

    cli = HisClient(args.base, token=args.token, timeout=5.0)
    suffix = uuid.uuid4().hex[:8]
    rows: List[Tuple[str, str, bool]] = []
    context: dict = {}

    def step(label: str, fn: Callable[[], str]) -> None:
        try:
            detail = fn()
            rows.append((label, detail, True))
        except Exception as exc:  # noqa: BLE001 - 核验脚本要报出所有失败
            rows.append((label, f"{type(exc).__name__}: {exc}", False))

    # 7-科室字典
    def depts() -> str:
        data = cli.request("GET", "/dept/list").data
        assert isinstance(data, list) and data, "科室字典为空"
        return f"{len(data)} 个科室"

    step("7 科室字典 GET /dept/list", depts)

    # 1-号源查询
    def slots() -> str:
        found = cli.query_slots(
            {"deptName": DEPT, "clinicDate": CLINIC_DATE, "timeDesc": TIME_DESC}
        )
        assert found, "号源查询为空"
        return (
            f"{len(found)} 条号表 / 剩余 "
            f"{sum(s.remaining for s in found)}"
        )

    step("1 号源查询 GET /clinicForRegist/list", slots)

    # 6-患者主档建档（不传 patientRef，由 HIS 分配）
    def create_patient() -> str:
        result = cli.create_patient(
            {"idNo": VALID_ID, "name": "张三"},
            idempotency_key=f"pat-{suffix}",
        )
        ref = str(result.get("patientRef") or "")
        assert ref, "HIS 建档未返回 patientRef"
        context["ref"] = ref
        return f"patientRef={ref}"

    step("6a 患者主档建档 POST /patMasterIndex/add", create_patient)

    # 6-患者主档查询
    def query_patient() -> str:
        data = cli.request(
            "GET", "/patMasterIndex/list", params={"idNo": VALID_ID}
        ).data
        assert isinstance(data, list) and data, "患者主档查询为空"
        return f"{len(data)} 条主档"

    step("6b 患者主档查询 GET /patMasterIndex/list", query_patient)

    # 2-预占
    def hold() -> str:
        result = cli.hold_slot(
            {"deptName": DEPT, "clinicDate": CLINIC_DATE, "timeDesc": TIME_DESC},
            idempotency_key=f"hold-{suffix}",
        )
        assert result.held and result.hold_id, "预占失败"
        context["hold"] = result.hold_id
        return f"holdId={result.hold_id}"

    step("2 预占 POST /clinicForRegist/hold", hold)

    # 3-落单（必须 HIS 返回的 patientRef）
    def confirm() -> str:
        ref = context.get("ref", "")
        hold_id = context.get("hold", "")
        assert ref and hold_id, "前置步骤失败"
        result = cli.confirm_hold(
            {"holdId": hold_id, "patientRef": ref},
            idempotency_key=f"conf-{suffix}",
        )
        assert result.confirmed and result.external_booking_id, "落单失败"
        context["booking"] = result.external_booking_id
        return result.external_booking_id

    step("3 落单 POST /currentRegister/add", confirm)

    # 5-查单
    def query_booking() -> str:
        result = cli.query_booking(context.get("booking", ""))
        assert result.found and result.status == "REGISTERED", "查单状态异常"
        return f"status={result.status}"

    step("5 查单 GET /currentRegister/{id}", query_booking)

    # 附加：按幂等键查单
    def by_key() -> str:
        result = cli.query_booking_by_idempotency_key(f"hold-{suffix}")
        assert result.found, "按幂等键查不到挂号单"
        return "found=True"

    step("8 按幂等键查单 GET /currentRegister/byKey/{key}", by_key)

    # 附加：挂号单列表
    def list_bookings() -> str:
        data = cli.request(
            "GET", "/clinicMaster/list", params={"idNo": VALID_ID}
        ).data
        assert isinstance(data, list), "挂号单列表结构异常"
        return f"{len(data)} 条挂号单"

    step("9 挂号单列表 GET /clinicMaster/list", list_bookings)

    # 4-退号
    def cancel() -> str:
        assert cli.cancel_booking(
            context.get("booking", ""), idempotency_key=f"can-{suffix}"
        ), "退号失败"
        return "cancelled=True"

    step("4 退号 POST /currentRegister/cancel", cancel)

    width = max(len(r[0]) for r in rows)
    passed = sum(1 for r in rows if r[2])
    print("\n=== HIS 七类接口 + 全流程真机核验 ===")
    for label, detail, ok in rows:
        print(f"[{'PASS' if ok else 'FAIL'}] {label:<{width}}  {detail}")
    print(f"\n{passed}/{len(rows)} passed（目标 {args.base}）")
    return 0 if passed == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
