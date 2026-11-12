"""HIS 沙箱演示数据 —— 从院方主档快照生成，保证「查得到的医生约得到」。

参考项目 his 的仓库里**一条业务表 DDL 和初始数据都没有**（sql脚本.sql 只有
26 行 PostgreSQL 补丁），所以这里从 ``xiaozhi.service.his_catalog`` 装载
院方主档（与内嵌 HIS 替身共用同一份权威定义）生成：

.. code-block:: text

    sys_dept（科室字典，带 deptCode）
    sys_doctor（医生主档，带职称/擅长）
      → clinic_index（号别：科室 + 医生）
        → clinic_schedule（周排班）
          → clinic_for_regist（未来 N 天按日期展开的号表实例）
    pat_master_index（预置患者主档，不含明文身份证）

**主档是 HIS 自持的**：应用侧「查询科室 / 查询医生」工具通过 REST 读这些表，
不再从知识库 doctors.md 单独解析一份。这样科室字典、专家名录、号源排班
三者永远同源，不会出现「查得到但约不到」的割裂。
"""

from __future__ import annotations

import argparse
import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Tuple

from his_sandbox.config import SandboxConfig
from his_sandbox.store import HisStore
from xiaozhi.service.his_catalog import load_his_catalog

logger = logging.getLogger(__name__)

BACKEND_ROOT = Path(__file__).resolve().parents[1]
DOCTORS_MD = BACKEND_ROOT / "xiaozhi" / "knowledge" / "doctors.md"

#: 预置患者（身份证号均通过 GB 11643 校验位；沙箱只存 HMAC 指纹 + 掩码）
DEMO_PATIENTS: Tuple[Tuple[str, str, str], ...] = (
    ("张三", "110101199003074567", "13800138000"),
    ("李四", "610121199401032831", "13900139000"),
)


def parse_doctors(path: Path = DOCTORS_MD) -> List[Tuple[str, str]]:
    """[兼容] 从院方主档取 ``(科室, 医生姓名)``。

    解析逻辑已收敛到 ``xiaozhi.service.his_catalog``（唯一定义处），
    本函数只保留给既有调用方，不再自己读文件。
    """
    return load_his_catalog(path).roster()


def _seed(cur, config: SandboxConfig, *, days: int, limit_num: int) -> Dict[str, int]:
    # 院方主档（科室字典 + 医生名录 + 职称/擅长）来自 HIS 域唯一定义处
    catalog = load_his_catalog(org_id=config.org_id)
    dept_codes = {d.dept_name: d.dept_code for d in catalog.departments}
    org_id = config.org_id

    # ---- 1. 科室字典（deptCode 可查，补上参考项目缺的按编码查询） ----
    for dept in catalog.departments:
        cur.execute(
            "INSERT INTO sys_dept (dept_id, dept_code, dept_name, org_id) "
            "VALUES (%s,%s,%s,%s) ON DUPLICATE KEY UPDATE dept_name=dept_name",
            (dept.dept_id, dept.dept_code, dept.dept_name, org_id),
        )

    # ---- 1b. 医生主档（「查询科室专家」工具的数据源） ----
    for doc in catalog.doctors:
        cur.execute(
            "INSERT INTO sys_doctor "
            "(doctor_id, dept_code, dept_name, doctor_name, gender, title, "
            " expertise, status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE title=VALUES(title), "
            "expertise=VALUES(expertise), status=VALUES(status)",
            (
                doc.doctor_id, doc.dept_code, doc.dept_name, doc.name,
                doc.gender, doc.title, doc.expertise, doc.status,
            ),
        )

    # ---- 2. 患者主档 ----
    for name, id_no, phone in DEMO_PATIENTS:
        cur.execute(
            "INSERT IGNORE INTO pat_master_index "
            "(patient_id, patient_ref, id_no_hash, id_no_masked, name, "
            "phone_masked, org_id) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (
                f"pat-demo-{name}",
                f"PAT-{config.id_no_hash(id_no)[:20]}",
                config.id_no_hash(id_no),
                f"{id_no[:6]}********{id_no[-4:]}",
                name,
                f"{phone[:3]}****{phone[-4:]}",
                org_id,
            ),
        )

    # ---- 3. 号别 → 周排班 → 按日期展开的号表实例 ----
    today = date.today()
    slot_rows = 0
    for i, doc in enumerate(catalog.doctors, start=1):
        dept_name, doctor_name = doc.dept_name, doc.name
        index_id = f"idx-{i:04d}"
        cur.execute(
            "INSERT IGNORE INTO clinic_index "
            "(clinic_index_id, dept_id, dept_name, dept_code, doctor_id, "
            "doctor_name, org_id) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (
                index_id,
                f"dept-{dept_codes[dept_name]}",
                dept_name,
                dept_codes[dept_name],
                doc.doctor_id,
                doctor_name,
                org_id,
            ),
        )
        # 每个医生每天两个时段都出诊（沙箱优先保证"任何时候都查得到号"，
        # 真实 HIS 由周排班模板决定，本项目 clinic_schedule 表结构一致）
        for time_desc in ("上午", "下午"):
            for weekday in range(7):
                schedule_id = f"sched-{i:04d}-{time_desc}-{weekday}"
                cur.execute(
                    "INSERT IGNORE INTO clinic_schedule "
                    "(clinic_schedule_id, clinic_index_id, weekday, time_desc, "
                    "limit_num) VALUES (%s,%s,%s,%s,%s)",
                    (schedule_id, index_id, weekday, time_desc, limit_num),
                )
            for offset in range(days):
                clinic_date = today + timedelta(days=offset)
                slot_id = f"{index_id}@{clinic_date.isoformat()}@{time_desc}"
                cur.execute(
                    "INSERT IGNORE INTO clinic_for_regist "
                    "(clinic_for_regist_id, clinic_index_id, clinic_schedule_id, "
                    "clinic_date, time_desc, limit_num, org_id, doctor_name, "
                    "dept_name) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        slot_id,
                        index_id,
                        f"sched-{i:04d}-{time_desc}-{clinic_date.weekday()}",
                        clinic_date.isoformat(),
                        time_desc,
                        limit_num,
                        org_id,
                        doctor_name,
                        dept_name,
                    ),
                )
                slot_rows += 1

    return {
        "depts": len(catalog.departments),
        "doctors": len(catalog.doctors),
        "patients": len(DEMO_PATIENTS),
        "slotRows": slot_rows,
        "days": days,
        "limitNum": limit_num,
    }


def seed(
    store: HisStore, config: SandboxConfig, *, days: int = 0, limit_num: int = 0,
    reset: bool = False,
) -> Dict[str, int]:
    days = days or config.seed_days
    limit_num = limit_num or config.limit_num
    store.init_schema()
    conn = store._connect()
    cur = conn.cursor()
    try:
        conn.start_transaction()
        if reset:
            for table in (
                "his_idempotency", "clinic_master", "his_hold",
                "clinic_for_regist", "clinic_schedule", "clinic_index",
                "pat_master_index", "sys_dept", "sys_doctor",
            ):
                cur.execute(f"DELETE FROM {table}")
        result = _seed(cur, config, days=days, limit_num=limit_num)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="HIS 沙箱演示数据灌库")
    parser.add_argument("--reset", action="store_true", help="先清空业务表")
    parser.add_argument("--days", type=int, default=0, help="展开未来 N 天号表")
    parser.add_argument("--limit-num", type=int, default=0, help="每个号池限号数")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    config = SandboxConfig.from_env()
    result = seed(
        HisStore(config), config,
        days=args.days, limit_num=args.limit_num, reset=args.reset,
    )
    print(f"HIS 沙箱灌库完成：{result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
