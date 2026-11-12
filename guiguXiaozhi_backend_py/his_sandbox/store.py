"""HIS 沙箱的业务与存储层 —— 7 类接口的数据来源。

**这一层是"HIS 系统"本身**：所有状态都在 Docker MySQL 里，不在进程内存。
因此重启服务不丢号源、不丢挂号单，多个沙箱实例连同一个库也不会超卖。

**并发正确性**（参考项目缺的部分）

预占不是"先查再改"，而是：

.. code-block:: sql

    SELECT ... WHERE (limitNum - currentNum - currentPreNum) > 0
              ORDER BY 剩余号 DESC LIMIT 1 FOR UPDATE;   -- 行锁
    UPDATE ... SET currentPreNum = currentPreNum + 1
              WHERE id = %s AND (limitNum - currentNum - currentPreNum) > 0;

``FOR UPDATE`` + 条件更新保证 20 个并发预占只会有 N 个成功（N = 号源数）。
参考项目是"查出来再 update ById"，既没有行锁也没有版本号，会超卖。

**幂等**：``his_idempotency`` 以 ``(idem_key, action)`` 为主键，
同一业务动作重发 → 返回首次响应快照，不重复扣号、不重复落单。
粒度按动作分开（hold / confirm / release / cancel 各带前缀），
与本项目 appointment_service 的约定一致。
"""

from __future__ import annotations

import json
import logging
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import mysql.connector

from his_sandbox.config import SandboxConfig, mask_id_no

logger = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


class SandboxReject(Exception):
    """HIS 业务拒绝 —— 转成 ``R{code, msg}``（HTTP 仍是 200）。"""

    def __init__(self, code: int, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


class HisStore:
    def __init__(self, config: SandboxConfig) -> None:
        self.config = config

    # ---------- 连接 / 事务 ----------

    def _connect(self, *, with_database: bool = True):
        kwargs: Dict[str, Any] = {
            "host": self.config.mysql_host,
            "port": self.config.mysql_port,
            "user": self.config.mysql_user,
            "password": self.config.mysql_password,
            "autocommit": False,
            "connection_timeout": 5,
        }
        if with_database:
            kwargs["database"] = self.config.mysql_database
        return mysql.connector.connect(**kwargs)

    @contextmanager
    def _tx(self) -> Iterator[Any]:
        """一个业务动作 = 一个事务。异常一律回滚（不留半截状态）。"""
        conn = self._connect()
        cur = conn.cursor(dictionary=True)
        try:
            conn.start_transaction()
            yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()
            conn.close()

    def init_schema(self) -> None:
        """建库 + 执行幂等 DDL。"""
        conn = self._connect(with_database=False)
        cur = conn.cursor()
        try:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS {self.config.mysql_database} "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
            conn.commit()
        finally:
            cur.close()
            conn.close()

        conn = self._connect()
        cur = conn.cursor()
        try:
            # 注意：不能按 ``片段是否以 -- 开头`` 丢弃整个片段 ——
            # 表间注释会贴在**下一条 CREATE TABLE** 前面，那样会漏建表。
            # 正确做法是先逐行去注释，再看剩下的还有没有 SQL。
            statements: List[str] = []
            for raw in SCHEMA_PATH.read_text(encoding="utf-8").split(";"):
                lines = [
                    line
                    for line in raw.splitlines()
                    if not line.strip().startswith("--")
                ]
                sql = "\n".join(lines).strip()
                if sql:
                    statements.append(sql)
            for sql in statements:
                cur.execute(sql)
            conn.commit()
        finally:
            cur.close()
            conn.close()

    # ---------- 幂等 / 过期预占 ----------

    @staticmethod
    def _idem_get(cur, key: str, action: str) -> Optional[Dict[str, Any]]:
        if not key:
            return None
        cur.execute(
            "SELECT response_json FROM his_idempotency "
            "WHERE idem_key=%s AND action=%s FOR UPDATE",
            (key, action),
        )
        row = cur.fetchone()
        return json.loads(row["response_json"]) if row else None

    @staticmethod
    def _idem_put(
        cur, key: str, action: str, payload: Dict[str, Any]
    ) -> None:
        if not key:
            return
        cur.execute(
            "INSERT INTO his_idempotency (idem_key, action, response_json) "
            "VALUES (%s,%s,%s) ON DUPLICATE KEY UPDATE response_json=response_json",
            (
                key,
                action,
                json.dumps(payload, ensure_ascii=False, default=str),
            ),
        )

    @staticmethod
    def _expire_holds(cur) -> int:
        """惰性回收过期预占，把号还回号池。

        每次写/查动作前调用一次 —— 不依赖后台定时任务，重启也不会卡住号源。
        """
        cur.execute(
            "SELECT hold_id, clinic_for_regist_id FROM his_hold "
            "WHERE status='HELD' AND expires_at < NOW() FOR UPDATE"
        )
        rows = cur.fetchall()
        for row in rows:
            cur.execute(
                "UPDATE clinic_for_regist "
                "SET current_pre_num=GREATEST(0, current_pre_num-1) "
                "WHERE clinic_for_regist_id=%s",
                (row["clinic_for_regist_id"],),
            )
            cur.execute(
                "UPDATE his_hold SET status='EXPIRED', resolved_at=NOW() "
                "WHERE hold_id=%s",
                (row["hold_id"],),
            )
        return len(rows)

    # ---------- 1. 号源查询 ----------

    @staticmethod
    def _slot_row(row: Dict[str, Any]) -> Dict[str, Any]:
        """库行 → HIS 号表实例 Vo（驼峰，对齐 ClinicForRegistVo + HisSlot）。"""
        limit_num = int(row["limit_num"])
        current_num = int(row["current_num"])
        current_pre_num = int(row["current_pre_num"])
        clinic_date = row["clinic_date"]
        return {
            "clinicForRegistId": row["clinic_for_regist_id"],
            "clinicIndexId": row["clinic_index_id"],
            "clinicScheduleId": row["clinic_schedule_id"],
            "clinicDate": (
                clinic_date.isoformat()
                if hasattr(clinic_date, "isoformat")
                else str(clinic_date)
            ),
            "timeDesc": row["time_desc"],
            "limitNum": limit_num,
            "limitPreNum": int(row["limit_pre_num"]),
            "currentNum": current_num,
            "currentRegisNum": int(row["current_regis_num"]),
            "currentPreNum": current_pre_num,
            "orgId": row["org_id"],
            "doctorName": row["doctor_name"],
            "deptName": row["dept_name"],
            "remaining": max(0, limit_num - current_num - current_pre_num),
        }

    def query_slots(self, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        conditions = ["del_flag='0'"]
        args: List[Any] = []
        mapping = (
            ("clinicDate", "clinic_date"),
            ("timeDesc", "time_desc"),
            ("deptName", "dept_name"),
            ("doctorName", "doctor_name"),
            ("orgId", "org_id"),
        )
        for key, column in mapping:
            value = str(params.get(key) or "").strip()
            if value:
                conditions.append(f"{column}=%s")
                args.append(value)
        with self._tx() as cur:
            self._expire_holds(cur)
            cur.execute(
                "SELECT * FROM clinic_for_regist WHERE "
                + " AND ".join(conditions)
                + " ORDER BY clinic_date, time_desc, doctor_name",
                tuple(args),
            )
            return [self._slot_row(row) for row in cur.fetchall()]

    def query_slot_by_id(self, slot_id: str) -> Optional[Dict[str, Any]]:
        with self._tx() as cur:
            cur.execute(
                "SELECT * FROM clinic_for_regist "
                "WHERE clinic_for_regist_id=%s AND del_flag='0'",
                (slot_id,),
            )
            row = cur.fetchone()
            return self._slot_row(row) if row else None

    # ---------- 2. 预占 ----------

    def hold_slot(
        self, payload: Dict[str, Any], idempotency_key: str = ""
    ) -> Dict[str, Any]:
        with self._tx() as cur:
            cached = self._idem_get(cur, idempotency_key, "hold")
            if cached is not None:
                return cached
            self._expire_holds(cur)

            conditions = [
                "del_flag='0'",
                "(limit_num - current_num - current_pre_num) > 0",
            ]
            args: List[Any] = []
            for key, column in (
                ("clinicDate", "clinic_date"),
                ("timeDesc", "time_desc"),
                ("deptName", "dept_name"),
                ("doctorName", "doctor_name"),
                ("orgId", "org_id"),
            ):
                value = str(payload.get(key) or "").strip()
                if value:
                    conditions.append(f"{column}=%s")
                    args.append(value)
            cur.execute(
                "SELECT * FROM clinic_for_regist WHERE "
                + " AND ".join(conditions)
                + " ORDER BY (limit_num - current_num - current_pre_num) DESC, "
                "clinic_for_regist_id LIMIT 1 FOR UPDATE",
                tuple(args),
            )
            slot = cur.fetchone()
            if slot is None:
                # 区分"没排班"与"号满了"，便于上层给用户准确提示
                cur.execute(
                    "SELECT COUNT(*) AS n FROM clinic_for_regist WHERE del_flag='0' "
                    "AND dept_name=%s AND clinic_date=%s AND time_desc=%s",
                    (
                        str(payload.get("deptName") or ""),
                        str(payload.get("clinicDate") or ""),
                        str(payload.get("timeDesc") or ""),
                    ),
                )
                exists = int(cur.fetchone()["n"])
                raise SandboxReject(
                    500,
                    "该时段号源已全部约满，建议换个日期或时段。"
                    if exists
                    else "该科室该时段暂无出诊安排。",
                )

            cur.execute(
                "UPDATE clinic_for_regist SET current_pre_num=current_pre_num+1 "
                "WHERE clinic_for_regist_id=%s "
                "AND (limit_num - current_num - current_pre_num) > 0",
                (slot["clinic_for_regist_id"],),
            )
            if cur.rowcount != 1:
                raise SandboxReject(500, "号源已被占用，请重试。")

            hold_id = _new_id("hold")
            master_id = _new_id("reg")
            expires_at = datetime.now() + timedelta(
                seconds=self.config.hold_ttl_seconds
            )
            try:
                cur.execute(
                    "INSERT INTO his_hold (hold_id, clinic_for_regist_id, "
                    "clinic_master_id, idempotency_key, status, expires_at) "
                    "VALUES (%s,%s,%s,%s,'HELD',%s)",
                    (
                        hold_id,
                        slot["clinic_for_regist_id"],
                        master_id,
                        idempotency_key,
                        expires_at,
                    ),
                )
            except mysql.connector.errors.IntegrityError as exc:
                # 幂等键撞了：说明并发的同一业务已经占过 → 回滚本次扣号，
                # 让调用方用同一个键重发即可拿到首次结果（不重复占号）
                raise SandboxReject(500, "重复的预占请求，请确认后重试。") from exc

            cur.execute(
                "SELECT * FROM clinic_for_regist WHERE clinic_for_regist_id=%s",
                (slot["clinic_for_regist_id"],),
            )
            result = self._slot_row(cur.fetchone())
            result.update(
                {
                    "holdId": hold_id,
                    "clinicMasterId": master_id,
                    "expiresAt": expires_at.isoformat(),
                }
            )
            self._idem_put(cur, idempotency_key, "hold", result)
            return result

    # ---------- 3. 落单（确认预占） ----------

    def confirm_hold(
        self, payload: Dict[str, Any], idempotency_key: str = ""
    ) -> Dict[str, Any]:
        hold_id = str(payload.get("holdId") or "").strip()
        patient_ref = str(payload.get("patientRef") or "").strip()
        if not hold_id:
            raise SandboxReject(500, "缺少 holdId，无法落单。")
        with self._tx() as cur:
            cached = self._idem_get(cur, idempotency_key, "confirm")
            if cached is not None:
                return cached
            self._expire_holds(cur)
            cur.execute(
                "SELECT * FROM his_hold WHERE hold_id=%s FOR UPDATE", (hold_id,)
            )
            hold = cur.fetchone()
            if hold is None:
                raise SandboxReject(500, f"预占不存在：{hold_id}")
            if hold["status"] != "HELD":
                raise SandboxReject(
                    500, f"预占已结束（{hold['status']}），不能重复落单。"
                )

            # 真实 HIS 的挂号单必须挂到患者主档上；没有档案就拒绝
            patient = None
            if patient_ref:
                cur.execute(
                    "SELECT * FROM pat_master_index WHERE patient_ref=%s "
                    "AND del_flag='0'",
                    (patient_ref,),
                )
                patient = cur.fetchone()
                if patient is None:
                    raise SandboxReject(
                        500, f"患者主档不存在：{patient_ref}，请先建档。"
                    )

            cur.execute(
                "SELECT * FROM clinic_for_regist WHERE clinic_for_regist_id=%s "
                "FOR UPDATE",
                (hold["clinic_for_regist_id"],),
            )
            slot_row = cur.fetchone()
            if slot_row is None:
                raise SandboxReject(500, "号表实例不存在，无法落单。")
            cur.execute(
                "UPDATE clinic_for_regist SET "
                "current_pre_num=GREATEST(0, current_pre_num-1), "
                "current_num=current_num+1, "
                "current_regis_num=current_regis_num+1 "
                "WHERE clinic_for_regist_id=%s",
                (hold["clinic_for_regist_id"],),
            )

            master_id = hold["clinic_master_id"]
            clinic_no = f"{date.today().strftime('%Y%m%d')}{uuid.uuid4().hex[:8].upper()}"
            cur.execute(
                "INSERT INTO clinic_master (clinic_master_id, clinic_no, "
                "patient_ref, patient_id, dept_name, doctor_name, clinic_date, "
                "time_desc, clinic_for_regist_id, hold_id, idempotency_key, "
                "clinic_type, status) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'1','REGISTERED')",
                (
                    master_id,
                    clinic_no,
                    patient_ref,
                    patient["patient_id"] if patient else "",
                    slot_row["dept_name"],
                    slot_row["doctor_name"],
                    slot_row["clinic_date"],
                    slot_row["time_desc"],
                    slot_row["clinic_for_regist_id"],
                    hold_id,
                    idempotency_key,
                ),
            )
            cur.execute(
                "UPDATE his_hold SET status='CONFIRMED', resolved_at=NOW() "
                "WHERE hold_id=%s",
                (hold_id,),
            )
            result = {
                "clinicMasterId": master_id,
                "bookingId": master_id,
                "clinicNo": clinic_no,
                "status": "REGISTERED",
                "patientRef": patient_ref,
                "clinicForRegistId": hold["clinic_for_regist_id"],
                "clinicDate": (
                    slot_row["clinic_date"].isoformat()
                    if hasattr(slot_row["clinic_date"], "isoformat")
                    else str(slot_row["clinic_date"])
                ),
                "timeDesc": slot_row["time_desc"],
                "deptName": slot_row["dept_name"],
                "doctorName": slot_row["doctor_name"],
            }
            self._idem_put(cur, idempotency_key, "confirm", result)
            return result

    # ---------- 4. 释放预占 ----------

    def release_hold(
        self, hold_id: str, idempotency_key: str = ""
    ) -> Dict[str, Any]:
        if not hold_id:
            raise SandboxReject(500, "缺少 holdId。")
        with self._tx() as cur:
            cached = self._idem_get(cur, idempotency_key, "release")
            if cached is not None:
                return cached
            cur.execute(
                "SELECT * FROM his_hold WHERE hold_id=%s FOR UPDATE", (hold_id,)
            )
            hold = cur.fetchone()
            if hold is None:
                # 幂等语义：已经释放过 / 从未存在 → 视为成功，不阻断本地回滚
                result: Dict[str, Any] = {
                    "released": True,
                    "holdId": hold_id,
                    "alreadyResolved": True,
                }
                self._idem_put(cur, idempotency_key, "release", result)
                return result
            if hold["status"] == "HELD":
                cur.execute(
                    "UPDATE clinic_for_regist "
                    "SET current_pre_num=GREATEST(0, current_pre_num-1) "
                    "WHERE clinic_for_regist_id=%s",
                    (hold["clinic_for_regist_id"],),
                )
                cur.execute(
                    "UPDATE his_hold SET status='RELEASED', resolved_at=NOW() "
                    "WHERE hold_id=%s",
                    (hold_id,),
                )
            result = {
                "released": True,
                "holdId": hold_id,
                "previousStatus": hold["status"],
            }
            self._idem_put(cur, idempotency_key, "release", result)
            return result

    # ---------- 5. 退号（已挂号 → 回补号源） ----------

    def cancel_booking(
        self, clinic_master_id: str, idempotency_key: str = ""
    ) -> Dict[str, Any]:
        if not clinic_master_id:
            raise SandboxReject(500, "缺少 clinicMasterId。")
        with self._tx() as cur:
            cached = self._idem_get(cur, idempotency_key, "cancel")
            if cached is not None:
                return cached
            cur.execute(
                "SELECT * FROM clinic_master WHERE clinic_master_id=%s FOR UPDATE",
                (clinic_master_id,),
            )
            booking = cur.fetchone()
            if booking is None:
                raise SandboxReject(404, f"挂号单不存在：{clinic_master_id}")
            if booking["status"] == "CANCELLED":
                result: Dict[str, Any] = {
                    "cancelled": True,
                    "clinicMasterId": clinic_master_id,
                    "alreadyCancelled": True,
                    "status": "CANCELLED",
                }
                self._idem_put(cur, idempotency_key, "cancel", result)
                return result

            cur.execute(
                "UPDATE clinic_for_regist "
                "SET current_num=GREATEST(0, current_num-1), "
                "current_regis_num=GREATEST(0, current_regis_num-1) "
                "WHERE clinic_for_regist_id=%s",
                (booking["clinic_for_regist_id"],),
            )
            cur.execute(
                "UPDATE clinic_master SET status='CANCELLED', clinic_type='9', "
                "cancelled_at=NOW() WHERE clinic_master_id=%s",
                (clinic_master_id,),
            )
            result = {
                "cancelled": True,
                "clinicMasterId": clinic_master_id,
                "status": "CANCELLED",
            }
            self._idem_put(cur, idempotency_key, "cancel", result)
            return result

    # ---------- 6. 查单（对账依据） ----------

    def query_booking(self, clinic_master_id: str) -> Dict[str, Any]:
        with self._tx() as cur:
            cur.execute(
                "SELECT * FROM clinic_master WHERE clinic_master_id=%s",
                (clinic_master_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise SandboxReject(404, f"挂号单不存在：{clinic_master_id}")
            clinic_date = row["clinic_date"]
            return {
                "clinicMasterId": row["clinic_master_id"],
                "bookingId": row["clinic_master_id"],
                "clinicNo": row["clinic_no"],
                "status": row["status"],
                "registStatus": row["status"],
                "clinicType": row["clinic_type"],
                "patientRef": row["patient_ref"],
                "deptName": row["dept_name"],
                "doctorName": row["doctor_name"],
                "clinicDate": (
                    clinic_date.isoformat()
                    if hasattr(clinic_date, "isoformat")
                    else str(clinic_date)
                ),
                "timeDesc": row["time_desc"],
            }

    def query_booking_by_idempotency_key(self, key: str) -> Dict[str, Any]:
        """按**业务动作的幂等键**查挂号单 —— 对账的最后一条线索。

        场景：落单请求超时，客户端既没拿到响应也不知道单号，本地只能落
        UNKNOWN 且 ``external_booking_id`` 为空。原先对账 Job 的纪律 1
        「无据可查 → 跳过」会让这条记录永远停在 UNKNOWN。HIS 侧其实留着
        幂等键与预占记录的关联，凭键就能问出"这个动作到底落单了没有"：

        * 查到挂号单 → 收敛为 CONFIRMED / CANCELLED；
        * 明确查不到 → 收敛为 FAILED（确实没挂上，号源由 TTL 回收）；
        * 查单本身超时 → 保持 UNKNOWN，不猜。
        """
        if not key:
            raise SandboxReject(404, "缺少幂等键")
        master_id = ""
        with self._tx() as cur:
            cur.execute(
                "SELECT clinic_master_id FROM his_hold "
                "WHERE idempotency_key=%s LIMIT 1",
                (key,),
            )
            row = cur.fetchone()
            if row is not None:
                master_id = str(row["clinic_master_id"] or "")
            if not master_id:
                # 也支持直接拿 confirm 动作的幂等键来查
                cur.execute(
                    "SELECT response_json FROM his_idempotency "
                    "WHERE idem_key=%s AND action='confirm' LIMIT 1",
                    (key,),
                )
                cached = cur.fetchone()
                if cached:
                    payload = json.loads(cached["response_json"])
                    master_id = str(payload.get("clinicMasterId") or "")
        if not master_id:
            raise SandboxReject(404, "该业务动作未产生挂号单")
        # 只有 hold 没有 confirm 时查不到单据 → 404，对账据此收敛为 FAILED
        return self.query_booking(master_id)

    def list_bookings(self, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        conditions = ["1=1"]
        args: List[Any] = []
        for key, column in (
            ("patientRef", "patient_ref"),
            ("status", "status"),
            ("deptName", "dept_name"),
        ):
            value = str(params.get(key) or "").strip()
            if value:
                conditions.append(f"{column}=%s")
                args.append(value)
        with self._tx() as cur:
            cur.execute(
                "SELECT clinic_master_id, clinic_no, status, patient_ref, "
                "dept_name, doctor_name, clinic_date, time_desc FROM clinic_master "
                "WHERE " + " AND ".join(conditions) + " ORDER BY created_at DESC",
                tuple(args),
            )
            rows = []
            for row in cur.fetchall():
                item = dict(row)
                if hasattr(item.get("clinic_date"), "isoformat"):
                    item["clinic_date"] = item["clinic_date"].isoformat()
                rows.append(item)
            return rows

    # ---------- 7. 患者主档 ----------

    def query_patients(self, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        conditions = ["del_flag='0'"]
        args: List[Any] = []
        id_no = str(params.get("idNo") or "").strip()
        if id_no:
            conditions.append("id_no_hash=%s")
            args.append(self.config.id_no_hash(id_no))
        for key, column in (
            ("name", "name"),
            ("patientRef", "patient_ref"),
        ):
            value = str(params.get(key) or "").strip()
            if value:
                conditions.append(f"{column}=%s")
                args.append(value)
        with self._tx() as cur:
            cur.execute(
                "SELECT patient_id, patient_ref, id_no_masked, name, "
                "phone_masked, org_id, created_at FROM pat_master_index "
                "WHERE " + " AND ".join(conditions),
                tuple(args),
            )
            rows = []
            for row in cur.fetchall():
                # 对外统一 camelCase —— 与 /patMasterIndex/add、/dept/list、
                # /doctor/list 保持一致。之前这里直接吐数据库列名
                # (patient_ref / id_no_masked)，建档返回却是 patientRef，
                # 同一个主档两个接口字段名对不上，真机核验才发现。
                created = row["created_at"]
                rows.append(
                    {
                        "patientRef": row["patient_ref"],
                        "patientId": row["patient_id"],
                        "idNoMasked": row["id_no_masked"],
                        "name": row["name"],
                        "phoneMasked": row["phone_masked"],
                        "orgId": row["org_id"],
                        "createdAt": (
                            created.isoformat()
                            if isinstance(created, (date, datetime))
                            else str(created or "")
                        ),
                    }
                )
            return rows

    def create_patient(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """建档（幂等：同一身份证号重复建档返回同一 patient_ref）。"""
        id_no = str(payload.get("idNo") or "").strip()
        name = str(payload.get("name") or "").strip()
        if not id_no or not name:
            raise SandboxReject(500, "建档需要 idNo 与 name。")
        fingerprint = self.config.id_no_hash(id_no)
        # 患者引用原则上**由 HIS 主档签发**（院方编码体系）。保留显式指定
        # 只是给运维灌数据/迁移用；本项目预约链路一律用建档返回值，
        # 不再拿本地 sha256 派生值落单（真机已复现「患者主档不存在」）。
        requested_ref = str(payload.get("patientRef") or "").strip()
        patient_ref = requested_ref or f"PAT-{fingerprint[:20]}"
        patient_id = _new_id("pat")
        with self._tx() as cur:
            cur.execute(
                "SELECT * FROM pat_master_index WHERE id_no_hash=%s FOR UPDATE",
                (fingerprint,),
            )
            existing = cur.fetchone()
            if existing:
                return {
                    "patientRef": existing["patient_ref"],
                    "patientId": existing["patient_id"],
                    "idNoMasked": existing["id_no_masked"],
                    "name": existing["name"],
                    "alreadyExists": True,
                }
            cur.execute(
                "INSERT INTO pat_master_index (patient_id, patient_ref, "
                "id_no_hash, id_no_masked, name, phone_masked, org_id) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (
                    patient_id,
                    patient_ref,
                    fingerprint,
                    mask_id_no(id_no),
                    name,
                    mask_id_no(str(payload.get("phone") or "")),
                    self.config.org_id,
                ),
            )
            return {
                "patientRef": patient_ref,
                "patientId": patient_id,
                "idNoMasked": mask_id_no(id_no),
                "name": name,
                "alreadyExists": False,
            }

    # ---------- 8. 科室字典 ----------

    def query_depts(self, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        conditions = ["status='0'"]
        args: List[Any] = []
        for key, column in (
            ("deptCode", "dept_code"),
            ("deptName", "dept_name"),
            ("orgId", "org_id"),
        ):
            value = str(params.get(key) or "").strip()
            if value:
                conditions.append(f"{column}=%s")
                args.append(value)
        with self._tx() as cur:
            cur.execute(
                "SELECT dept_id, dept_code, dept_name, org_id, status "
                "FROM sys_dept WHERE " + " AND ".join(conditions) +
                " ORDER BY dept_code",
                tuple(args),
            )
            return [
                {
                    "deptId": row["dept_id"],
                    "deptCode": row["dept_code"],
                    "deptName": row["dept_name"],
                    "orgId": row["org_id"],
                    "status": row["status"],
                }
                for row in cur.fetchall()
            ]

    def query_doctors(self, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        """医生名录 —— 应用侧「查询科室专家」工具的唯一数据源。

        ``keyword`` 同时匹配姓名 / 职称 / 擅长 / 科室，与工具层的
        ``QueryDoctorsTool`` 过滤语义一致；停诊医生（status='1'）不下发。
        """
        conditions = ["status='0'"]
        args: List[Any] = []
        for key, column in (
            ("deptCode", "dept_code"),
            ("deptName", "dept_name"),
            ("doctorName", "doctor_name"),
        ):
            value = str(params.get(key) or "").strip()
            if value:
                conditions.append(f"{column}=%s")
                args.append(value)
        keyword = str(params.get("keyword") or "").strip()
        if keyword:
            conditions.append(
                "(doctor_name LIKE %s OR title LIKE %s OR expertise LIKE %s "
                "OR dept_name LIKE %s)"
            )
            args.extend([f"%{keyword}%"] * 4)
        with self._tx() as cur:
            cur.execute(
                "SELECT doctor_id, dept_code, dept_name, doctor_name, gender, "
                "title, expertise, status FROM sys_doctor WHERE " +
                " AND ".join(conditions) + " ORDER BY dept_code, doctor_id",
                tuple(args),
            )
            return [
                {
                    "doctorId": row["doctor_id"],
                    "deptCode": row["dept_code"],
                    "deptName": row["dept_name"],
                    "doctorName": row["doctor_name"],
                    "gender": row["gender"],
                    "title": row["title"],
                    "expertise": row["expertise"],
                    "status": row["status"],
                }
                for row in cur.fetchall()
            ]

    # ---------- 运维：状态 / 重置 ----------

    def stats(self) -> Dict[str, Any]:
        with self._tx() as cur:
            self._expire_holds(cur)
            result: Dict[str, Any] = {}
            for table in (
                "sys_dept",
                "sys_doctor",
                "pat_master_index",
                "clinic_index",
                "clinic_schedule",
                "clinic_for_regist",
                "his_hold",
                "clinic_master",
                "his_idempotency",
            ):
                cur.execute(f"SELECT COUNT(*) AS n FROM {table}")
                result[table] = int(cur.fetchone()["n"])
            cur.execute(
                "SELECT COALESCE(SUM(current_num),0) AS booked, "
                "COALESCE(SUM(current_pre_num),0) AS held, "
                "COALESCE(SUM(limit_num),0) AS total FROM clinic_for_regist"
            )
            result["counters"] = {
                k: int(v) for k, v in cur.fetchone().items()
            }
            cur.execute(
                "SELECT status, COUNT(*) AS n FROM his_hold GROUP BY status"
            )
            result["holdsByStatus"] = {
                row["status"]: int(row["n"]) for row in cur.fetchall()
            }
            cur.execute(
                "SELECT status, COUNT(*) AS n FROM clinic_master GROUP BY status"
            )
            result["bookingsByStatus"] = {
                row["status"]: int(row["n"]) for row in cur.fetchall()
            }
            return result

    def reset(self) -> None:
        """清空业务数据（保留表结构），供联调重复跑。"""
        with self._tx() as cur:
            for table in (
                "his_idempotency",
                "clinic_master",
                "his_hold",
                "clinic_for_regist",
                "clinic_schedule",
                "clinic_index",
                "pat_master_index",
                "sys_dept",
            ):
                cur.execute(f"DELETE FROM {table}")
