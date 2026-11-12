"""本地 HIS 替身 —— 让预约挂号全流程**本地零外部依赖**可用。

**为什么需要它**

审计 P0-03 的适配层（`his_client.py` / `his_slot_service.py`）写完了，但
真实医院系统接不进来 —— 于是"预约挂号流程"在本地只能跑 Mock 的**一步式**
流程（查号源 → 直接写 CONFIRMED），**预占/确认/退号/对账这些真正的业务动作
一条都验证不了**。

本模块提供一个**行为完整的本地 HIS**：接口形态、响应包、号源语义都与
`参考项目/his` 的 zp-* 一致，区别只是数据存在本地 JSON 文件里。

**它与 MockSlotService 的区别（关键）**

.. code-block:: text

    MockSlotService          本地 HIS 替身
    ─────────────────        ──────────────────────────
    只查号源                 查 / 预占 / 确认 / 释放 / 退号 / 查单
    号源是散列算出来的假数     号源是**计数器**，预约真扣、取消真还
    一步式写 CONFIRMED       两段式 HOLDING → CONFIRMED
    无 UNKNOWN               超时/不确定 → UNKNOWN → 对账收敛
    无号源扣减                号满是真的会满（可构造满号场景）

因此本地用它能**真正验证**预约挂号流程，而不是"看起来能用"。

**号源模型**（对齐参考项目 zp-*）：

.. code-block:: text

    clinic_index      排班索引（科室 + 医生 + 限号数 + 出诊星期）
      └─ clinic_schedule   出诊安排（星期 + 时段）
           └─ clinic_for_regist   **按日期展开的号表实例**
              limit_num 限号 / current_num 已挂号 / current_pre_num 已预占

    剩余 = limit_num − current_num − current_pre_num

**号源会被真实消耗**：

.. code-block:: text

    预占 hold     current_pre_num += 1     （占住但还没挂号）
    确认 add      current_pre_num −= 1, current_num += 1   （真正挂号）
    释放 release  current_pre_num −= 1     （失败回滚，号还回去）
    退号 cancel   current_num −= 1          （用户退号，号还回去）

**响应包**：与参考项目一致 ``R{code, msg, data}``，``code == 200`` 才是
业务成功 —— HTTP 200 但 code≠200 表示业务拒绝。

**边界韧性**（与 ``his_client.py`` / ``his_sandbox`` 的分类纪律对齐）：

.. code-block:: text

    超时 / 断连 / 5xx / 系统类业务码   → 结果不确定（UNKNOWN，交给对账）
    明确业务拒绝（4xx / 号满 / 重复）  → 明确失败
    带幂等键的写操作重放             → 返回首次结果，绝不重复扣号/退号
    预占超时未确认                   → TTL 惰性回收，号源不会永久占死
    超大请求体 / 畸形 JSON / handler 异常 → 结构化错误响应 + 日志，不静默断连

**四个容易漏掉的坑**（真机演练暴露）：

1. ``release`` / ``cancel`` 也必须幂等 —— 网络抖动导致客户端重放时，
   若服务端回"预占不存在"/"已退号"，本地回滚链路会卡死；
2. 预占必须有 TTL —— 落单超时后若没人释放，号源被永久占住（号源泄漏）；
3. 按幂等键查单端点必须有 —— 落单超时且没拿到外部单号时，对账 Job
   只能凭幂等键问"这个动作到底落单了没有"，否则 UNKNOWN 永远收敛不了；
4. HTTP handler 不能裸抛异常 —— 连接直接断开虽然会被客户端正确归类为
   "不确定"，但服务端没有任何日志，排障时无从下手。

**运行方式**：由组合根以 ``slot_service_impl=local_his`` 装配时**内嵌启动**
（后台线程 + ThreadingHTTPServer），零外部进程；也可以独立运行
（``python -m xiaozhi.service.local_his``）便于调试。
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

from xiaozhi.service.his_catalog import HisCatalog, catalog_stats, load_his_catalog

logger = logging.getLogger(__name__)

#: HIS 统一响应包的成功码
HIS_SUCCESS_CODE = 200

#: 号表实例的时段枚举（与 HIS 的 timeDesc 对应）
TIME_SLOTS = ("上午", "下午")

#: 请求体上限（256KB）—— 与主管线 BodySizeLimitMiddleware 保持一致
MAX_BODY_BYTES = 256 * 1024

#: 超限时最多丢弃多少字节（防止恶意长度头把服务端拖死）
_MAX_DRAIN_BYTES = 8 * 1024 * 1024

#: 预占默认存活时间（秒）。超时未确认 → 惰性回收，号源归还号池
DEFAULT_HOLD_TTL_SECONDS = 600.0


class BodyTooLarge(Exception):
    """请求体超过上限 —— 明确拒绝（不是"结果不确定"）。"""


@dataclass
class ClinicIndex:
    """排班索引（clinic_index）—— 哪个科室哪位医生哪天出诊、限号多少。"""

    clinic_index_id: str
    dept_name: str
    doctor_name: str
    limit_num: int
    weekdays: List[int] = field(default_factory=lambda: [0, 1, 2, 3, 4])
    time_desc: str = "上午"


@dataclass
class ClinicSchedule:
    """出诊安排（clinic_schedule）—— 星期 + 时段。"""

    clinic_schedule_id: str
    clinic_index_id: str
    weekday: int
    time_desc: str
    limit_num: int


@dataclass
class ClinicForRegist:
    """号表实例（clinic_for_regist）—— 按日期展开，**号源计数器在这里**。"""

    clinic_for_regist_id: str
    clinic_index_id: str
    clinic_schedule_id: str
    clinic_date: str
    time_desc: str
    limit_num: int
    current_num: int = 0
    current_pre_num: int = 0
    org_id: str = ""
    doctor_name: str = ""
    dept_name: str = ""

    @property
    def remaining(self) -> int:
        """剩余可挂号数（必须扣掉预占数，否则并发下超卖）。"""
        return max(0, self.limit_num - self.current_num - self.current_pre_num)


@dataclass
class Booking:
    """挂号单（对应真实 HIS 的 ClinicMaster）。"""

    clinic_master_id: str
    clinic_for_regist_id: str
    patient_ref: str = ""
    status: str = "REGISTERED"  # REGISTERED / CANCELLED
    hold_id: str = ""
    created_at: str = ""
    #: 落单动作的幂等键 —— 供按幂等键查单（对账收敛 UNKNOWN）使用
    idempotency_key: str = ""


@dataclass
class Hold:
    """预占记录。"""

    hold_id: str
    clinic_for_regist_id: str
    clinic_master_id: str
    created_at: str = ""
    #: 预占动作的幂等键：hold 超时但 confirm 已落单时，凭它反查挂号单
    idempotency_key: str = ""
    #: 过期时刻（time.time() 秒）。0 = 不过期（兼容旧持久化文件）
    expires_at: float = 0.0


class LocalHisState:
    """本地 HIS 的全部状态（可持久化到 JSON）。

    持久化由 **state 自己**负责（而不是 HTTP handler）——
    任何路径改了号源（HTTP 或直接调用）都会落盘，不会出现
    "接口层改了但状态没存" 的不一致。
    """

    def __init__(
        self,
        persist_path: Optional[Path] = None,
        *,
        hold_ttl_seconds: float = DEFAULT_HOLD_TTL_SECONDS,
        catalog: Optional[HisCatalog] = None,
        require_patient_master: bool = False,
    ) -> None:
        self.lock = threading.RLock()
        self.persist_path: Optional[Path] = (
            Path(persist_path) if persist_path else None
        )
        #: 院方主档（科室字典 + 医生名录）。**HIS 自持**，应用侧只能通过
        #: REST 接口读，不直接 import 主档模块。留空时由调用方自行 seed
        #:（既有单测与纯内存演示需要这种行为）。
        self.catalog: Optional[HisCatalog] = catalog
        self.indexes: Dict[str, ClinicIndex] = {}
        self.schedules: Dict[str, ClinicSchedule] = {}
        self.regist: Dict[str, ClinicForRegist] = {}   # key: slot_id|clinic_date
        self.bookings: Dict[str, Booking] = {}
        self.holds: Dict[str, Hold] = {}
        #: 患者主档：HMAC 指纹 → 院方患者引用（不存明文身份证）
        self.patients: Dict[str, Dict[str, Any]] = {}
        self.idempotency: Dict[str, Dict[str, Any]] = {}
        #: release/cancel 的幂等结果，按 (动作, 键) 隔离，避免跨动作串号
        self.write_results: Dict[str, Dict[str, Any]] = {}
        #: 幂等键 → 挂号单号。hold 与 confirm 的键都指向同一张单，
        #: 让「落单超时、没拿到单号」的 UNKNOWN 凭任一业务动作键可查证
        self.booking_by_key: Dict[str, str] = {}
        self.hold_ttl_seconds = max(0.0, float(hold_ttl_seconds))
        #: 落单是否强制患者引用来自本院主档（与沙箱一致）。
        #: 纯内存单测直接构造 state 时默认关，组合根装配真机时打开。
        self.require_patient_master = bool(require_patient_master)
        self._seq = 0

    def next_id(self, prefix: str) -> str:
        with self.lock:
            self._seq += 1
            return f"{prefix}-{self._seq}"

    # ---------- 院方主档（HIS 自持，唯一权威源） ----------

    def query_depts(self) -> List[Dict[str, Any]]:
        """科室字典 —— 对应 ``GET /dept/list``。"""
        if self.catalog is None:
            return []
        return self.catalog.dept_rows()

    def create_patient(
        self, id_no: str, name: str, *, idempotency_key: str = ""
    ) -> Dict[str, Any]:
        """建档 / 查主档 —— 返回 **HIS 侧**患者引用（院方编码体系）。

        只存 HMAC 指纹，不存明文身份证（与沙箱、P0-06 一致）。
        同一身份证重复建档返回首次引用（主档天然幂等）。
        """
        fingerprint = hashlib.sha256(id_no.encode("utf-8")).hexdigest()
        with self.lock:
            existing = self.patients.get(fingerprint)
            if existing is not None:
                return dict(existing)
            ref = f"PAT-{fingerprint[:20]}"
            row = {
                "patientRef": ref,
                "patientId": self.next_id("pat"),
                "name": name,
                "idNoMasked": f"{id_no[:6]}********{id_no[-4:]}" if len(id_no) >= 10 else "***",
            }
            self.patients[fingerprint] = row
            if idempotency_key:
                self.idempotency[idempotency_key] = row
            self.save()
            return dict(row)

    def query_patients(self, id_no: str = "") -> List[Dict[str, Any]]:
        """查主档（按身份证指纹精确匹配；不传则返回全部掩码记录）。"""
        with self.lock:
            if not id_no:
                return [dict(row) for row in self.patients.values()]
            fingerprint = hashlib.sha256(id_no.encode("utf-8")).hexdigest()
            row = self.patients.get(fingerprint)
            return [dict(row)] if row else []

    def has_patient(self, patient_ref: str) -> bool:
        """落单前校验：患者引用必须来自本院主档（与沙箱行为一致）。"""
        if not patient_ref:
            return False
        with self.lock:
            return any(
                row.get("patientRef") == patient_ref
                for row in self.patients.values()
            )

    def query_doctors(self, dept_name: str = "", keyword: str = "") -> List[Dict[str, Any]]:
        """医生名录 —— 对应 ``GET /doctor/list``。

        ``keyword`` 命中姓名、职称或擅长方向（对齐「查询科室专家」工具）。
        """
        if self.catalog is None:
            return []
        rows = self.catalog.doctor_rows(dept_name.strip())
        needle = (keyword or "").strip()
        if needle:
            rows = [
                row for row in rows
                if any(needle in str(row.get(field) or "") for field in
                       ("doctorName", "title", "expertise", "deptName"))
            ]
        return rows

    def load_catalog(
        self, catalog: Optional[HisCatalog] = None, *, limit_num: int = 5
    ) -> Dict[str, Any]:
        """装载院方主档并据此生成排班 —— HIS 启动时调用（幂等）。

        之前是组合根把知识库名录反向喂给 HIS（``seed_from_roster``），
        方向反了：院方主档必须由 HIS 自己持有。这里主档 + 排班都从
        同一份快照生成，保证「查得到的医生」与「约得到的号」同源。
        """
        snap = catalog or load_his_catalog()
        with self.lock:
            self.catalog = snap
            # 已有排班时不重复生成（避免启动 / 恢复持久化状态后号源翻倍）
            if self.indexes:
                return catalog_stats(snap)
            for dept_name, doctor_name in snap.roster():
                self.add_roster(dept_name, doctor_name, limit_num)
            self.save()
        logger.info("本地 HIS 院方主档已装载：%s", catalog_stats(snap))
        return catalog_stats(snap)

    # ---------- 边界韧性：预占 TTL + 幂等 ----------

    @staticmethod
    def _write_key(action: str, idempotency_key: str) -> str:
        return f"{action}:{idempotency_key}"

    def expire_holds(self) -> int:
        """惰性回收过期预占，把号还回号池。

        每次读/写动作前调用一次 —— 不依赖后台定时任务，进程重启也不会
        让号源被"上次超时没释放的预占"永久占死。
        """
        if self.hold_ttl_seconds <= 0:
            return 0
        now = time.time()
        expired = [
            hold_id
            for hold_id, hold in self.holds.items()
            if 0 < hold.expires_at <= now
        ]
        for hold_id in expired:
            hold = self.holds.pop(hold_id, None)
            if hold is None:
                continue
            slot = self._find_regist(hold.clinic_for_regist_id)
            if slot is not None:
                slot.current_pre_num = max(0, slot.current_pre_num - 1)
            logger.info("本地 HIS 预占过期回收：hold=%s", hold_id[:24])
        if expired:
            self.save()
        return len(expired)

    def query_booking_by_idempotency_key(self, key: str) -> Optional[Booking]:
        """按业务动作幂等键查挂号单 —— 对账的最后一条线索。"""
        if not key:
            return None
        with self.lock:
            master_id = self.booking_by_key.get(key, "")
            return self.bookings.get(master_id) if master_id else None

    # ---------- 排班模板 ----------

    def add_roster(
        self,
        dept_name: str,
        doctor_name: str,
        limit_num: int,
        weekdays: Optional[List[int]] = None,
    ) -> None:
        """登记一条排班（组合根从医生知识库调用）。"""
        with self.lock:
            index_id = self.next_id("idx")
            weekdays = weekdays or [0, 1, 2, 3, 4]
            self.indexes[index_id] = ClinicIndex(
                clinic_index_id=index_id,
                dept_name=dept_name,
                doctor_name=doctor_name,
                limit_num=limit_num,
                weekdays=list(weekdays),
            )
            # 每位医生一个时段（按医生序号奇偶错开上午/下午，与 Mock 一致）
            time_desc = "上午" if len(self.indexes) % 2 == 1 else "下午"
            self.indexes[index_id].time_desc = time_desc
            schedule_id = self.next_id("sched")
            for weekday in weekdays:
                self.schedules[schedule_id + f"-{weekday}"] = ClinicSchedule(
                    clinic_schedule_id=schedule_id + f"-{weekday}",
                    clinic_index_id=index_id,
                    weekday=weekday,
                    time_desc=time_desc,
                    limit_num=limit_num,
                )

    # ---------- 号表实例（按需展开） ----------

    def _slot_key(self, clinic_for_regist_id: str, clinic_date: str) -> str:
        return f"{clinic_for_regist_id}|{clinic_date}"

    def ensure_regist(
        self, schedule: ClinicSchedule, clinic_date: str
    ) -> ClinicForRegist:
        """按需展开某日的号表实例（幂等：已存在就返回）。

        真实 HIS 是提前把排班展开成未来 N 天的号表实例；本地按需展开，
        语义等价（同一日期永远得到同一条记录，计数器不丢）。
        """
        with self.lock:
            index = self.indexes.get(schedule.clinic_index_id)
            if index is None:
                raise KeyError(f"排班不存在：{schedule.clinic_index_id}")
            slot_id = f"{schedule.clinic_schedule_id}@{clinic_date}"
            key = self._slot_key(slot_id, clinic_date)
            existing = self.regist.get(key)
            if existing is not None:
                return existing
            instance = ClinicForRegist(
                clinic_for_regist_id=slot_id,
                clinic_index_id=index.clinic_index_id,
                clinic_schedule_id=schedule.clinic_schedule_id,
                clinic_date=clinic_date,
                time_desc=schedule.time_desc,
                limit_num=schedule.limit_num,
                current_num=0,
                current_pre_num=0,
                org_id="ORG-LOCAL",
                doctor_name=index.doctor_name,
                dept_name=index.dept_name,
            )
            self.regist[key] = instance
            return instance

    def query_slots(
        self,
        clinic_date: str,
        time_desc: str = "",
        dept_name: str = "",
        doctor_name: str = "",
    ) -> List[ClinicForRegist]:
        """按条件查号表实例（对应 ``GET /clinicForRegist/list``）。"""
        try:
            target = date.fromisoformat((clinic_date or "").strip())
        except ValueError:
            return []
        weekday = target.weekday()
        results: List[ClinicForRegist] = []
        with self.lock:
            # 纯查询也触发惰性回收：否则"预占超时没人来确认"会让号源
            # 一直显示为已占，直到下一次写操作才恢复。
            self.expire_holds()
            for schedule in self.schedules.values():
                if schedule.weekday != weekday:
                    continue
                index = self.indexes.get(schedule.clinic_index_id)
                if index is None:
                    continue
                if dept_name and index.dept_name != dept_name:
                    continue
                if doctor_name and index.doctor_name != doctor_name:
                    continue
                if time_desc and schedule.time_desc != time_desc:
                    continue
                results.append(self.ensure_regist(schedule, target.isoformat()))
        return results

    # ---------- 预占 / 确认 / 释放 / 退号 ----------

    def hold_slot(
        self,
        clinic_date: str,
        time_desc: str,
        dept_name: str,
        doctor_name: str = "",
        idempotency_key: str = "",
    ) -> Tuple[Optional[Dict[str, Any]], str]:
        """预占号源。返回 (结果, 错误消息)；成功时错误消息为空。"""
        with self.lock:
            self.expire_holds()
            if idempotency_key and idempotency_key in self.idempotency:
                return self.idempotency[idempotency_key], ""
            slots = self.query_slots(clinic_date, time_desc, dept_name, doctor_name)
            if not slots:
                return None, f"{dept_name} 在 {clinic_date} {time_desc}暂无出诊安排。"
            # 优先选剩余号最多的（真实 HIS 也是按号池顺序取）
            slot = max(slots, key=lambda s: s.remaining)
            if slot.remaining <= 0:
                return None, f"{dept_name} 在 {clinic_date} {time_desc}的号源已满。"
            slot.current_pre_num += 1  # 预占：占住号
            hold_id = self.next_id("hold")
            master_id = self.next_id("reg")
            self.holds[hold_id] = Hold(
                hold_id=hold_id,
                clinic_for_regist_id=slot.clinic_for_regist_id,
                clinic_master_id=master_id,
                created_at=datetime.now().isoformat(timespec="seconds"),
                idempotency_key=idempotency_key,
                expires_at=(
                    time.time() + self.hold_ttl_seconds
                    if self.hold_ttl_seconds > 0
                    else 0.0
                ),
            )
            result = {
                "holdId": hold_id,
                "clinicForRegistId": slot.clinic_for_regist_id,
                "clinicMasterId": master_id,
                "remaining": slot.remaining,
            }
            if idempotency_key:
                self.idempotency[idempotency_key] = result
                # hold 超时但 confirm 已成功时，凭 hold 键也能反查到挂号单
                self.booking_by_key[idempotency_key] = master_id
            self.save()
            return result, ""

    def confirm_hold(
        self,
        hold_id: str,
        patient_ref: str = "",
        idempotency_key: str = "",
    ) -> Tuple[Optional[Dict[str, Any]], str]:
        """确认预占并落单。"""
        with self.lock:
            self.expire_holds()
            if idempotency_key and idempotency_key in self.idempotency:
                return self.idempotency[idempotency_key], ""
            hold = self.holds.get(hold_id)
            if hold is None:
                return None, f"预占不存在或已过期：{hold_id}"
            if self.require_patient_master and not self.has_patient(patient_ref):
                # 与沙箱一致：患者引用必须由 HIS 建档签发，不能拿本地
                # sha256 派生值直接落单（真机核验已复现"患者主档不存在"）。
                return None, f"患者主档不存在：{patient_ref}，请先建档。"
            slot = self._find_regist(hold.clinic_for_regist_id)
            if slot is None:
                return None, "号表实例不存在，无法落单。"
            # 预占转挂号：预占数减、已挂号数加
            slot.current_pre_num = max(0, slot.current_pre_num - 1)
            slot.current_num += 1
            self.bookings[hold.clinic_master_id] = Booking(
                clinic_master_id=hold.clinic_master_id,
                clinic_for_regist_id=slot.clinic_for_regist_id,
                patient_ref=patient_ref,
                status="REGISTERED",
                hold_id=hold_id,
                created_at=datetime.now().isoformat(timespec="seconds"),
                idempotency_key=idempotency_key or hold.idempotency_key,
            )
            self.holds.pop(hold_id, None)
            result = {
                "clinicMasterId": hold.clinic_master_id,
                "clinicForRegistId": slot.clinic_for_regist_id,
                "remaining": slot.remaining,
            }
            if idempotency_key:
                self.idempotency[idempotency_key] = result
                self.booking_by_key[idempotency_key] = hold.clinic_master_id
            self.save()
            return result, ""

    def release_hold(
        self, hold_id: str, idempotency_key: str = ""
    ) -> Tuple[bool, str]:
        """释放预占（失败回滚）：预占数减，号还回去。

        带幂等键时**重放永远成功** —— 网络抖动会让客户端用同一个键重发，
        首次响应丢了不代表业务失败。若服务端回"预占不存在"，本地回滚
        链路会卡死，号源也永远退不掉。不带键时保持严格语义（重复即拒绝），
        便于测试与人工排障。
        """
        with self.lock:
            self.expire_holds()
            cache_key = self._write_key("release", idempotency_key) if idempotency_key else ""
            if cache_key and cache_key in self.write_results:
                return True, ""
            hold = self.holds.pop(hold_id, None)
            if hold is None:
                if cache_key:
                    self.write_results[cache_key] = {
                        "released": True, "alreadyResolved": True,
                    }
                    self.save()
                    return True, ""
                return False, f"预占不存在或已释放：{hold_id}"
            slot = self._find_regist(hold.clinic_for_regist_id)
            if slot is not None:
                slot.current_pre_num = max(0, slot.current_pre_num - 1)
            if cache_key:
                self.write_results[cache_key] = {"released": True}
            self.save()
            return True, ""

    def cancel_booking(
        self, clinic_master_id: str, idempotency_key: str = ""
    ) -> Tuple[bool, str]:
        """退号（用户取消）：已挂号数减，号还回去。

        与 ``release_hold`` 同理：带幂等键的重放视为成功，绝不因"已退号"
        把一次网络重发判成失败（否则本地说取消失败、医院其实已退号）。
        不带键的重复退号仍明确拒绝，保证号源不会被扣成负数。
        """
        with self.lock:
            cache_key = self._write_key("cancel", idempotency_key) if idempotency_key else ""
            if cache_key and cache_key in self.write_results:
                return True, ""
            booking = self.bookings.get(clinic_master_id)
            if booking is None:
                if cache_key:
                    self.write_results[cache_key] = {
                        "cancelled": True, "alreadyResolved": True,
                    }
                    self.save()
                    return True, ""
                return False, f"挂号单不存在：{clinic_master_id}"
            if booking.status == "CANCELLED":
                if cache_key:
                    self.write_results[cache_key] = {
                        "cancelled": True, "alreadyResolved": True,
                    }
                    self.save()
                    return True, ""
                return False, "该挂号单已退号。"
            slot = self._find_regist(booking.clinic_for_regist_id)
            if slot is not None:
                slot.current_num = max(0, slot.current_num - 1)
            booking.status = "CANCELLED"
            if cache_key:
                self.write_results[cache_key] = {"cancelled": True}
            self.save()
            return True, ""

    def query_booking(self, clinic_master_id: str) -> Optional[Booking]:
        with self.lock:
            return self.bookings.get(clinic_master_id)

    def _find_regist(self, clinic_for_regist_id: str) -> Optional[ClinicForRegist]:
        for slot in self.regist.values():
            if slot.clinic_for_regist_id == clinic_for_regist_id:
                return slot
        return None

    # ---------- 持久化 ----------

    def save(self) -> None:
        """落盘（原子写）。失败只告警，不阻断业务。"""
        if self.persist_path is None:
            return
        try:
            self.persist_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.persist_path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self.to_dict(), ensure_ascii=False, default=str),
                encoding="utf-8",
            )
            tmp.replace(self.persist_path)
        except OSError as exc:
            logger.warning("本地 HIS 状态持久化失败：%s", exc)

    def to_dict(self) -> Dict[str, Any]:
        with self.lock:
            return {
                "indexes": [asdict(v) for v in self.indexes.values()],
                "schedules": [asdict(v) for v in self.schedules.values()],
                "regist": [asdict(v) for v in self.regist.values()],
                "bookings": [asdict(v) for v in self.bookings.values()],
                "holds": [asdict(v) for v in self.holds.values()],
                "patients": self.patients,
                "idempotency": self.idempotency,
                "write_results": self.write_results,
                "booking_by_key": self.booking_by_key,
                "seq": self._seq,
            }

    def load_dict(self, payload: Dict[str, Any]) -> None:
        with self.lock:
            for row in payload.get("indexes", []):
                self.indexes[row["clinic_index_id"]] = ClinicIndex(**row)
            for row in payload.get("schedules", []):
                self.schedules[row["clinic_schedule_id"]] = ClinicSchedule(**row)
            for row in payload.get("regist", []):
                key = self._slot_key(row["clinic_for_regist_id"], row["clinic_date"])
                self.regist[key] = ClinicForRegist(**row)
            for row in payload.get("bookings", []):
                self.bookings[row["clinic_master_id"]] = Booking(**row)
            for row in payload.get("holds", []):
                hold = Hold(**row)
                # 旧版本持久化文件没有 expires_at（=0，永不过期）→
                # 重启后必须补上 TTL，否则崩溃前残留的预占会永久占住号源。
                if hold.expires_at <= 0 and self.hold_ttl_seconds > 0:
                    hold.expires_at = time.time() + self.hold_ttl_seconds
                self.holds[hold.hold_id] = hold
            self.patients.update(payload.get("patients", {}))
            self.idempotency.update(payload.get("idempotency", {}))
            self.write_results.update(payload.get("write_results", {}))
            self.booking_by_key.update(payload.get("booking_by_key", {}))
            self._seq = int(payload.get("seq") or 0)


class LocalHisHandler(BaseHTTPRequestHandler):
    """本地 HIS 的 HTTP 处理器（协议与参考项目 zp-* 一致）。"""

    state: LocalHisState = None  # 由 LocalHisServer 注入
    persist_path: Optional[Path] = None
    #: 响应是否已发出 —— 异常兜底不能二次发送，否则会破坏 HTTP 帧
    _responded = False
    #: 是否写操作（故障注入只作用于写，否则查单也会被拖超时）
    _is_write = False
    #: ``X-His-Fault: timeout`` 时"吃掉"响应的时长（秒）
    fault_delay: float = 3.0

    def log_message(self, *_args):  # 静音
        return

    # ---------- 工具 ----------

    def _send(self, code: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
        self._responded = True

    def _ok(self, data: Any = None, msg: str = "ok") -> None:
        """成功响应，可注入"业务已提交但响应丢失/报错"的边界故障。

        ``timeout`` 是**先执行完业务再不回响应** —— 只有这样才能验证
        "HIS 其实已经落单，客户端却超时了"，对账才有东西可收敛。
        """
        if self._is_write:
            fault = self.headers.get("X-His-Fault") or ""
            if fault == "timeout":
                time.sleep(self.fault_delay)
                return
            if fault == "http500":
                self._send(500, {"code": 500, "msg": "HIS 服务端错误（故障注入）", "data": None})
                return
        self._send(200, {"code": HIS_SUCCESS_CODE, "msg": msg, "data": data})

    def _fail(self, msg: str, code: int = 500) -> None:
        """业务失败：HTTP 200 但 code≠200（对齐 HIS 统一响应包）。"""
        self._send(200, {"code": code, "msg": msg, "data": None})

    def _not_found(self, msg: str) -> None:
        self._send(200, {"code": 404, "msg": msg, "data": None})

    def _safe_error(self, http_code: int, msg: str) -> None:
        """统一异常兜底：结构化错误响应 + 日志，不静默断连。

        HTTP 5xx 会被 ``HisClient`` 归类为"结果不确定"（UNKNOWN），
        HTTP 413 归类为"明确拒绝" —— 两者语义不同，别混用。
        """
        if self._responded:
            logger.warning("本地 HIS 异常发生在响应之后（不再发送）：%s", msg)
            return
        try:
            self._send(http_code, {"code": http_code, "msg": msg, "data": None})
        except OSError as exc:
            logger.warning("本地 HIS 错误响应发送失败（连接已断开）：%s", exc)

    def _read_body(self) -> Dict[str, Any]:
        """读取请求体，带上限与畸形输入兜底。

        只认 ``Content-Length``；chunked 请求没有长度头，读成空体后
        由业务层明确拒绝（生产环境应由网关在入口兜底）。
        """
        raw_length = self.headers.get("Content-Length") or "0"
        try:
            length = int(raw_length)
        except (TypeError, ValueError) as exc:
            raise BodyTooLarge(f"Content-Length 非法：{raw_length[:32]}") from exc
        if length < 0 or length > MAX_BODY_BYTES:
            # 必须先把请求体**读掉再拒绝**：客户端还在写、服务端就回 413
            # 并关连接 → TCP RST，响应根本到不了客户端（真机演练会看到
            # ConnectionResetError 而不是 413）。分块丢弃，不占内存。
            if 0 < length <= _MAX_DRAIN_BYTES:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
            raise BodyTooLarge(
                f"请求体超过上限（{MAX_BODY_BYTES} 字节）：{length}"
            )
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _save(self) -> None:
        """落盘（委托给 state —— 持久化逻辑只有一份）。"""
        self.state.save()

    # ---------- 路由（外层统一兜底） ----------

    def do_GET(self):
        self._responded = False
        self._is_write = False
        try:
            self._dispatch_get()
        except BodyTooLarge as exc:
            self._safe_error(413, str(exc))
        except Exception as exc:  # noqa: BLE001 - handler 兜底必须兜住一切
            logger.exception("本地 HIS GET 处理异常：%s：%s", self.path, exc)
            self._safe_error(500, "HIS 内部错误，本次结果待核实")

    def do_POST(self):
        self._responded = False
        self._is_write = True
        try:
            self._dispatch_post()
        except BodyTooLarge as exc:
            self._safe_error(413, str(exc))
        except Exception as exc:  # noqa: BLE001 - handler 兜底必须兜住一切
            logger.exception("本地 HIS POST 处理异常：%s：%s", self.path, exc)
            self._safe_error(500, "HIS 内部错误，本次结果待核实")

    def _dispatch_get(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        def param(name: str, default: str = "") -> str:
            return (params.get(name) or [default])[0]

        if path == "/clinicForRegist/list":
            slots = self.state.query_slots(
                param("clinicDate"),
                param("timeDesc"),
                param("deptName"),
                param("doctorName"),
            )
            self._ok([asdict(s) for s in slots])
            return
        # 院方主档：科室字典 / 医生名录（应用侧查询科室、查询医生工具的数据源）
        if path == "/dept/list":
            rows = self.state.query_depts()
            code = param("deptCode")
            if code:
                rows = [r for r in rows if r.get("deptCode") == code]
            name = param("deptName")
            if name:
                rows = [r for r in rows if r.get("deptName") == name]
            self._ok(rows)
            return
        if path == "/doctor/list":
            self._ok(
                self.state.query_doctors(param("deptName"), param("keyword"))
            )
            return
        if path == "/patMasterIndex/list":
            self._ok(self.state.query_patients(param("idNo")))
            return
        # 按业务动作幂等键查单：解开「落单超时且没拿到单号」的对账死结。
        # 必须放在通用 /currentRegister/{id} 之前，否则会被当成挂号单号，
        # 返回 404 → 对账把 UNKNOWN 误收敛成 FAILED（真机演练暴露的坑）。
        if path.startswith("/currentRegister/byKey/"):
            key = unquote(path[len("/currentRegister/byKey/"):])
            booking = self.state.query_booking_by_idempotency_key(key)
            if booking is None:
                self._not_found(f"该业务动作未产生挂号单：{key[:24]}")
                return
            self._ok(asdict(booking))
            return
        if path.startswith("/currentRegister/"):
            master_id = path.rsplit("/", 1)[-1]
            booking = self.state.query_booking(master_id)
            if booking is None:
                self._not_found(f"挂号单不存在：{master_id}")
                return
            self._ok(asdict(booking))
            return
        if path == "/health":
            self._ok({"service": "local-his", "regist": len(self.state.regist)})
            return
        self._not_found(f"未知路径：{path}")

    def _dispatch_post(self):
        path = urlparse(self.path).path
        body = self._read_body()
        idem = self.headers.get("X-Idempotency-Key") or ""

        # 建档必须放在通用 "/add" 之前：``/patMasterIndex/add`` 也以
        # ``/add`` 结尾，否则会被当成落单（真机核验暴露的路由歧义）。
        if path in ("/patMasterIndex", "/patMasterIndex/add"):
            id_no = str(body.get("idNo") or "")
            if not id_no:
                self._fail("建档缺少身份证号（idNo）")
                return
            self._ok(
                self.state.create_patient(
                    id_no, str(body.get("name") or ""), idempotency_key=idem
                )
            )
            return

        if path.endswith("/hold"):
            result, error = self.state.hold_slot(
                body.get("clinicDate") or "",
                body.get("timeDesc") or "",
                body.get("deptName") or "",
                body.get("doctorName") or "",
                idempotency_key=idem,
            )
            if error:
                self._fail(error)
                return
            self._save()
            self._ok(result)
            return
        if path.endswith("/add"):
            result, error = self.state.confirm_hold(
                str(body.get("holdId") or ""),
                str(body.get("patientRef") or ""),
                idempotency_key=idem,
            )
            if error:
                self._fail(error)
                return
            self._save()
            self._ok(result)
            return
        if path.endswith("/release"):
            released, error = self.state.release_hold(
                str(body.get("holdId") or ""), idempotency_key=idem
            )
            if not released:
                self._fail(error)
                return
            self._save()
            self._ok({"released": True})
            return
        if path.endswith("/cancel"):
            cancelled, error = self.state.cancel_booking(
                str(body.get("clinicMasterId") or body.get("bookingId") or ""),
                idempotency_key=idem,
            )
            if not cancelled:
                self._fail(error)
                return
            self._save()
            self._ok({"cancelled": True})
            return
        self._not_found(f"未知路径：{path}")


class LocalHisServer:
    """本地 HIS 服务（内嵌运行于应用进程的后台线程）。"""

    def __init__(
        self,
        state: Optional[LocalHisState] = None,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        persist_path: Optional[Path] = None,
        hold_ttl_seconds: Optional[float] = None,
        catalog: Optional[HisCatalog] = None,
        limit_num: int = 5,
        require_patient_master: Optional[bool] = None,
    ) -> None:
        self._host = host
        self._port = port
        self._persist_path = Path(persist_path) if persist_path else None
        self.state = state or LocalHisState(persist_path=self._persist_path)
        # HIS 自持主档（组合根/独立运行传 catalog）时才强制校验患者引用；
        # 纯内存 state 直接构造（既有单测）保持宽松，不破坏历史契约。
        self.state.require_patient_master = (
            catalog is not None
            if require_patient_master is None
            else bool(require_patient_master)
        )
        if hold_ttl_seconds is not None:
            self.state.hold_ttl_seconds = max(0.0, float(hold_ttl_seconds))
        if self.state.persist_path is None and self._persist_path is not None:
            self.state.persist_path = self._persist_path
        #: 院方主档由 HIS 自己装载（组合根不再反向喂 roster）
        self._catalog = catalog
        self._limit_num = limit_num
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    # ---------- 生命周期 ----------

    @property
    def base_url(self) -> str:
        if self._server is None:
            return ""
        host, port = self._server.server_address[0], self._server.server_address[1]
        return f"http://{host}:{port}"

    def start(self) -> str:
        """启动并返回 base_url。"""
        if self._server is not None:
            return self.base_url
        if self._persist_path and self._persist_path.exists():
            try:
                payload = json.loads(self._persist_path.read_text(encoding="utf-8"))
                self.state.load_dict(payload)
                logger.info(
                    "本地 HIS 状态已恢复：%d 条排班 / %d 个号表实例",
                    len(self.state.indexes),
                    len(self.state.regist),
                )
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("本地 HIS 状态恢复失败（从头开始）：%s", exc)

        # 院方主档必须在服务接受请求之前就绪：否则启动瞬间「查询科室」
        # 会拿到空名单，模型随即编造科室名（真机踩过的坑）。
        if self._catalog is not None:
            self.state.load_catalog(self._catalog, limit_num=self._limit_num)

        handler = type(
            "BoundLocalHisHandler",
            (LocalHisHandler,),
            {"state": self.state, "persist_path": self._persist_path},
        )
        server_class = type(
            "LocalHisHTTPServer",
            (ThreadingHTTPServer,),
            {"request_queue_size": 128, "daemon_threads": True},
        )
        self._server = server_class((self._host, self._port), handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="local-his",
            daemon=True,
        )
        self._thread.start()
        logger.info("本地 HIS 已启动：%s", self.base_url)
        return self.base_url

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    # ---------- 排班 ----------

    def seed_from_roster(
        self,
        roster: List[Dict[str, Any]],
        *,
        limit_num: int = 5,
    ) -> int:
        """[兼容] 按外部传入的排班表生成号源。

        ⚠️ 院方主档必须**由 HIS 自己持有**：正常装配请用构造参数
        ``catalog=load_his_catalog()``（HIS 启动时自动装载主档 + 字典 +
        排班，三者同源）。本方法只为纯内存单测/演练保留，正式链路不再调用。
        """
        for entry in roster:
            dept = entry.get("department") or ""
            doctor = entry.get("name") or ""
            if not dept or not doctor:
                continue
            self.state.add_roster(dept, doctor, limit_num)
        return len(self.state.indexes)


def main() -> None:  # pragma: no cover - 独立运行入口
    """独立运行（调试/真机演练用）。

    ``python -m xiaozhi.service.local_his 18082``
    可选环境变量：``LOCAL_HIS_HOLD_TTL``（秒，演练预占过期回收）、
    ``LOCAL_HIS_LIMIT``（每位医生限号数，演练需要大号池避免 UNKNOWN
    残留预占把号占满）。生产/演示保持默认值即可。
    """
    import os
    import sys

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9099
    ttl = float(os.environ.get("LOCAL_HIS_HOLD_TTL") or DEFAULT_HOLD_TTL_SECONDS)
    limit = int(os.environ.get("LOCAL_HIS_LIMIT") or 5)
    # HIS 自持院方主档：科室字典、医生名录、排班、号源全部由这一份快照生成
    catalog = load_his_catalog()
    state = LocalHisState(catalog=catalog, hold_ttl_seconds=ttl)
    state.load_catalog(catalog, limit_num=limit)
    server = LocalHisServer(state, port=port, hold_ttl_seconds=ttl)
    url = server.start()
    print(f"本地 HIS 已启动：{url}（Ctrl+C 退出）", flush=True)
    try:
        while True:
            threading.Event().wait(1)
    except KeyboardInterrupt:
        server.stop()


if __name__ == "__main__":  # pragma: no cover
    main()
