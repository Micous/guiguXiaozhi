"""HIS REST 客户端 —— Phase 3b（审计 P0-03 的对接落点）。

**这一层解决什么**

审计 P0-03 的核心风险是：

    查询有号 ≠ 实际锁定号源；本地预约成功 ≠ 医院挂号成功；
    外部 HIS 成功但本地超时 → 结果不确定。

也就是说，真实接入必须有「预占 → 确认 → 释放 → 查单」四段式，
而不是查一下号源就直接写本地表。本模块提供这四段式的 HTTP 契约。

**契约来源**（`参考项目/his`，zp-* Spring Cloud 微服务）：

* 统一响应包：``R{code:int, msg:str, data:T}``（``zp-common/zp-core/.../R.java``）
  —— ``code == 200`` 才是成功，**HTTP 200 不代表业务成功**；
* 号源查询：``GET /clinicForRegist/list``（不分页列表）；
* 挂号落单：``POST /currentRegister/add``；
* 号源三层实体：``clinic_index``（科室排班索引）→ ``clinic_schedule``
  （医生出诊安排）→ ``clinic_for_regist``（**按日期展开的号表实例**，
  带 ``currentNum`` 已挂号数 / ``currentPreNum`` 已预占数 /
  ``limitNum`` 限号数）；
* 认证：Sa-Token（``@SaCheckPermission``），走请求头携带 token；
* 落单顺序：**先扣号源再落单**（Seata 事务）—— 所以"扣源成功、落单失败"
  是真实存在的中间态，这正是我们 ``UNKNOWN`` 状态的语义落点。

**超时 = 不确定，不是失败**

本模块最重要的分类纪律：

.. code-block:: text

    HTTP 超时 / 连接断开 / 5xx   → HisUncertainError  → UNKNOWN（必须对账）
    HTTP 4xx / code != 200       → HisRejectedError   → 明确失败
    成功                          → 正常返回

把超时当成"失败"会导致：HIS 其实已经扣了号源并落单，我们却告诉用户
"预约失败"，用户重试 → 重复挂号。宁可说"核实中"，也不能说假话。

**幂等**：写操作必须带幂等键（``X-Idempotency-Key`` 头 + 请求体字段），
由 HIS 侧保证"同一键只处理一次"。没有沙箱前无法验证 HIS 侧行为，
但**我们这边必须先做到**：同一幂等键重发 → 同一结果、不重复扣号源。
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# HIS 统一响应包的成功码（R.SUCCESS）
HIS_SUCCESS_CODE = 200


class HisError(Exception):
    """HIS 调用异常基类。"""


class HisRejectedError(HisError):
    """HIS 明确拒绝（业务失败）—— 可以如实告诉用户失败原因。

    HTTP 4xx 或 ``code != 200`` 且语义明确（如号源已满、患者不存在）。
    """

    def __init__(self, message: str, *, code: Optional[int] = None) -> None:
        self.code = code
        super().__init__(message)


class HisUncertainError(HisError):
    """HIS 调用结果**不确定**（超时 / 连接中断 / 5xx）—— 必须对账。

    绝不能当成失败：HIS 可能已经扣了号源甚至落了单。
    """

    def __init__(self, message: str, *, code: Optional[int] = None) -> None:
        self.code = code
        super().__init__(message)


class HisAuthError(HisRejectedError):
    """认证/授权失败（token 过期、权限不足）。"""


@dataclass
class HisResponse:
    """统一响应包解包结果。"""

    code: int
    msg: str
    data: Any = None

    @property
    def ok(self) -> bool:
        return self.code == HIS_SUCCESS_CODE


@dataclass
class HisSlot:
    """HIS 号表实例（对应 ``clinic_for_regist`` 一行）。

    字段名与 HIS 实体保持一致（``currentNum`` / ``currentPreNum`` /
    ``limitNum``），避免二次映射时产生语义漂移。
    """

    clinic_for_regist_id: str = ""
    clinic_index_id: str = ""
    clinic_schedule_id: str = ""
    clinic_date: str = ""
    time_desc: str = ""
    limit_num: int = 0
    limit_pre_num: int = 0
    current_num: int = 0
    current_regis_num: int = 0
    current_pre_num: int = 0
    org_id: str = ""
    doctor_name: str = ""
    dept_name: str = ""

    @property
    def remaining(self) -> int:
        """剩余可挂号数 = 限号数 - 已挂号 - 已预占。

    ⚠️ 必须扣掉 ``currentPreNum``（预占数）—— 否则两个并发请求会
    各自看到"还有号"，然后同时去扣，第二个要么被 HIS 拒绝、
    要么把号源扣成负数。
    """
        return max(0, self.limit_num - self.current_num - self.current_pre_num)


@dataclass
class HisHold:
    """预占结果（``hold_slot``）。"""

    held: bool
    hold_id: str = ""
    external_booking_id: str = ""
    slot: Optional[HisSlot] = None
    message: str = ""
    expires_at: Optional[str] = None


@dataclass
class HisBooking:
    """挂号单结果（``confirm_hold``）。"""

    confirmed: bool
    external_booking_id: str = ""
    message: str = ""
    slot: Optional[HisSlot] = None


@dataclass
class HisBookingStatus:
    """查单结果（``query_booking``）—— 对账的依据。"""

    found: bool
    external_booking_id: str = ""
    status: str = ""  # REGISTERED / CANCELLED / UNKNOWN
    message: str = ""
    slot: Optional[HisSlot] = None


class HisClient:
    """HIS REST 客户端。

    只做**协议**：HTTP 调用、认证、响应包解包、错误分类。
    业务语义（三层号源映射、预占/确认流程）在 ``his_slot_service.py``。
    """

    def __init__(
        self,
        base_url: str,
        *,
        token: str = "",
        timeout: float = 10.0,
        idempotency_header: str = "X-Idempotency-Key",
        paths: Optional[Dict[str, str]] = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._idempotency_header = idempotency_header
        # 端点路径可配置：医院侧实现差异很大，沙箱到位后按实际对齐，
        # 不必改代码（参考项目里"预占"就没有独立端点）。
        self._paths: Dict[str, str] = {
            "hold": "/clinicForRegist/hold",
            "confirm": "/currentRegister/add",
            "release": "/clinicForRegist/release",
            "cancel": "/currentRegister/cancel",
        }
        if paths:
            self._paths.update({k: v for k, v in paths.items() if v})

    # ---------- 协议层 ----------

    def _url(self, path: str, params: Optional[Dict[str, Any]] = None) -> str:
        url = f"{self._base_url}/{path.lstrip('/')}"
        if params:
            query = {
                k: v for k, v in params.items() if v is not None and v != ""
            }
            if query:
                url = f"{url}?{urllib.parse.urlencode(query, doseq=True)}"
        return url

    def _headers(self, idempotency_key: Optional[str] = None) -> Dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self._token:
            # Sa-Token：请求头携带 token
            headers["satoken"] = self._token
            headers["Authorization"] = f"Bearer {self._token}"
        if idempotency_key:
            headers[self._idempotency_header] = idempotency_key
        return headers

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
        idempotency_key: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> HisResponse:
        """发起一次 HIS 调用并解包统一响应。

        错误分类纪律（见模块 docstring）：
          * 超时 / 连接错误 / 5xx → ``HisUncertainError``（**必须对账**）；
          * 401/403 → ``HisAuthError``；
          * 其他 4xx → ``HisRejectedError``；
          * ``code != 200`` → ``HisRejectedError``。
        """
        url = self._url(path, params)
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
        request = urllib.request.Request(url, data=data, method=method.upper())
        for key, value in self._headers(idempotency_key).items():
            request.add_header(key, value)

        try:
            with urllib.request.urlopen(
                request, timeout=timeout or self._timeout
            ) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            if exc.code in (401, 403):
                raise HisAuthError(
                    f"HIS 认证/授权失败（HTTP {exc.code}）：{detail or exc.reason}",
                    code=exc.code,
                ) from exc
            if exc.code >= 500:
                # 5xx：HIS 内部异常，**结果不确定** —— 可能已扣号源
                raise HisUncertainError(
                    f"HIS 服务端错误（HTTP {exc.code}），结果待核实：{detail}",
                    code=exc.code,
                ) from exc
            raise HisRejectedError(
                f"HIS 拒绝了请求（HTTP {exc.code}）：{detail or exc.reason}",
                code=exc.code,
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # 连接失败/超时 —— 结果不确定
            raise HisUncertainError(
                f"无法确认 HIS 调用结果（连接失败或超时）：{exc}"
            ) from exc

        try:
            payload = json.loads(raw or "{}")
        except json.JSONDecodeError as exc:
            raise HisUncertainError(
                f"HIS 返回的不是合法 JSON（结果待核实）：{raw[:200]}"
            ) from exc

        code = int(payload.get("code", HIS_SUCCESS_CODE))
        message = str(payload.get("msg") or payload.get("message") or "")
        data = payload.get("data")
        response = HisResponse(code=code, msg=message, data=data)
        if not response.ok:
            raise HisRejectedError(
                message or f"HIS 返回失败（code={code}）", code=code
            )
        return response

    # ---------- 号源查询 ----------

    def query_slots(self, params: Dict[str, Any]) -> List[HisSlot]:
        """查询号表实例列表（对应 ``GET /clinicForRegist/list``）。"""
        response = self.request("GET", "/clinicForRegist/list", params=params)
        rows = response.data if isinstance(response.data, list) else []
        return [self._to_slot(row) for row in rows if isinstance(row, dict)]

    def query_slot_by_id(self, clinic_for_regist_id: str) -> Optional[HisSlot]:
        """按号表实例 id 查详情（对应 ``GET /clinicForRegist/{id}``）。"""
        response = self.request(
            "GET", f"/clinicForRegist/{clinic_for_regist_id}"
        )
        if isinstance(response.data, dict):
            return self._to_slot(response.data)
        return None

    # ---------- 预占 / 确认 / 释放 / 查单 ----------

    def hold_slot(
        self,
        payload: Dict[str, Any],
        *,
        idempotency_key: str,
    ) -> HisHold:
        """预占号源。

        真实 HIS 的预占接口名取决于医院实现（参考项目是「先扣号源再落单」
        的组合动作，没有独立预占端点）。这里把**端点路径做成可配置**，
        沙箱到位后只需改配置，不需要改代码。
        """
        response = self.request(
            "POST",
            self._hold_path(),
            body=payload,
            idempotency_key=idempotency_key,
        )
        data = response.data if isinstance(response.data, dict) else {}
        slot = self._to_slot(data) if (
            data.get("clinicForRegistId") or data.get("clinic_for_regist_id")
        ) else None
        return HisHold(
            held=bool(data.get("holdId") or data.get("hold_id")),
            hold_id=str(data.get("holdId") or data.get("hold_id") or ""),
            external_booking_id=str(
                data.get("clinicMasterId")
                or data.get("clinic_master_id")
                or data.get("bookingId")
                or ""
            ),
            slot=slot,
            message=response.msg,
            expires_at=data.get("expireAt") or data.get("expiresAt"),
        )

    def confirm_hold(
        self,
        payload: Dict[str, Any],
        *,
        idempotency_key: str,
    ) -> HisBooking:
        """确认预占并落单（对应 ``POST /currentRegister/add``）。"""
        response = self.request(
            "POST",
            self._confirm_path(),
            body=payload,
            idempotency_key=idempotency_key,
        )
        data = response.data if isinstance(response.data, dict) else {}
        return HisBooking(
            confirmed=True,
            external_booking_id=str(
                data.get("clinicMasterId")
                or data.get("clinic_master_id")
                or data.get("bookingId")
                or data.get("registId")
                or ""
            ),
            message=response.msg,
        )

    def release_hold(
        self,
        hold_id: str,
        *,
        idempotency_key: str,
    ) -> bool:
        """释放预占（把号源还回去）。"""
        self.request(
            "POST",
            self._release_path(),
            body={"holdId": hold_id},
            idempotency_key=idempotency_key,
        )
        return True

    def query_booking(self, external_booking_id: str) -> HisBookingStatus:
        """查单 —— 对账的依据。

        查不到（404 / 空）→ ``found=False``：说明 HIS 侧确实没有这条单，
        可以把本地 UNKNOWN 收敛为失败；查到 → 按 HIS 的状态推进本地状态。
        """
        try:
            response = self.request(
                "GET", f"/currentRegister/{external_booking_id}"
            )
        except HisRejectedError as exc:
            if exc.code == 404:
                return HisBookingStatus(
                    found=False, external_booking_id=external_booking_id
                )
            raise
        data = response.data if isinstance(response.data, dict) else {}
        return HisBookingStatus(
            found=True,
            external_booking_id=str(
                data.get("clinicMasterId")
                or data.get("clinic_master_id")
                or data.get("bookingId")
                or external_booking_id
            ),
            status=str(data.get("status") or data.get("registStatus") or ""),
            message=response.msg,
        )

    def cancel_booking(
        self,
        external_booking_id: str,
        *,
        idempotency_key: str = "",
    ) -> bool:
        """退号（用户取消预约）—— 把号源还给医院。

        ⚠️ **真实 HIS 的退号接口在参考项目里没有调研**（端点路径未知），
        所以路径做成可配置（默认 ``/currentRegister/cancel``）。
        沙箱到位后按实际对齐即可。

        退号失败**必须让上层知道**：本地说"取消成功"而医院还留着号，
        用户到院发现没退 —— 这是比"退不掉"更糟的结果。
        """
        self.request(
            "POST",
            self._cancel_path(),
            body={"clinicMasterId": external_booking_id},
            idempotency_key=idempotency_key or None,
        )
        return True

    # ---------- 端点路径（可配置，等沙箱到位后对齐） ----------

    def _hold_path(self) -> str:
        return self._paths.get("hold", "/clinicForRegist/hold")

    def _confirm_path(self) -> str:
        return self._paths.get("confirm", "/currentRegister/add")

    def _release_path(self) -> str:
        return self._paths.get("release", "/clinicForRegist/release")

    def _cancel_path(self) -> str:
        return self._paths.get("cancel", "/currentRegister/cancel")

    def configure_paths(self, **paths: str) -> None:
        """覆盖端点路径（沙箱对齐时用；键：hold / confirm / release / cancel）。"""
        self._paths.update({k: v for k, v in paths.items() if v})

    # ---------- 字段映射 ----------

    @staticmethod
    def _to_slot(row: Dict[str, Any]) -> HisSlot:
        """HIS 行 → ``HisSlot``。

        兼容三种命名：驼峰（``currentNum``，参考项目风格）、下划线
        （``current_num``，本地 HIS 替身风格）、以及少数字段缺失。
        医院侧实现差异很大，两个都认。
        """

        def pick(*names: str, default: Any = "") -> Any:
            for name in names:
                if name in row and row[name] is not None:
                    return row[name]
            return default

        def as_int(value: Any) -> int:
            try:
                return int(value)
            except (TypeError, ValueError):
                return 0

        return HisSlot(
            clinic_for_regist_id=str(
                pick("clinicForRegistId", "clinic_for_regist_id")
            ),
            clinic_index_id=str(pick("clinicIndexId", "clinic_index_id")),
            clinic_schedule_id=str(pick("clinicScheduleId", "clinic_schedule_id")),
            clinic_date=str(pick("clinicDate", "clinic_date", "regDate")),
            time_desc=str(pick("timeDesc", "time_desc", "amPm")),
            limit_num=as_int(pick("limitNum", "limit_num")),
            limit_pre_num=as_int(pick("limitPreNum", "limit_pre_num")),
            current_num=as_int(pick("currentNum", "current_num")),
            current_regis_num=as_int(pick("currentRegisNum", "current_regis_num")),
            current_pre_num=as_int(pick("currentPreNum", "current_pre_num")),
            org_id=str(pick("orgId", "org_id")),
            doctor_name=str(pick("doctorName", "doctor_name", "doctorName2")),
            dept_name=str(pick("deptName", "dept_name", "deptName2")),
        )
