"""失败兜底：指数退避重试 + 熔断器 + 本地规则降级应答。

对应 PLAN.md「3.4.4 失败兜底」：任何单点故障都不让用户看到 500。
全部是纯 Python，不依赖外部服务，可离线单测。

设计要点：
* 重试只针对**尚未向用户吐出任何文本**的失败（空响应 / 连接错误），
  否则重试会造成前端出现重复文字 —— 这由 engine 决定何时调用；
* 熔断器按组件隔离（模型一个、每个工具一个），避免一个工具拖垮整轮对话；
* 规则应答只给分导诊建议，**绝不声称完成了预约**。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import random
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetryConfig:
    """指数退避重试配置"""

    max_attempts: int = 3          # 含首次，共尝试 max_attempts 次
    base_delay: float = 0.1        # 第一次重试前的等待（秒）
    max_delay: float = 2.0
    backoff_factor: float = 2.0
    jitter: float = 0.05

    def delay_for(self, attempt: int) -> float:
        """第 attempt 次重试（从 1 开始）前的等待时长"""
        delay = self.base_delay * (self.backoff_factor ** max(0, attempt - 1))
        delay = min(delay, self.max_delay)
        if self.jitter:
            delay += random.uniform(0, self.jitter)
        return round(delay, 4)


async def retry_async(
    operation: Callable[[], Awaitable[Any]],
    config: Optional[RetryConfig] = None,
    *,
    should_retry: Optional[Callable[[Exception], bool]] = None,
    on_retry: Optional[Callable[[int, Exception, float], Any]] = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> Any:
    """执行异步操作，失败按指数退避重试。

    ``on_retry`` 可以是同步或异步函数，签名 ``(attempt, exc, delay)``。
    """
    cfg = config or RetryConfig()
    last_exc: Optional[Exception] = None

    for attempt in range(1, max(1, cfg.max_attempts) + 1):
        try:
            return await operation()
        except Exception as exc:
            last_exc = exc
            if should_retry is not None and not should_retry(exc):
                raise
            if attempt >= cfg.max_attempts:
                break
            delay = cfg.delay_for(attempt)
            if on_retry is not None:
                result = on_retry(attempt, exc, delay)
                if inspect.isawaitable(result):
                    await result
            logger.warning(
                "重试 %s/%s：%s: %s，等待 %.3fs",
                attempt,
                cfg.max_attempts,
                type(exc).__name__,
                exc,
                delay,
            )
            await sleep(delay)

    assert last_exc is not None
    raise last_exc


class CircuitState(str, Enum):
    CLOSED = "closed"        # 正常
    OPEN = "open"            # 熔断，直接降级
    HALF_OPEN = "half_open"  # 试探恢复


class CircuitBreaker:
    """三态熔断器（线程安全，按组件各建一个实例）"""

    def __init__(
        self,
        name: str = "default",
        failure_threshold: int = 3,
        recovery_timeout: float = 30.0,
        half_open_max_calls: int = 1,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self.failure_threshold = max(1, failure_threshold)
        self.recovery_timeout = max(0.0, recovery_timeout)
        self.half_open_max_calls = max(1, half_open_max_calls)
        self._clock = clock
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._opened_at: Optional[float] = None
        self._half_open_calls = 0
        self._successes = 0
        self._failures_total = 0

    @property
    def state(self) -> CircuitState:
        self._refresh()
        return self._state

    def _refresh(self) -> None:
        if self._state == CircuitState.OPEN and self._opened_at is not None:
            if self._clock() - self._opened_at >= self.recovery_timeout:
                self._state = CircuitState.HALF_OPEN
                self._half_open_calls = 0

    def allow(self) -> bool:
        """是否放行本次调用"""
        self._refresh()
        if self._state == CircuitState.OPEN:
            return False
        if self._state == CircuitState.HALF_OPEN:
            if self._half_open_calls >= self.half_open_max_calls:
                return False
            self._half_open_calls += 1
        return True

    def record_success(self) -> None:
        self._successes += 1
        self._failures = 0
        self._state = CircuitState.CLOSED
        self._opened_at = None
        self._half_open_calls = 0

    def record_failure(self) -> None:
        self._failures += 1
        self._failures_total += 1
        if self._state == CircuitState.HALF_OPEN:
            self._trip()
            return
        if self._failures >= self.failure_threshold:
            self._trip()

    def _trip(self) -> None:
        self._state = CircuitState.OPEN
        self._opened_at = self._clock()
        self._half_open_calls = 0
        logger.warning("熔断器 %s 打开，%s 秒内直接降级", self.name, self.recovery_timeout)

    def reset(self) -> None:
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._opened_at = None
        self._half_open_calls = 0

    def snapshot(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state.value,
            "consecutive_failures": self._failures,
            "total_failures": self._failures_total,
            "successes": self._successes,
        }


# 本地规则分诊表 —— 只在模型完全不可用时给方向性建议，不编造号源/预约结果
_TRIAGE_RULES: Tuple[Tuple[Tuple[str, ...], str, str], ...] = (
    (("头痛", "头晕", "偏头痛", "眩晕", "肢体麻木"), "神经内科", "避免自行服用止痛药掩盖症状"),
    (("胸闷", "心悸", "胸痛", "心慌"), "心内科", "胸痛持续不缓解请立即急诊"),
    (("咳嗽", "气喘", "呼吸困难", "发热"), "呼吸内科", "注意记录体温与持续时间"),
    (("胃痛", "腹痛", "腹泻", "反酸"), "消化内科", "就诊前尽量空腹，便于检查"),
    (("皮疹", "瘙痒", "过敏"), "皮肤科", "就诊前避免涂抹药物，便于医生观察"),
    (("失眠", "焦虑", "情绪低落"), "心理科", "保持规律作息，记录睡眠情况"),
    (("挂号", "预约", "号源", "取消预约"), "门诊服务台", "可通过对话继续完成挂号流程"),
    (("地址", "在哪", "怎么走", "电话"), "门诊服务台", "医院信息可在知识库或院方官网查询"),
)

BASE_FALLBACK = (
    "抱歉，智能服务暂时不可用，请稍后重试。\n"
    "如需立即就诊，请直接前往医院门诊服务台或急诊分诊处。"
)


def rule_based_reply(query: str) -> Optional[str]:
    """按关键词给出确定性分导诊建议；匹配不到返回 None"""
    text = query or ""
    for keywords, department, advice in _TRIAGE_RULES:
        if any(keyword in text for keyword in keywords):
            return (
                f"智能服务暂时不可用，先给您一个方向性建议："
                f"建议优先咨询【{department}】。{advice}。\n"
                "（本建议由本地规则给出，不构成诊断结论；服务恢复后可继续对话。）"
            )
    return None


def degraded_reply(query: str) -> str:
    """模型故障时的最终兜底文案（绝不声称预约成功）"""
    return rule_based_reply(query) or BASE_FALLBACK
