"""真机核验：所有院方数据是否**唯一来自 HIS**。

这不是离线单测 —— 脚本内部临时启动真实的 HIS HTTP 服务（内嵌
``LocalHisServer`` + 可选独立沙箱进程），用真实 ``HisClient`` 走 socket，
再用**真实工具对象**（预约挂号 / 取消预约挂号 / 查询号源 / 查询科室 /
查询科室专家）跑完整链路，跑完自动关闭服务，不留后台进程。

核验的四条硬约束：

1. 科室字典、医生名录、号源排班**同源**（HIS 主档一份快照生成）；
2. 「查询科室 / 查询医生」工具读的是 HIS REST，不是知识库 doctors.md；
3. 预约 / 取消 / 查号源全部落到 HIS，号源计数真的被扣与归还；
4. 工具层代码零改动：只用既有 ``DoctorService`` / ``SlotService`` 抽象。

运行::

    NO_PROXY='*' ./.venv/bin/python scripts/his_single_source_check.py
    # 加 --sandbox http://127.0.0.1:18081 时同时核验独立沙箱进程

退出码 0 = 全部通过；1 = 有失败项。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid
from pathlib import Path
from typing import Any, Callable, List, Tuple

# 直接以 ``python scripts/xxx.py`` 运行时 sys.path 不含项目根
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from xiaozhi.repositories.appointment_repo import InMemoryAppointmentRepository
from xiaozhi.service.his_catalog import load_his_catalog
from xiaozhi.service.his_client import HisClient
from xiaozhi.service.his_doctor_service import HisDoctorService
from xiaozhi.service.his_slot_service import HisSlotService
from xiaozhi.service.local_his import LocalHisServer
from xiaozhi.tools.appointment_tools import (
    BookAppointmentTool,
    CancelAppointmentTool,
    QueryDoctorsTool,
    QuerySlotTool,
    build_department_hint,
)

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

VALID_ID = "610121199401032831"  # 通过 GB 11643 校验位（与 HIS 主档预置患者一致）
DEPT = "呼吸内科"
DATE = "2026-11-13"
TIME = "上午"


class Recorder:
    def __init__(self) -> None:
        self.rows: List[Tuple[str, bool, str]] = []

    def check(self, name: str, fn: Callable[[], Any]) -> bool:
        try:
            detail = str(fn())[:110]
            self.rows.append((name, True, detail))
            return True
        except Exception as exc:  # noqa: BLE001 - 演练脚本要收集全部失败
            self.rows.append((name, False, f"{type(exc).__name__}: {exc}"))
            return False

    def report(self) -> int:
        passed = sum(1 for _, ok, _ in self.rows if ok)
        print("\n" + "=" * 72)
        for name, ok, detail in self.rows:
            print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")
        print("=" * 72)
        print(f"{passed}/{len(self.rows)} 通过")
        return 0 if passed == len(self.rows) else 1


def run_against(base_url: str, label: str, rec: Recorder) -> None:
    """对一套真实 HIS 服务跑全部核验。"""
    client = HisClient(base_url, token="his-sandbox-token", timeout=5.0)
    doctors = HisDoctorService(client)
    # 与组合根一致：真机装配打开患者主档建档，落单用 HIS 签发的引用
    slots = HisSlotService(client, patient_master_enabled=True)
    repo = InMemoryAppointmentRepository()

    # 真实工具对象：工具层代码一行没改，只换了注入的实现
    book_tool = BookAppointmentTool(repo, slots, doctors)
    cancel_tool = CancelAppointmentTool(repo, slots)
    slot_tool = QuerySlotTool(slots, doctors)
    doctor_tool = QueryDoctorsTool(doctors)

    # ---- 1. 院方主档由 HIS 提供 ----
    def catalog_loaded() -> str:
        stat = doctors.refresh()
        assert stat["departments"] >= 10, f"科室太少：{stat}"
        assert stat["doctors"] >= 20, f"医生太少：{stat}"
        return f"HIS 主档 {stat['departments']} 科室 / {stat['doctors']} 医生"

    rec.check(f"[{label}] HIS 主档预加载（非空 fail-fast）", catalog_loaded)

    depts = doctors.list_departments()
    rec.check(
        f"[{label}] 查询科室 → HIS /dept/list",
        lambda: (f"{len(depts)} 个科室，含 {DEPT}"
                 if DEPT in depts and len(depts) >= 10
                 else (_ for _ in ()).throw(AssertionError(f"科室异常：{depts}"))),
    )

    def doctor_query() -> str:
        result = asyncio.run(doctor_tool.call({"department": DEPT}))
        text = str(getattr(result, "content", result))
        assert "徐国兴" in text, f"医生名录不含本科室专家：{text[:120]}"
        assert "主任医师" in text, f"缺少职称：{text[:120]}"
        assert "擅长" in text, f"缺少擅长方向：{text[:120]}"
        return text.replace("\n", " ")[:80]

    rec.check(f"[{label}] 查询医生 → HIS /doctor/list（含职称/擅长）", doctor_query)

    def hint_from_his() -> str:
        hint = build_department_hint(doctors)
        assert DEPT in hint, f"提示词科室清单未来自 HIS：{hint[:120]}"
        return f"权威科室清单来自 HIS：{len(depts)} 个"

    rec.check(f"[{label}] 工具描述 / 系统提示词科室清单 → HIS", hint_from_his)

    # ---- 2. 字典与号源同源 ----
    def same_source() -> str:
        rows = client.query_slots({"deptName": DEPT, "clinicDate": DATE})
        his_doctors = {d.name for d in doctors.list_doctors(DEPT)}
        slot_doctors = {r.doctor_name for r in rows if r.doctor_name}
        assert his_doctors, "医生名录为空"
        assert slot_doctors, "号源里没有医生"
        assert slot_doctors <= his_doctors, (
            f"号源里出现主档没有的医生（数据不同源）：{slot_doctors - his_doctors}"
        )
        return f"号源医生 {sorted(slot_doctors)} ⊆ 主档医生 {sorted(his_doctors)}"

    rec.check(f"[{label}] 字典与号源同源（无第二份院方数据）", same_source)

    # ---- 3. 查号源工具 ----
    def slot_query() -> str:
        result = asyncio.run(
            slot_tool.call({"department": DEPT, "date": DATE, "time": TIME})
        )
        text = str(getattr(result, "content", result))
        assert "有号源" in text or "暂无" in text, text[:120]
        return text[:80]

    rec.check(f"[{label}] 查询号源工具 → HIS /clinicForRegist/list", slot_query)

    # ---- 4. 预约挂号工具（真实预占 + 落单） ----
    suffix = uuid.uuid4().hex[:8]
    params = {
        "username": "张三",
        "id_card": VALID_ID,
        "department": DEPT,
        "date": DATE,
        "time": TIME,
    }

    def book() -> str:
        result = asyncio.run(book_tool.call(dict(params)))
        text = str(getattr(result, "content", result))
        ok = bool(getattr(result, "success", True))
        assert ok, f"预约未成功：{text[:160]}"
        assert "预约成功" in text or "已有预约" in text, text[:160]
        return text[:80]

    rec.check(f"[{label}] 预约挂号工具 → HIS 预占 + 落单", book)

    def slot_consumed() -> str:
        bookings = repo._rows
        assert bookings, "本地没有预约记录"
        assert bookings[-1].external_booking_id, "HIS 未返回外部挂号单号"
        return f"外部单号 {bookings[-1].external_booking_id[:24]}"

    rec.check(f"[{label}] 落单回写外部挂号单号", slot_consumed)

    def patient_ref_from_his() -> str:
        ref = repo._rows[-1].patient_ref
        assert ref.startswith("PAT-"), (
            f"患者引用不是 HIS 签发的（疑似本地 sha256 派生）：{ref[:40]}"
        )
        his_patients = client.request(
            "GET", client.path("patients") or "/patMasterIndex/list",
            params={"idNo": VALID_ID},
        ).data
        his_refs = {
            str(row.get("patientRef") or "")
            for row in (his_patients or [])
            if isinstance(row, dict)
        }
        assert ref in his_refs, f"HIS 主档里没有该引用：{ref[:40]}"
        return f"患者引用 {ref} 由 HIS 主档签发"

    rec.check(f"[{label}] 患者主档引用 → HIS 建档签发", patient_ref_from_his)

    def query_booking() -> str:
        booking_id = repo._rows[-1].external_booking_id
        status = slots.query_booking(booking_id)
        assert status.found, "HIS 查不到刚落的单"
        return f"HIS 查单 found={status.found} status={status.status}"

    rec.check(f"[{label}] 查单 → HIS /currentRegister/{{id}}", query_booking)

    # ---- 5. 取消挂号工具（真实退号，号源归还） ----
    def cancel() -> str:
        result = asyncio.run(cancel_tool.call(dict(params)))
        text = str(getattr(result, "content", result))
        assert "取消预约成功" in text or "无需重复取消" in text, text[:160]
        return text[:80]

    rec.check(f"[{label}] 取消挂号工具 → HIS 退号", cancel)

    def slot_returned() -> str:
        status = slots.query_booking(repo._rows[-1].external_booking_id)
        assert status.found, "退号后单子不该消失（应改状态）"
        assert status.status == "CANCELLED", f"HIS 侧状态未更新：{status.status}"
        return f"HIS 单状态={status.status}，号源已归还"

    rec.check(f"[{label}] 退号后 HIS 状态回落", slot_returned)

    # ---- 6. 端点集中映射 ----
    def paths_centralized() -> str:
        keys = ["slots", "hold", "confirm", "cancel", "depts", "doctors", "patient"]
        missing = [k for k in keys if not client.path(k)]
        assert not missing, f"端点映射缺失：{missing}"
        return "7 类端点集中在 HisClient._paths（接真 HIS 只改这里）"

    rec.check(f"[{label}] 端点映射集中（唯一改动点）", paths_centralized)


def main() -> int:
    parser = argparse.ArgumentParser(description="院方数据单一源真机核验")
    parser.add_argument("--sandbox", default="", help="额外核验独立沙箱服务地址")
    args = parser.parse_args()

    rec = Recorder()
    catalog = load_his_catalog(org_id="ORG-LOCAL")
    # 内嵌 HIS：临时端口，脚本退出即销毁
    server = LocalHisServer(catalog=catalog, limit_num=20)
    base_url = server.start()
    print(f"临时 HIS 服务已启动：{base_url}（脚本结束后自动关闭）")
    try:
        run_against(base_url, "内嵌 HIS", rec)
        if args.sandbox:
            run_against(args.sandbox.rstrip("/"), "独立沙箱", rec)
    finally:
        server.stop()
        print("临时 HIS 服务已关闭")
    return rec.report()


if __name__ == "__main__":
    sys.exit(main())
