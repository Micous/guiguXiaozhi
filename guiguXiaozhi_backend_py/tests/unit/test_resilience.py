"""失败兜底单测：重试退避、熔断状态机、本地规则降级。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from xiaozhi.reliability.resilience import (  # noqa: E402
    CircuitBreaker,
    CircuitState,
    RetryConfig,
    degraded_reply,
    retry_async,
    rule_based_reply,
)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


async def test_retry_succeeds_on_second_attempt():
    attempts = {"n": 0}
    delays = []

    async def operation():
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise RuntimeError("临时失败")
        return "ok"

    async def fake_sleep(delay):
        delays.append(delay)

    result = await retry_async(
        operation,
        RetryConfig(max_attempts=3, base_delay=0.1, jitter=0),
        sleep=fake_sleep,
    )
    assert result == "ok"
    assert attempts["n"] == 2
    assert delays == [0.1]


async def test_retry_exhausts_attempts_with_exponential_backoff():
    attempts = {"n": 0}
    delays = []

    async def operation():
        attempts["n"] += 1
        raise RuntimeError("一直失败")

    async def fake_sleep(delay):
        delays.append(delay)

    with pytest.raises(RuntimeError, match="一直失败"):
        await retry_async(
            operation,
            RetryConfig(max_attempts=4, base_delay=0.2, jitter=0),
            sleep=fake_sleep,
        )
    assert attempts["n"] == 4
    assert delays == [0.2, 0.4, 0.8]


async def test_should_retry_can_stop_immediately():
    attempts = {"n": 0}

    async def operation():
        attempts["n"] += 1
        raise ValueError("不可重试")

    with pytest.raises(ValueError):
        await retry_async(operation, RetryConfig(max_attempts=3), should_retry=lambda e: False)
    assert attempts["n"] == 1


def test_circuit_breaker_state_transitions():
    clock = Clock()
    breaker = CircuitBreaker(
        "ollama", failure_threshold=2, recovery_timeout=10.0, clock=clock
    )
    assert breaker.state == CircuitState.CLOSED
    assert breaker.allow()

    breaker.record_failure()
    assert breaker.state == CircuitState.CLOSED
    breaker.record_failure()
    assert breaker.state == CircuitState.OPEN
    assert not breaker.allow()

    clock.advance(11.0)
    assert breaker.state == CircuitState.HALF_OPEN
    assert breaker.allow()
    assert not breaker.allow()  # 半开态只放一次试探
    breaker.record_success()
    assert breaker.state == CircuitState.CLOSED


def test_half_open_failure_reopens_circuit():
    clock = Clock()
    breaker = CircuitBreaker(
        "tool", failure_threshold=1, recovery_timeout=5.0, clock=clock
    )
    breaker.record_failure()
    assert breaker.state == CircuitState.OPEN
    clock.advance(6.0)
    assert breaker.allow()
    breaker.record_failure()
    assert breaker.state == CircuitState.OPEN
    assert breaker.snapshot()["total_failures"] == 2


def test_rule_based_reply_gives_department_without_fake_booking():
    reply = rule_based_reply("我最近总是头痛")
    assert reply is not None
    assert "神经内科" in reply
    assert "预约成功" not in reply
    assert "本地规则" in reply


def test_degraded_reply_falls_back_for_unknown_query():
    reply = degraded_reply("随便聊聊")
    assert "抱歉" in reply
    assert "门诊服务台" in reply
    assert rule_based_reply("随便聊聊") is None
