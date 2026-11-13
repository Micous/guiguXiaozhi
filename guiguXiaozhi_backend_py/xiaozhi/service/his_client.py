"""HIS REST 客户端 —— Phase 3b 对接落点 + 边界韧性加固。

**核心分类纪律**（详见 ``his_resilience.py``）

.. code-block:: text

    读超时 / 连接中途断开 / 5xx / 系统类业务码 → HisUncertainError  → UNKNOWN
    HTTP 4xx / 明确业务拒绝码                  → HisRejectedError   → 明确失败
    连接被拒绝 / DNS 失败 / 429 / 熔断打开      → HisUnavailableError → 明确"暂时不可用"
    成功码但响应结构不完整                     → HisUncertainError  → UNKNOWN

把超时当"失败"会导致 HIS 已落单却告诉用户失败 → 用户重试 → 重复挂号；
把"连接根本没建立"说成"核实中"会产生一堆永远收敛不了的 UNKNOWN。两者都不能混。

**网络波动的收敛能力**

* 有限重试 + 指数退避 + 抖动，只对可重试的失败；
* **写操作只有带幂等键时才重试** —— 这时重试等价于「用同一幂等键重放查证」，
  HIS 幂等则直接返回首次结果，UNKNOWN 收敛为确定结果；裸写绝不重试；
* 整体 deadline：socket timeout 只管单次 recv，服务端慢吐字节可无限拖，
  读取循环里逐步检查总预算；
* 熔断器：HIS 宕机时快速失败，不让每个请求都等满超时打满线程池；
* 禁用自动重定向与环境代理、响应体大小上限、错误正文脱敏（PII/token）。

**契约**：统一响应包 ``R{code,msg,data}``（code==200 才是成功）；
号源 ``GET /clinicForRegist/list``；落单 ``POST /currentRegister/add``；
认证 Sa-Token 走请求头；号源三层 ``index → schedule → for_regist``。
"""

from __future__ import annotations

import hashlib
import json
import logging
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List, Optional

from xiaozhi.ops.bounded_io import get_bounded_io
from xiaozhi.service.his_resilience import (
    DEFAULT_UNCERTAIN_BUSINESS_CODES,
    KIND_AUTH,
    KIND_NOT_SENT,
    KIND_REJECTED,
    KIND_UNCERTAIN,
    MSG_AUTH,
    MSG_CIRCUIT_OPEN,
    MSG_PROTOCOL,
    MSG_UNAVAILABLE,
    MSG_UNCERTAIN,
    CircuitBreaker,
    Decision,
    RetryPolicy,
    classify_business_code,
    classify_http_status,
    decision_from_exception,
    redact_detail,
)

logger = logging.getLogger(__name__)

HIS_SUCCESS_CODE = 200
#: 响应体上限（1MB）—— 故障 HIS 可能吐回整页 HTML 或无限流
DEFAULT_MAX_RESPONSE_BYTES = 1 << 20
_READ_CHUNK = 65536


class HisError(Exception):
    """HIS 调用异常基类。

    ``message`` 是用户可见的中性话术；``detail`` 是脱敏后的原始错误，
    只用于日志/排障。二者分离，避免 HIS 错误正文里的身份证号、token
    经工具结果 → 模型 → 用户回复 → 历史落库泄漏出去。
    """

    def __init__(
        self, message: str, *, code: Optional[int] = None, detail: str = ""
    ) -> None:
        self.code = code
        self.detail = detail or ""
        super().__init__(message)


class HisRejectedError(HisError):
    """HIS 明确拒绝（业务失败）—— 可以如实告诉用户失败原因。"""


class HisUncertainError(HisError):
    """HIS 调用结果**不确定**（超时 / 连接中断 / 5xx）—— 必须对账。"""


class HisAuthError(HisRejectedError):
    """认证/授权失败 —— 不重试，需要运维换 token。"""


class HisUnavailableError(HisRejectedError):
    """HIS **确定没收到请求**（连接被拒绝 / DNS 失败 / 429 / 熔断打开）。

    与 ``HisUncertainError`` 的区别：请求没送达 ⇒ 确定没执行 ⇒
    应如实说"暂时不可用"，**不产生 UNKNOWN**。
    """


class HisCircuitOpenError(HisUnavailableError):
    """熔断器打开，请求未发出。"""


class HisPatientMasterUnsupported(HisRejectedError):
    """院方 HIS 没有患者建档端点（明确 404），无法取得院方患者引用。

    这是**能力缺失**而不是调用失败：业务层可据此决定是否允许降级到
    本地派生引用；production 下应拒绝，避免出现「本地约上了、医院没有
    这个患者」的脏数据。
    """


def _raise_error(decision: Decision) -> None:
    """分类结论 → 具体异常（唯一出口，保证分类纪律不被各处绕过）。"""
    kwargs = {"code": decision.code, "detail": decision.detail}
    if decision.kind == KIND_AUTH:
        raise HisAuthError(decision.message or MSG_AUTH, **kwargs)
    if decision.kind == KIND_UNCERTAIN:
        raise HisUncertainError(decision.message or MSG_UNCERTAIN, **kwargs)
    if decision.kind == KIND_NOT_SENT:
        raise HisUnavailableError(decision.message or MSG_UNAVAILABLE, **kwargs)
    raise HisRejectedError(decision.message, **kwargs)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """禁用自动重定向：POST 会被改成 GET，且 satoken 可能转发到第三方。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


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
    """HIS 号表实例（对应 ``clinic_for_regist`` 一行）。"""

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
        """剩余号 = 限号 - 已挂号 - **已预占**（不扣预占数会并发超卖）。"""
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
    """查单结果 —— 对账的依据。"""

    found: bool
    external_booking_id: str = ""
    status: str = ""  # REGISTERED / CANCELLED / UNKNOWN
    message: str = ""
    slot: Optional[HisSlot] = None


class HisClient:
    """HIS REST 客户端（只做协议；业务语义在 ``his_slot_service.py``）。"""

    def __init__(
        self,
        base_url: str,
        *,
        token: str = "",
        timeout: float = 10.0,
        idempotency_header: str = "X-Idempotency-Key",
        paths: Optional[Dict[str, str]] = None,
        retry_policy: Optional[RetryPolicy] = None,
        breaker: Optional[CircuitBreaker] = None,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        metrics: Optional[Any] = None,
        uncertain_business_codes: Optional[FrozenSet[int]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = max(0.01, float(timeout))
        self._idempotency_header = idempotency_header
        # 院方网关常要求额外请求头（租户/院区/故障注入标记），做成可配置
        self._extra_headers = dict(headers or {})
        self._retry = retry_policy or RetryPolicy()
        self._breaker = breaker or CircuitBreaker()
        self._max_response_bytes = max(1024, int(max_response_bytes))
        self._metrics = metrics
        self._uncertain_codes = (
            frozenset(uncertain_business_codes)
            if uncertain_business_codes is not None
            else DEFAULT_UNCERTAIN_BUSINESS_CODES
        )
        #: **院方端点映射全部集中在这里** —— 接真实 HIS 时只改这张表
        #:（或注入 ``paths=`` / 环境变量），其余代码不动。
        #: 前缀类键（booking / byKey / slot / dept）用于拼接 ``{id}`` 子路径。
        self._paths: Dict[str, str] = {
            "slots": "/clinicForRegist/list",
            "slot": "/clinicForRegist",
            "hold": "/clinicForRegist/hold",
            "confirm": "/currentRegister/add",
            "release": "/clinicForRegist/release",
            "cancel": "/currentRegister/cancel",
            "booking": "/currentRegister",
            "byKey": "/currentRegister/byKey",
            "bookings": "/clinicMaster/list",
            "patient": "/patMasterIndex/add",
            "patients": "/patMasterIndex/list",
            "depts": "/dept/list",
            "dept": "/dept",
            "doctors": "/doctor/list",
        }
        if paths:
            self._paths.update({k: v for k, v in paths.items() if v})
        # 显式禁用环境代理：本机 HTTP_PROXY 会把内网 HIS 请求送去代理，
        # 表现为莫名其妙的 502 / 连接失败（真机已踩过）。
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirectHandler()
        )

    @property
    def breaker(self) -> CircuitBreaker:
        return self._breaker

    def _metric(self, name: str) -> None:
        if self._metrics is None:
            return
        try:
            self._metrics.inc(f"his_{name}")
        except Exception:  # 指标故障绝不影响业务调用
            logger.debug("HIS 指标写入失败：%s", name)

    # ---------- 协议层 ----------

    def _url(self, path: str, params: Optional[Dict[str, Any]] = None) -> str:
        url = f"{self._base_url}/{path.lstrip('/')}"
        if params:
            query = {k: v for k, v in params.items() if v is not None and v != ""}
            if query:
                url = f"{url}?{urllib.parse.urlencode(query, doseq=True)}"
        return url

    def _headers(self, idempotency_key: Optional[str] = None) -> Dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        headers.update(self._extra_headers)
        if self._token:
            headers["satoken"] = self._token
            headers["Authorization"] = f"Bearer {self._token}"
        if idempotency_key:
            headers[self._idempotency_header] = idempotency_key
        return headers

    @staticmethod
    def _derive_idempotency_key(
        method: str, path: str, body: Optional[Dict[str, Any]]
    ) -> str:
        """调用方没给幂等键时派生稳定键：同一请求永远同一个键。"""
        raw = json.dumps(body or {}, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(f"{method}|{path}|{raw}".encode("utf-8")).hexdigest()

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
        """发起一次 HIS 调用并解包统一响应（含重试 / 总时限 / 熔断）。"""
        method = method.upper()
        if method not in ("GET", "HEAD") and not idempotency_key:
            idempotency_key = self._derive_idempotency_key(method, path, body)
            logger.warning(
                "HIS 写操作未带幂等键，已自动派生（调用方应显式传入）：%s %s",
                method, path,
            )

        if not self._breaker.allow():
            self._metric("circuit_open_total")
            raise HisCircuitOpenError(
                MSG_CIRCUIT_OPEN, detail="circuit breaker open, request not sent"
            )

        per_call_timeout = max(0.01, float(timeout or self._timeout))
        deadline = time.monotonic() + self._retry.budget(per_call_timeout)
        last_exc: Optional[HisError] = None

        for attempt in range(1, self._retry.max_attempts + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                response = self._once(
                    method,
                    path,
                    params=params,
                    body=body,
                    idempotency_key=idempotency_key,
                    socket_timeout=min(per_call_timeout, remaining),
                    deadline=deadline,
                )
            except HisError as exc:
                last_exc = exc
                decision = decision_from_exception(exc)
                self._record_failure(decision, method, path)
                next_delay = (
                    self._retry.backoff(attempt)
                    if attempt < self._retry.max_attempts
                    else 0.0
                )
                if not self._retry.should_retry(
                    decision,
                    method=method,
                    idempotency_key=idempotency_key or "",
                    attempt=attempt,
                    remaining=deadline - time.monotonic(),
                    next_delay=next_delay,
                ):
                    raise
                if time.monotonic() + next_delay >= deadline:
                    raise
                logger.info(
                    "HIS 调用重试 %d/%d（%s %s kind=%s，%.2fs 后）",
                    attempt + 1, self._retry.max_attempts,
                    method, path, decision.kind, next_delay,
                )
                self._metric("retries_total")
                time.sleep(next_delay)
                continue
            else:
                self._breaker.record_success()
                self._metric("requests_total")
                return response

        if last_exc is not None:
            raise last_exc
        raise HisUncertainError(
            MSG_UNCERTAIN, detail="整体时限在首次调用前耗尽"
        )

    async def request_async(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
        idempotency_key: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> HisResponse:
        """B3-1：async 链路专用的 HIS 调用入口。

        把**整段同步重试循环**（含 ``time.sleep`` 退避）一次性 offload 到
        ``BoundedIOExecutor`` 的专用线程池，等价于「把阻塞与 sleep 移出 event
        loop」。这是 ``time.sleep → await asyncio.sleep`` 的等价实现：

        * 把 ``request()`` 改成 async 会破坏同步契约并波及全部服务层与 914 条
          用例，不符合 B3-1「行为不变」硬约束；
        * 在线程里 sleep 只占工作线程，不再阻塞事件循环，且重试 deadline、
          熔断、幂等键等语义逐字不变。

        新增入口，既有 ``request()`` 与所有同步调用方零改动。返回值与异常经
        执行器原样传递。
        """
        return await get_bounded_io().run(
            self.request,
            method,
            path,
            params=params,
            body=body,
            idempotency_key=idempotency_key,
            timeout=timeout,
        )

    def _record_failure(self, decision: Decision, method: str, path: str) -> None:
        self._breaker.record_failure(infra=decision.infra_failure)
        counter = {
            KIND_UNCERTAIN: "uncertain_total",
            KIND_NOT_SENT: "unavailable_total",
            KIND_AUTH: "auth_failure_total",
            KIND_REJECTED: "rejected_total",
        }.get(decision.kind, "errors_total")
        self._metric(counter)
        if decision.kind != KIND_REJECTED:
            logger.warning(
                "HIS 调用失败 %s %s：kind=%s code=%s detail=%s",
                method, path, decision.kind, decision.code, decision.detail,
            )

    def _once(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]],
        body: Optional[Dict[str, Any]],
        idempotency_key: Optional[str],
        socket_timeout: float,
        deadline: float,
    ) -> HisResponse:
        """单次 HTTP 尝试（不含重试）。"""
        url = self._url(path, params)
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
        request = urllib.request.Request(url, data=data, method=method)
        for key, value in self._headers(idempotency_key).items():
            request.add_header(key, value)

        try:
            with self._opener.open(request, timeout=socket_timeout) as response:
                raw = self._read_body(response, deadline)
        except urllib.error.HTTPError as exc:
            # HTTPError 是 URLError 子类，必须先捕获
            _raise_error(classify_http_status(exc.code, self._error_detail(exc)))
        except HisError:
            raise
        except urllib.error.URLError as exc:
            self._raise_url_error(exc)
        except (TimeoutError, ConnectionError, OSError) as exc:
            raise HisUncertainError(
                MSG_UNCERTAIN, detail=redact_detail(str(exc))
            ) from exc
        except Exception as exc:  # 未预期的协议层异常一律归"不确定"
            raise HisUncertainError(
                MSG_UNCERTAIN, detail=redact_detail(repr(exc))
            ) from exc

        return self._parse_envelope(raw)

    def _read_body(self, response: Any, deadline: float) -> str:
        """分块读响应，同时守住**整体**时限与大小上限。

        socket timeout 只约束单次 recv；HIS 每隔 0.4s 吐一个字节就能把
        单次超时永远重置。只有整体 deadline 能拦住这种"慢吐"。
        """
        chunks: List[bytes] = []
        total = 0
        while True:
            if time.monotonic() >= deadline:
                raise HisUncertainError(
                    MSG_UNCERTAIN, detail="整体时限耗尽（HIS 响应过慢）"
                )
            # read(65536) 会一直等到**读满**或 EOF：HIS 每 0.12s 吐 512B 时
            # 单次 socket 超时永远不触发，deadline 也没有机会被检查（真机
            # 演练里因此拖了 15.7s）。read1() 拿到任意字节就返回，让
            # 整体 deadline 真正生效。
            read1 = getattr(response, "read1", None)
            chunk = (
                read1(_READ_CHUNK)
                if read1 is not None
                else response.read(min(_READ_CHUNK, 8192))
            )
            if not chunk:
                break
            total += len(chunk)
            if total > self._max_response_bytes:
                self._metric("oversize_response_total")
                raise HisUncertainError(
                    MSG_PROTOCOL, detail="响应体超过大小上限"
                )
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8", "replace")

    def _error_detail(self, exc: urllib.error.HTTPError) -> str:
        try:
            raw = exc.read(4096)
            return raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        except Exception:
            return str(getattr(exc, "reason", exc))

    def _raise_url_error(self, exc: urllib.error.URLError) -> None:
        """区分「请求没发出去」与「发出去后断在半路」。"""
        reason = getattr(exc, "reason", None)
        text = redact_detail(str(reason or exc))
        lowered = text.lower()
        definite_markers = (
            "connection refused", "name or service not known",
            "nodename nor servname", "temporary failure in name resolution",
            "no route to host", "network is unreachable",
        )
        if (
            isinstance(reason, (ConnectionRefusedError, socket.gaierror))
            or any(marker in lowered for marker in definite_markers)
        ):
            raise HisUnavailableError(MSG_UNAVAILABLE, detail=text) from exc
        # 连接中途断开 / 读超时：HIS 可能已经执行 → 不确定
        raise HisUncertainError(MSG_UNCERTAIN, detail=text) from exc

    def _parse_envelope(self, raw: str) -> HisResponse:
        """解包 ``R{code,msg,data}``；协议违规一律按"结果不确定"处理。"""
        try:
            payload = json.loads(raw or "{}")
        except (json.JSONDecodeError, ValueError) as exc:
            self._metric("malformed_response_total")
            raise HisUncertainError(
                MSG_PROTOCOL, detail=redact_detail(raw)
            ) from exc
        if not isinstance(payload, dict) or "code" not in payload:
            self._metric("malformed_response_total")
            raise HisUncertainError(
                MSG_PROTOCOL, detail="响应不是统一响应包或缺少 code 字段"
            )
        try:
            code = int(payload["code"])
        except (TypeError, ValueError) as exc:
            self._metric("malformed_response_total")
            raise HisUncertainError(
                MSG_PROTOCOL, detail="响应包 code 不是整数"
            ) from exc
        message = str(payload.get("msg") or payload.get("message") or "")
        if code != HIS_SUCCESS_CODE:
            _raise_error(
                classify_business_code(
                    code, message, uncertain_codes=self._uncertain_codes
                )
            )
        return HisResponse(code=code, msg=message, data=payload.get("data"))

    # ---------- 院方主档：科室字典 / 医生名录 ----------

    def _rows(self, response: HisResponse, label: str) -> List[Dict[str, Any]]:
        """解包列表响应（兼容数组与分页对象），结构异常 = 结果不确定。"""
        rows = response.data
        if rows is None:
            return []
        if isinstance(rows, dict):
            for key in ("rows", "records", "list", "items"):
                if isinstance(rows.get(key), list):
                    rows = rows[key]
                    break
            else:
                self._metric("malformed_response_total")
                raise HisUncertainError(
                    MSG_PROTOCOL, detail=f"{label}结构异常（既非数组也非分页对象）"
                )
        if not isinstance(rows, list):
            self._metric("malformed_response_total")
            raise HisUncertainError(MSG_PROTOCOL, detail=f"{label}结构异常")
        return [row for row in rows if isinstance(row, dict)]

    def list_departments(self, *, org_id: str = "") -> List[Dict[str, Any]]:
        """科室字典 —— 应用侧「查询科室」的唯一入口。

        返回行含 ``deptId / deptCode / deptName / orgId / status``。
        院方字段名不同时，只改 ``_FIELD_MAP`` 或这里的映射，工具层不动。
        """
        response = self.request(
            "GET", self.path("depts") or "/dept/list",
            params={"orgId": org_id} if org_id else None,
        )
        return self._rows(response, "科室字典列表")

    def list_doctors(
        self, dept_name: str = "", *, keyword: str = ""
    ) -> List[Dict[str, Any]]:
        """医生名录 —— 应用侧「查询科室专家」的唯一入口。

        返回行含 ``doctorId / deptName / doctorName / title / expertise``。
        """
        params: Dict[str, Any] = {}
        if dept_name:
            params["deptName"] = dept_name
        if keyword:
            params["keyword"] = keyword
        response = self.request(
            "GET", self.path("doctors") or "/doctor/list", params=params or None
        )
        return self._rows(response, "医生名录列表")

    # ---------- 号源查询 ----------

    def query_slots(self, params: Dict[str, Any]) -> List[HisSlot]:
        """查询号表实例列表（对应 ``GET /clinicForRegist/list``）。"""
        response = self.request(
            "GET", self.path("slots") or "/clinicForRegist/list", params=params
        )
        # 统一走 _rows（兼容数组与分页对象），避免三类列表各写一份解包
        return [self._to_slot(row) for row in self._rows(response, "号源列表")]

    def query_slot_by_id(self, clinic_for_regist_id: str) -> Optional[HisSlot]:
        """按号表实例 id 查详情。"""
        response = self.request(
            "GET", f"{self.path('slot') or '/clinicForRegist'}/{clinic_for_regist_id}"
        )
        if isinstance(response.data, dict):
            return self._to_slot(response.data)
        return None

    # ---------- 患者主档 ----------

    def create_patient(
        self, payload: Dict[str, Any], *, idempotency_key: str = ""
    ) -> Dict[str, Any]:
        """建档 / 查主档（对应 ``POST /patMasterIndex/add``）。

        真实 HIS 的挂号单必须挂到患者主档；本地沙箱允许调用方显式指定
        ``patientRef``，让本项目生成的脱敏引用能直接用于链路联调。
        """
        response = self.request(
            "POST", self.path("patient") or "/patMasterIndex/add", body=payload,
            idempotency_key=idempotency_key or None,
        )
        return response.data if isinstance(response.data, dict) else {}

    # ---------- 预占 / 确认 / 释放 / 查单 ----------

    def hold_slot(
        self, payload: Dict[str, Any], *, idempotency_key: str
    ) -> HisHold:
        """预占号源（端点路径可配置，按院方实现对齐）。"""
        response = self.request(
            "POST", self._hold_path(), body=payload,
            idempotency_key=idempotency_key,
        )
        data = response.data
        if not isinstance(data, dict):
            self._metric("malformed_response_total")
            raise HisUncertainError(
                MSG_UNCERTAIN, detail="预占成功但响应缺少 data 对象"
            )
        if data.get("held") is False or data.get("hold") is False:
            return HisHold(held=False, message=response.msg)
        hold_id = str(data.get("holdId") or data.get("hold_id") or "")
        if not hold_id:
            # 成功码却没给 holdId：号可能已经扣了，绝不能当"预占失败"
            self._metric("malformed_response_total")
            raise HisUncertainError(
                MSG_UNCERTAIN, detail="HIS 返回成功但缺少 holdId，预占结果待核实"
            )
        slot = self._to_slot(data) if (
            data.get("clinicForRegistId") or data.get("clinic_for_regist_id")
        ) else None
        return HisHold(
            held=True,
            hold_id=hold_id,
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
        self, payload: Dict[str, Any], *, idempotency_key: str
    ) -> HisBooking:
        """确认预占并落单（对应 ``POST /currentRegister/add``）。"""
        response = self.request(
            "POST", self._confirm_path(), body=payload,
            idempotency_key=idempotency_key,
        )
        data = response.data if isinstance(response.data, dict) else {}
        booking_id = str(
            data.get("clinicMasterId")
            or data.get("clinic_master_id")
            or data.get("bookingId")
            or data.get("registId")
            or ""
        )
        if not booking_id:
            # 落单成功却拿不到单号 → 之后无法对账、无法退号 → 必须 UNKNOWN
            self._metric("malformed_response_total")
            raise HisUncertainError(
                MSG_UNCERTAIN, detail="HIS 返回成功但缺少挂号单号，落单结果待核实"
            )
        return HisBooking(
            confirmed=True, external_booking_id=booking_id, message=response.msg
        )

    def release_hold(self, hold_id: str, *, idempotency_key: str) -> bool:
        """释放预占（把号源还回去）。"""
        self.request(
            "POST", self._release_path(), body={"holdId": hold_id},
            idempotency_key=idempotency_key,
        )
        return True

    def _query_booking_ref(self, route: str, value: str) -> HisBookingStatus:
        """按外部单据号 / 预占号 / 幂等键查单（对账依据）。"""
        safe = urllib.parse.quote(value, safe="")
        # 端点前缀可配：接真 HIS 时按院方路由覆盖 booking / byKey 即可
        base = self.path("byKey") if route else (self.path("booking") or "/currentRegister")
        if route and not base:
            base = "/currentRegister/byKey"
        path = f"{base}/{safe}"
        try:
            response = self.request("GET", path)
        except HisRejectedError as exc:
            if exc.code == 404:
                return HisBookingStatus(found=False, external_booking_id=value)
            raise
        data = response.data if isinstance(response.data, dict) else {}
        return HisBookingStatus(
            found=True,
            external_booking_id=str(
                data.get("clinicMasterId")
                or data.get("clinic_master_id")
                or data.get("bookingId")
                or value
            ),
            status=str(data.get("status") or data.get("registStatus") or ""),
            message=response.msg,
        )

    def query_booking(self, external_booking_id: str) -> HisBookingStatus:
        """查单 —— 404 / 空 = HIS 确实没有这条单，可把 UNKNOWN 收敛为失败。"""
        return self._query_booking_ref("", external_booking_id)

    def query_booking_by_idempotency_key(self, key: str) -> HisBookingStatus:
        """按幂等键查单 —— 解开「落单超时且没拿到单号」的对账死结。

        预约 UNKNOWN 且没有外部单号时，可凭该业务动作的幂等键问 HIS：
        "这个动作到底落单了没有？" 有 → 收敛；明确没有 → 收敛为失败；
        查单本身不确定 → 保持 UNKNOWN（不猜）。
        """
        return self._query_booking_ref("byKey", key)

    def cancel_booking(
        self, external_booking_id: str, *, idempotency_key: str = ""
    ) -> bool:
        """退号（用户取消预约）。

        退号失败**必须让上层知道**：本地说"取消成功"而医院还留着号，
        用户到院发现没退 —— 这是比"退不掉"更糟的结果。
        """
        self.request(
            "POST", self._cancel_path(),
            body={"clinicMasterId": external_booking_id},
            idempotency_key=idempotency_key or None,
        )
        return True

    # ---------- 端点路径（可配置） ----------

    def path(self, key: str) -> str:
        """取端点路径（集中映射，接真 HIS 只改这里）。"""
        return self._paths.get(key, "")

    def _hold_path(self) -> str:
        return self.path("hold") or "/clinicForRegist/hold"

    def _confirm_path(self) -> str:
        return self.path("confirm") or "/currentRegister/add"

    def _release_path(self) -> str:
        return self.path("release") or "/clinicForRegist/release"

    def _cancel_path(self) -> str:
        return self.path("cancel") or "/currentRegister/cancel"

    def configure_paths(self, **paths: str) -> None:
        """覆盖端点路径。

        可用键：``slots / slot / hold / confirm / release / cancel / booking /
        byKey / bookings / patient / patients / depts / dept / doctors``。
        院方路由与参考项目不一致时，在组合根按配置注入即可，**不要**改
        工具层或业务层。
        """
        self._paths.update({k: v for k, v in paths.items() if v})

    # ---------- 字段映射 ----------

    @staticmethod
    def _to_slot(row: Dict[str, Any]) -> HisSlot:
        """HIS 行 → ``HisSlot``（兼容驼峰 / 下划线 / 字段缺失）。"""

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
