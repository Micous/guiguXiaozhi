"""HIS 调用韧性层 —— 超时 / 网络波动 / 报错的**唯一裁决点**。

**为什么单独一层**

``his_client.py`` 原本只区分"确定失败 / 不确定"，但真实链路上远不止两种：

.. code-block:: text

    DNS 解析失败 / 连接被拒绝   → 请求根本没发出去 → **确定没执行**
    HTTP 429 / 熔断器打开       → 请求没被处理     → **确定没执行**
    读超时 / 连接中途断开 / 5xx → 可能已经扣号落单 → **结果不确定**
    HTTP 200 + code=500 系统忙  → 医院网关把内部错包成业务码 → **不确定**
    HTTP 200 + 成功码但缺单号   → 协议违规           → **不确定**
    HTTP 200 + code=404/号源已满→ 明确业务拒绝       → **确定失败**

把"确定没执行"误报成 UNKNOWN 会让用户白等一轮对账；把"结果不确定"
误报成明确失败会让用户重试 → **重复挂号**（最危险）。所以分类必须先于重试。

**网络波动怎么收敛**

一次瞬断不该直接把预约推到 UNKNOWN。本层提供：

* 有限重试（指数退避 + 抖动），只对「可重试」的失败；
* 写操作**只有带幂等键时才重试** —— 这时重试等价于「用同一幂等键重放查证」，
  HIS 幂等的话会直接返回首次结果，把 UNKNOWN 收敛成确定结果；
  没有幂等键的写操作**绝不重试**（否则重复扣号源）；
* **总时限预算**：socket timeout 只管单次 recv，服务器慢慢吐字节可以无限拖，
  所以另设整体 deadline，读取循环里逐步检查；
* **熔断器**：HIS 宕机时不能让每个用户请求都等满超时（线程池打满 → 雪崩），
  连续基础设施失败达阈值就快速失败，半开只放一个探测请求。

**业务拒绝不进熔断器**：号源已满、患者不存在是正常业务结果，不是 HIS 故障。

**对外话术与日志详情分离**：HIS 的错误正文可能夹带患者身份证号、姓名、
甚至 token。所有异常的用户可见消息都用固定中性话术，脱敏后的原文只进
``detail``（日志用）—— 否则 ``str(exc)`` 会经工具结果 → 模型 → 用户 + 历史落库。
"""

from __future__ import annotations

import logging
import random
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, FrozenSet, Optional

from xiaozhi.security.masking import mask_pii_in_text

logger = logging.getLogger(__name__)

# ---------- 失败分类 ----------

#: 请求确定没送达 / 没被处理 → 明确"暂时不可用"，可以重试，不制造 UNKNOWN
KIND_NOT_SENT = "not_sent"
#: 请求可能已被执行 → 结果不确定，必须 UNKNOWN + 对账
KIND_UNCERTAIN = "uncertain"
#: HIS 明确拒绝 → 可如实告知用户
KIND_REJECTED = "rejected"
#: 认证/授权失败 → 不重试，需要运维换 token
KIND_AUTH = "auth"

#: 用户可见的固定话术（HIS 原始错误绝不直接回给用户）
MSG_UNAVAILABLE = "医院信息系统暂时无法连接，请稍后再试。"
MSG_UNCERTAIN = "医院系统处理结果暂未确认，请勿重复提交，我们会尽快核实。"
MSG_AUTH = "医院系统认证失败，请联系管理员处理。"
MSG_RATE_LIMITED = "医院系统繁忙，请稍后再试。"
MSG_PROTOCOL = "医院系统响应异常，处理结果待核实，请勿重复提交。"
MSG_CIRCUIT_OPEN = "医院信息系统暂时不可用，请稍后再试。"

#: 业务码中表示「HIS 内部故障」的码。医院实现差异大，可由配置覆盖。
#: 注意 500 也在表里：很多院方网关把内部异常统一包成 ``{code:500}``，
#: 这种响应绝不能当"明确失败"（可能已经落单）。
DEFAULT_UNCERTAIN_BUSINESS_CODES: FrozenSet[int] = frozenset(
    {408, 425, 429, 500, 502, 503, 504, 522, 524, 598, 599}
)

#: 命中即认定「HIS 内部故障/超时」—— 即使业务码看起来是普通 4xx
UNCERTAIN_MESSAGE_MARKERS = (
    "超时", "time out", "timeout", "timed out", "系统忙", "系统繁忙",
    "系统异常", "系统错误", "内部错误", "内部异常", "数据库", "连接失败",
    "网关", "服务不可用", "未知异常", "处理中", "请重试",
    "unavailable", "gateway", "internal error", "busy", "try again",
)

#: 命中即认定「明确业务拒绝」—— **优先级高于系统码**，否则
#: ``{code:500, msg:"号源已满"}``（院方常见包法）会被误判成结果不确定。
CLEAR_REJECTION_MARKERS = (
    "号源已满", "已约满", "约满", "满号", "无号", "停诊", "不出诊",
    # 2026-11-12 真机事故：`暂无出诊安排` 一条都没命中，
    # 于是一个**明确的业务拒绝**被归类成"结果不确定"（因为院方把
    # 无排班包成 code=500）→ 客户端白重试 2 次、落 UNKNOWN、
    # 用户被告知"请勿重复提交"。下面这组是"这天这个时段没医生出诊"的
    # 明确说法，语义上等价于"没号"，必须归 REJECTED。
    "暂无出诊", "无排班", "没有排班", "未排班", "暂无号源",
    "无可预约", "无可用号源", "不在出诊",
    "患者不存在", "病人不存在", "预占不存在", "不存在", "未找到",
    "参数", "无效", "非法", "拒绝", "重复", "过期", "已退号", "已取消",
    "已释放", "未登录", "令牌", "无权限", "不允许", "超出", "不能超过",
    "余额不足", "号表不存在", "排班不存在", "业务失败",
    "not found", "invalid", "rejected",
)

#: 认证类业务码
AUTH_BUSINESS_CODES: FrozenSet[int] = frozenset({401, 403})

_HTML_TAG_RE = re.compile(r"<[^>]{1,200}>")
_WS_RE = re.compile(r"\s+")
_SECRET_RE = re.compile(
    r"(?i)\b(token|satoken|authorization|access_token|secret|password|pwd)"
    r"(\s*[:=]\s*)([^\s,;\"'&]{1,120})"
)


@dataclass(frozen=True)
class Decision:
    """一次失败的分类结论。"""

    kind: str
    retryable: bool
    message: str
    code: Optional[int] = None
    detail: str = ""

    @property
    def infra_failure(self) -> bool:
        """是否属于「基础设施故障」（计入熔断）。"""
        return self.kind in (KIND_NOT_SENT, KIND_UNCERTAIN)


def redact_detail(raw: str, limit: int = 200) -> str:
    """HIS 错误正文 → 可安全写日志的脱敏摘要。

    三件事：去 HTML 标签（院方网关常返回整页错误）、打码身份证/手机号、
    打码 token 类凭据、压缩空白并截断。``mask_pii_in_text`` 是幂等的。
    """
    if not raw:
        return ""
    text = _HTML_TAG_RE.sub(" ", str(raw))
    text = _SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", text)
    text = mask_pii_in_text(text)
    text = _WS_RE.sub(" ", text).strip()
    return text[:limit]


def classify_http_status(status: int, detail: str = "") -> Decision:
    """HTTP 状态码 → 分类（**客户端已禁用自动重定向**，3xx 也会走到这里）。"""
    safe = redact_detail(detail)
    if status in (401, 403):
        return Decision(KIND_AUTH, False, MSG_AUTH, status, safe)
    if status == 429:
        # 限流：请求没被业务处理 → 明确未执行，可退避重试
        return Decision(KIND_NOT_SENT, True, MSG_RATE_LIMITED, status, safe)
    if status in (408, 409, 425):
        # 408 请求超时 / 409 幂等冲突：HIS 可能已经处理过 → 不确定
        return Decision(KIND_UNCERTAIN, True, MSG_UNCERTAIN, status, safe)
    if status >= 500:
        return Decision(KIND_UNCERTAIN, True, MSG_UNCERTAIN, status, safe)
    if 300 <= status < 400:
        # 不跟随重定向：避免 POST 被改成 GET、token 被转发到第三方
        return Decision(
            KIND_REJECTED, False,
            "医院系统返回了重定向，请检查接口地址配置。", status, safe,
        )
    return Decision(
        KIND_REJECTED, False,
        "医院系统拒绝了本次请求，请稍后再试。", status, safe,
    )


def classify_business_code(
    code: int,
    msg: str,
    *,
    uncertain_codes: FrozenSet[int] = DEFAULT_UNCERTAIN_BUSINESS_CODES,
) -> Decision:
    """统一响应包 ``R{code,msg,data}`` 里的业务码 → 分类。

    判定顺序**不可调换**：

    1. 认证码 → AUTH；
    2. 明确业务拒绝关键词 → REJECTED（优先于系统码！院方常把
       ``号源已满`` 包成 ``code=500``）；
    3. 系统故障关键词 → UNCERTAIN；
    4. 系统类业务码 → UNCERTAIN；
    5. 其余 → REJECTED。
    """
    safe = redact_detail(msg)
    lowered = (msg or "").lower()
    if code in AUTH_BUSINESS_CODES:
        return Decision(KIND_AUTH, False, MSG_AUTH, code, safe)
    if any(marker in lowered for marker in CLEAR_REJECTION_MARKERS):
        # 业务拒绝的原因本身可以给用户看（已脱敏），不要换成含糊话术
        return Decision(KIND_REJECTED, False, safe or "医院系统拒绝了本次请求。", code, safe)
    if any(marker in lowered for marker in UNCERTAIN_MESSAGE_MARKERS):
        return Decision(KIND_UNCERTAIN, True, MSG_UNCERTAIN, code, safe)
    if code in uncertain_codes:
        return Decision(KIND_UNCERTAIN, True, MSG_UNCERTAIN, code, safe)
    return Decision(KIND_REJECTED, False, safe or "医院系统拒绝了本次请求。", code, safe)


def decision_from_exception(exc: Exception) -> Decision:
    """把 ``HisError`` 子类映射回分类结论（供重试/熔断使用）。

    这里用类名而不是 import，避免 ``his_resilience`` 反向依赖 ``his_client``。
    """
    name = type(exc).__name__
    code = getattr(exc, "code", None)
    detail = getattr(exc, "detail", "") or ""
    message = str(exc)
    if name in ("HisUnavailableError", "HisCircuitOpenError"):
        return Decision(KIND_NOT_SENT, True, message, code, detail)
    if name == "HisUncertainError":
        return Decision(KIND_UNCERTAIN, True, message, code, detail)
    if name == "HisAuthError":
        return Decision(KIND_AUTH, False, message, code, detail)
    return Decision(KIND_REJECTED, False, message, code, detail)


# ---------- 重试 ----------


@dataclass
class RetryPolicy:
    """有限重试策略（总尝试次数含首次）。"""

    max_attempts: int = 2
    base_delay: float = 0.15
    max_delay: float = 1.0
    jitter_ratio: float = 0.2
    #: 写操作只有在**带幂等键**时才允许重试（重试 = 同键重放查证）
    retry_writes: bool = True
    #: 整体时限硬上限；0 = 按 ``timeout * max_attempts + 1s`` 自动推导
    total_deadline: float = 0.0

    def __post_init__(self) -> None:
        self.max_attempts = max(1, int(self.max_attempts))
        self.base_delay = max(0.0, float(self.base_delay))
        self.max_delay = max(self.base_delay, float(self.max_delay))
        self.jitter_ratio = min(1.0, max(0.0, float(self.jitter_ratio)))
        self.total_deadline = max(0.0, float(self.total_deadline))

    def budget(self, timeout: float) -> float:
        """整次调用（含所有重试与退避）的时间预算。"""
        return self.total_deadline or (max(0.01, timeout) * self.max_attempts + 1.0)

    def backoff(self, attempt: int, rng: Any = random) -> float:
        """指数退避 + 抖动（避免故障恢复瞬间的重试风暴打垮 HIS）。"""
        raw = min(self.max_delay, self.base_delay * (2 ** max(0, attempt - 1)))
        return raw * (1.0 + rng.uniform(-self.jitter_ratio, self.jitter_ratio))

    def should_retry(
        self,
        decision: Decision,
        *,
        method: str,
        idempotency_key: str,
        attempt: int,
        remaining: float,
        next_delay: float,
    ) -> bool:
        """是否值得再试一次。

        **写操作的红线**：超时/5xx 时请求可能已在 HIS 侧生效，只有带幂等键
        才能安全重放；裸写重试会造成重复挂号，一律禁止。
        """
        if not decision.retryable or attempt >= self.max_attempts:
            return False
        if remaining <= next_delay:
            return False
        is_read = method.upper() in ("GET", "HEAD")
        if is_read:
            return True
        if not self.retry_writes:
            return False
        return bool(idempotency_key)


# ---------- 熔断 ----------


class CircuitBreaker:
    """HIS 故障熔断器（CLOSED → OPEN → HALF_OPEN → CLOSED）。

    * 只有**基础设施失败**（未送达 / 不确定 / 5xx / 超时）计入连续失败数；
      业务拒绝（号满、患者不存在）不算故障，不触发熔断；
    * OPEN 期间直接快速失败，请求根本不发出 → 明确"暂时不可用"，
      **不制造 UNKNOWN**（否则 HIS 一挂就产生一堆永远收敛不了的待核实单）；
    * 半开只放行一个探测请求，成功即闭合，失败立即重新打开。
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        recovery_timeout: float = 20.0,
        clock: Any = time.monotonic,
    ) -> None:
        self._threshold = max(1, int(failure_threshold))
        self._recovery = max(0.0, float(recovery_timeout))
        self._clock = clock
        self._lock = threading.Lock()
        self._state = self.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._probe_in_flight = False

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def allow(self) -> bool:
        """是否放行本次请求。"""
        with self._lock:
            if self._state == self.CLOSED:
                return True
            now = self._clock()
            if self._state == self.OPEN:
                if now - self._opened_at < self._recovery:
                    return False
                self._state = self.HALF_OPEN
                self._probe_in_flight = True
                logger.info("HIS 熔断器进入半开，放行一个探测请求")
                return True
            # HALF_OPEN：只允许那一个探测请求，其余继续快速失败
            if self._probe_in_flight:
                return False
            return False

    def record_success(self) -> None:
        with self._lock:
            if self._state != self.CLOSED:
                logger.info("HIS 熔断器闭合（探测成功）")
            self._state = self.CLOSED
            self._failures = 0
            self._probe_in_flight = False

    def record_failure(self, *, infra: bool) -> None:
        """``infra=False``（业务拒绝）不改变熔断状态。"""
        if not infra:
            return
        with self._lock:
            if self._state == self.HALF_OPEN:
                self._open()
                logger.warning("HIS 半开探测失败，熔断器重新打开")
                return
            self._failures += 1
            if self._failures >= self._threshold:
                self._open()

    def _open(self) -> None:
        self._state = self.OPEN
        self._opened_at = self._clock()
        self._probe_in_flight = False
        self._failures = self._threshold
        logger.warning(
            "HIS 连续 %d 次基础设施失败，熔断器打开（%.1fs 后半开）",
            self._failures, self._recovery,
        )

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "state": self._state,
                "consecutive_failures": self._failures,
                "failure_threshold": self._threshold,
            }
