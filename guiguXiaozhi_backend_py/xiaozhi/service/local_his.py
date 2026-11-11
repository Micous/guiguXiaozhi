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

**运行方式**：由组合根以 ``slot_service_impl=local_his`` 装配时**内嵌启动**
（后台线程 + ThreadingHTTPServer），零外部进程；也可以独立运行
（``python -m xiaozhi.service.local_his``）便于调试。
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

#: HIS 统一响应包的成功码
HIS_SUCCESS_CODE = 200

#: 号表实例的时段枚举（与 HIS 的 timeDesc 对应）
TIME_SLOTS = ("上午", "下午")


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


@dataclass
class Hold:
    """预占记录。"""

    hold_id: str
    clinic_for_regist_id: str
    clinic_master_id: str
    created_at: str = ""


class LocalHisState:
    """本地 HIS 的全部状态（可持久化到 JSON）。

    持久化由 **state 自己**负责（而不是 HTTP handler）——
    任何路径改了号源（HTTP 或直接调用）都会落盘，不会出现
    "接口层改了但状态没存" 的不一致。
    """

    def __init__(self, persist_path: Optional[Path] = None) -> None:
        self.lock = threading.RLock()
        self.persist_path: Optional[Path] = (
            Path(persist_path) if persist_path else None
        )
        self.indexes: Dict[str, ClinicIndex] = {}
        self.schedules: Dict[str, ClinicSchedule] = {}
        self.regist: Dict[str, ClinicForRegist] = {}   # key: slot_id|clinic_date
        self.bookings: Dict[str, Booking] = {}
        self.holds: Dict[str, Hold] = {}
        self.idempotency: Dict[str, Dict[str, Any]] = {}
        self._seq = 0

    def next_id(self, prefix: str) -> str:
        with self.lock:
            self._seq += 1
            return f"{prefix}-{self._seq}"

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
            )
            result = {
                "holdId": hold_id,
                "clinicForRegistId": slot.clinic_for_regist_id,
                "clinicMasterId": master_id,
                "remaining": slot.remaining,
            }
            if idempotency_key:
                self.idempotency[idempotency_key] = result
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
            if idempotency_key and idempotency_key in self.idempotency:
                return self.idempotency[idempotency_key], ""
            hold = self.holds.get(hold_id)
            if hold is None:
                return None, f"预占不存在或已过期：{hold_id}"
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
            )
            self.holds.pop(hold_id, None)
            result = {
                "clinicMasterId": hold.clinic_master_id,
                "clinicForRegistId": slot.clinic_for_regist_id,
                "remaining": slot.remaining,
            }
            if idempotency_key:
                self.idempotency[idempotency_key] = result
            self.save()
            return result, ""

    def release_hold(self, hold_id: str) -> Tuple[bool, str]:
        """释放预占（失败回滚）：预占数减，号还回去。"""
        with self.lock:
            hold = self.holds.pop(hold_id, None)
            if hold is None:
                return False, f"预占不存在或已释放：{hold_id}"
            slot = self._find_regist(hold.clinic_for_regist_id)
            if slot is not None:
                slot.current_pre_num = max(0, slot.current_pre_num - 1)
            self.save()
            return True, ""

    def cancel_booking(
        self, clinic_master_id: str
    ) -> Tuple[bool, str]:
        """退号（用户取消）：已挂号数减，号还回去。"""
        with self.lock:
            booking = self.bookings.get(clinic_master_id)
            if booking is None:
                return False, f"挂号单不存在：{clinic_master_id}"
            if booking.status == "CANCELLED":
                return False, "该挂号单已退号。"
            slot = self._find_regist(booking.clinic_for_regist_id)
            if slot is not None:
                slot.current_num = max(0, slot.current_num - 1)
            booking.status = "CANCELLED"
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
                "idempotency": self.idempotency,
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
                self.holds[row["hold_id"]] = Hold(**row)
            self.idempotency.update(payload.get("idempotency", {}))
            self._seq = int(payload.get("seq") or 0)


class LocalHisHandler(BaseHTTPRequestHandler):
    """本地 HIS 的 HTTP 处理器（协议与参考项目 zp-* 一致）。"""

    state: LocalHisState = None  # 由 LocalHisServer 注入
    persist_path: Optional[Path] = None

    def log_message(self, *_args):  # 静音
        return

    # ---------- 工具 ----------

    def _send(self, code: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _ok(self, data: Any = None, msg: str = "ok") -> None:
        self._send(200, {"code": HIS_SUCCESS_CODE, "msg": msg, "data": data})

    def _fail(self, msg: str, code: int = 500) -> None:
        """业务失败：HTTP 200 但 code≠200（对齐 HIS 统一响应包）。"""
        self._send(200, {"code": code, "msg": msg, "data": None})

    def _not_found(self, msg: str) -> None:
        self._send(200, {"code": 404, "msg": msg, "data": None})

    def _read_body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return {}

    def _save(self) -> None:
        """落盘（委托给 state —— 持久化逻辑只有一份）。"""
        self.state.save()

    # ---------- 路由 ----------

    def do_GET(self):
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

    def do_POST(self):
        path = urlparse(self.path).path
        body = self._read_body()
        idem = self.headers.get("X-Idempotency-Key") or ""

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
            released, error = self.state.release_hold(str(body.get("holdId") or ""))
            if not released:
                self._fail(error)
                return
            self._save()
            self._ok({"released": True})
            return
        if path.endswith("/cancel"):
            cancelled, error = self.state.cancel_booking(
                str(body.get("clinicMasterId") or body.get("bookingId") or "")
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
    ) -> None:
        self._host = host
        self._port = port
        self._persist_path = Path(persist_path) if persist_path else None
        self.state = state or LocalHisState(persist_path=self._persist_path)
        if self.state.persist_path is None and self._persist_path is not None:
            self.state.persist_path = self._persist_path
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
        """按医生排班表生成号源（组合根调用）。

        ``roster`` 元素：``{"department": ..., "name": ...}``。
        限号数默认 5（与 MockSlotService 一致，可通过参数调）。
        """
        for entry in roster:
            dept = entry.get("department") or ""
            doctor = entry.get("name") or ""
            if not dept or not doctor:
                continue
            self.state.add_roster(dept, doctor, limit_num)
        return len(self.state.indexes)


def main() -> None:  # pragma: no cover - 独立运行入口
    """独立运行（调试用）：``python -m xiaozhi.service.local_his 9099``。"""
    import sys

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9099
    state = LocalHisState()
    state.add_roster("呼吸内科", "徐国兴", 5)
    state.add_roster("心内科", "张抒扬", 3)
    server = LocalHisServer(state, port=port)
    url = server.start()
    print(f"本地 HIS 已启动：{url}（Ctrl+C 退出）", flush=True)
    try:
        while True:
            threading.Event().wait(1)
    except KeyboardInterrupt:
        server.stop()


if __name__ == "__main__":  # pragma: no cover
    main()
