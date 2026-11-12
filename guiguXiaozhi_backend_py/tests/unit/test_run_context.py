"""B1 护栏：请求级 RunContext / 统一 deadline / 取消与 finally 收尾。

全部用 fake，不依赖 Ollama / Milvus / MySQL / MongoDB。
重点钉住两件事：
1. 一次 Run 只有一份 deadline，各阶段读同一份剩余预算；
2. 生命周期壳不改变正常路径的事件序列，异常与取消仍原样传播且上下文被清理。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from xiaozhi.core.event import EventType  # noqa: E402
from xiaozhi.core.tool import ToolResult  # noqa: E402
from xiaozhi.models.ollama import ChatTurn  # noqa: E402
from xiaozhi.ops.metrics import MetricsCollector  # noqa: E402
from xiaozhi.reliability.tracing import get_trace_id, set_trace_id  # noqa: E402
from xiaozhi.runtime.engine import AgentEngine  # noqa: E402
from xiaozhi.runtime.run_context import (  # noqa: E402
    CancellationScope,
    ExecutionBudget,
    RunContext,
)


# ---------------------------------------------------------------- RunContext


def test_run_context_creates_unique_identity_per_run():
    a = RunContext.create(trace_id="t-1", session_id="session-1", subject_id="alice")
    b = RunContext.create(trace_id="t-1", session_id="session-1", subject_id="alice")

    assert a.run_id.startswith("run-") and a.request_id.startswith("req-")
    assert a.run_id != b.run_id and a.request_id != b.request_id
    assert a.trace_id == "t-1" and a.session_id == "session-1"
    assert a.subject_id == "alice"


def test_deadline_is_one_absolute_moment_created_at_entry():
    ctx = RunContext.create(
        trace_id="t", session_id="s", timeout_seconds=10.0, now=100.0
    )
    assert ctx.started_monotonic == 100.0
    assert ctx.deadline_monotonic == 110.0
    assert ctx.timeout_seconds == 10.0


def test_budget_never_restarts_per_phase():
    """各阶段拿到的都是同一份 deadline 的剩余，不会各自重开计时。"""
    ctx = RunContext.create(
        trace_id="t", session_id="s", timeout_seconds=30.0, now=1000.0
    )
    budget = ExecutionBudget(ctx)

    # 用固定时钟替换 time.monotonic，模拟时间流逝（不真的 sleep）
    import xiaozhi.runtime.run_context as rc

    original = rc.time.monotonic
    rc.time.monotonic = lambda: 1005.0
    try:
        assert ctx.elapsed_seconds == pytest.approx(5.0)
        values = [
            budget.remaining_for("retrieval"),
            budget.remaining_for("model"),
            budget.remaining_for("tool"),
            budget.remaining_for("persistence"),
        ]
        assert all(v == pytest.approx(25.0) for v in values)
        assert len(set(values)) == 1
        assert budget.time_exceeded is False
    finally:
        rc.time.monotonic = original


def test_expired_context_and_no_timeout_context():
    import xiaozhi.runtime.run_context as rc

    expired = RunContext.create(
        trace_id="t", session_id="s", timeout_seconds=1.0, now=100.0
    )
    original = rc.time.monotonic
    rc.time.monotonic = lambda: 102.0
    try:
        assert expired.expired is True
        assert expired.remaining_seconds == 0.0
        assert ExecutionBudget(expired).time_exceeded is True
    finally:
        rc.time.monotonic = original

    unlimited = RunContext.create(trace_id="t", session_id="s", timeout_seconds=None)
    assert ExecutionBudget(unlimited).time_exceeded is False
    assert ExecutionBudget(unlimited).remaining_for("tool") == float("inf")
    assert unlimited.as_span_attributes()["deadline_seconds"] is None


def test_invalid_timeout_is_rejected():
    with pytest.raises(ValueError):
        RunContext.create(trace_id="t", session_id="s", timeout_seconds=-1)


def test_cancellation_is_idempotent_and_keeps_first_reason():
    scope = CancellationScope()
    assert scope.cancelled is False
    scope.cancel("client_cancelled")
    scope.cancel("generator_closed")
    assert scope.cancelled is True
    assert scope.reason == "client_cancelled"
    assert scope.cancelled_at is not None
    assert scope.snapshot() == {
        "cancelled": True,
        "reason": "client_cancelled",
    }


# ---------------------------------------------------------------- 引擎级 fake


class FakeChatModel:
    model = "fake-model"

    def __init__(self, answer="回答内容", error=None):
        self.answer = answer
        self.error = error
        self.calls = 0

    async def stream_chat(self, messages, tools=None, result_holder=None):
        self.calls += 1
        if self.error is not None:
            raise self.error
        yield self.answer
        if result_holder is not None:
            result_holder["turn"] = ChatTurn(text=self.answer)


class FakeAssembled:
    history = []
    dropped_items = 0
    total_tokens = 0

    def to_messages(self, user_input, *, neutralize_history: bool = False):
        return [{"role": "user", "content": user_input}]


class FakeAssembler:
    def assemble(self, *args, **kwargs):
        return FakeAssembled()


class FakeStore:
    def __init__(self, error=None):
        self.error = error
        self.saved = None

    async def load(self, memory_id, limit=20):
        if self.error is not None:
            raise self.error
        return []

    async def save(self, memory_id, messages):
        self.saved = messages

    async def close(self):
        return None


class FakeRegistry:
    def list_definitions(self):
        return []

    def get(self, name):
        return object()


class FakeExecutor:
    async def execute(self, call, tool):
        return ToolResult.ok("OK")


def make_engine(model=None, store=None):
    metrics = MetricsCollector()
    engine = AgentEngine(
        chat_model=model or FakeChatModel(),
        tool_registry=FakeRegistry(),
        secure_executor=FakeExecutor(),
        conversation_store=store or FakeStore(),
        context_assembler=FakeAssembler(),
        system_prompt="系统提示",
        metrics=metrics,
    )
    return engine, metrics


async def collect(engine, message="查询号源"):
    events = []
    async for event in engine.run("memory-1", message):
        events.append(event)
    return events


# ---------------------------------------------------------------- 引擎级护栏


async def test_normal_run_keeps_event_sequence_unchanged():
    """B1 是纯增量壳：事件顺序、AgentStart/AgentEnd 数量逐字不变。"""
    engine, metrics = make_engine()
    events = await collect(engine)

    assert events[0].event_type == EventType.AGENT_START
    assert events[-1].event_type == EventType.AGENT_END
    assert sum(e.event_type == EventType.AGENT_START for e in events) == 1
    assert sum(e.event_type == EventType.AGENT_END for e in events) == 1
    assert any(e.event_type == EventType.TEXT_RESPONSE for e in events)
    assert metrics.snapshot()["counters"]["run_finished_total"] == 1
    assert "run_generator_closed_total" not in metrics.snapshot()["counters"]


async def test_short_circuit_still_emits_exactly_one_agent_end():
    """问候快路径在 _run_body 内 return，外层 finally 不能重复发结束事件。"""
    engine, metrics = make_engine()
    events = await collect(engine, "你好")

    assert sum(e.event_type == EventType.AGENT_END for e in events) == 1
    assert metrics.snapshot()["counters"]["greeting_fast_path_total"] == 1
    assert metrics.snapshot()["counters"]["run_finished_total"] == 1


async def test_trace_context_does_not_leak_between_runs():
    """没有 HTTP 路由代为 set_trace_id 时，连续两次 run 也必须是两条独立链路。"""
    from xiaozhi.reliability import tracing

    tracing._trace_id_var.set(None)
    engine, _ = make_engine()
    first = await collect(engine)
    second = await collect(engine)
    assert first[0].trace_id != second[0].trace_id

    # 外层显式设置的上下文必须被精确恢复，不被请求清空
    set_trace_id("outer-trace")
    await collect(engine)
    assert tracing._trace_id_var.get() == "outer-trace"
    tracing._trace_id_var.set(None)


async def test_unhandled_exception_is_reraised_and_context_cleaned():
    boom = RuntimeError("存储炸了")
    engine, metrics = make_engine(store=FakeStore(error=boom))

    with pytest.raises(RuntimeError, match="存储炸了"):
        await collect(engine)

    counters = metrics.snapshot()["counters"]
    assert counters["run_unhandled_error_total"] == 1
    assert counters["run_finished_total"] == 1


async def test_cancellation_is_recorded_and_reraised():
    """CancelledError 是 BaseException，不能被 except Exception 吞掉。"""
    engine, metrics = make_engine(
        model=FakeChatModel(error=asyncio.CancelledError("client gone"))
    )

    with pytest.raises(asyncio.CancelledError):
        await collect(engine)

    counters = metrics.snapshot()["counters"]
    assert counters["run_cancelled_total"] == 1
    assert counters["run_finished_total"] == 1
    assert "run_unhandled_error_total" not in counters


async def test_generator_close_marks_cancellation_without_yielding():
    """客户端断开（aclose → GeneratorExit）：finally 必须能安全收尾。"""
    engine, metrics = make_engine()
    gen = engine.run("memory-1", "查询号源")
    first = await gen.__anext__()
    assert first.event_type == EventType.AGENT_START
    await gen.aclose()

    counters = metrics.snapshot()["counters"]
    assert counters["run_generator_closed_total"] == 1
    assert counters["run_finished_total"] == 1


async def test_concurrent_runs_do_not_cross_talk():
    """同一 engine 实例上并发两个请求，事件序列各自完整。"""
    engine, _ = make_engine()
    left, right = await asyncio.gather(collect(engine), collect(engine))

    for events in (left, right):
        assert events[0].event_type == EventType.AGENT_START
        assert events[-1].event_type == EventType.AGENT_END
        assert sum(e.event_type == EventType.AGENT_END for e in events) == 1
