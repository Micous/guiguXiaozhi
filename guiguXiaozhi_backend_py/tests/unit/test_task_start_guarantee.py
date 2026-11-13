"""B3-2 护栏：写操作 STARTED 强保证（D1/D2 决策落点）。

覆盖《主体Agent整改-B3-调研与分批实施方案》§3.2 故障注入矩阵：

1. 写工具 + ``try_tool_started`` FAILED → **拒绝**：不进入 Adapter、无副作用、
   ``ToolResultEvent.is_error=True``、``error_type=TaskStartUnavailable``、
   ``tool_finished`` 不被调用、``task_started_failed_total`` 与
   ``task_started_failed_write_total`` 计数；
2. 只读工具 + FAILED → **放行**（D2）：正常执行、记 ``task_started_failed_total``
   但不记 write 计数；
3. 灰度关闭（tracker=None，DISABLED）→ 写/只读事件序列均不变（D1=A）；
4. 同一轮混合：只读 + 写工具，两者 STARTED 都失败 → 只读执行、写被拒、循环继续；
5. ``tool_finished`` 落库失败 → 副作用已提交，不得假装回滚，工具结果照常返回；
6. ``require_started_for_writes=true`` 且未开启任务追踪 → 启动期即失败；
7. 写工具清单单一来源（``WRITE_TOOL_NAMES``）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from xiaozhi.config import Settings  # noqa: E402
from xiaozhi.core.message import ToolCall  # noqa: E402
from xiaozhi.core.tool import ToolResult  # noqa: E402
from xiaozhi.models.ollama import ChatTurn  # noqa: E402
from xiaozhi.ops.metrics import MetricsCollector  # noqa: E402
from xiaozhi.application import validate_task_tracking_settings  # noqa: E402
from xiaozhi.runtime.engine import AgentEngine, AgentEngineResultHolder  # noqa: E402
from xiaozhi.runtime.state import TurnState  # noqa: E402
from xiaozhi.runtime.task_tracker import (  # noqa: E402
    TaskTracker,
    ToolStartResult,
    ToolStartStatus,
)
from xiaozhi.runtime.write_tools import WRITE_TOOL_NAMES  # noqa: E402
from xiaozhi.repositories.task_repo import InMemoryTaskRepository  # noqa: E402


# ---------------------------------------------------------------- 引擎级 fake


class FakeChatModel:
    model = "fake-model"

    async def stream_chat(self, messages, tools=None, result_holder=None):
        yield ""


class FakeAssembler:
    def assemble(self, *args, **kwargs):
        return type("A", (), {"history": [], "dropped_items": 0, "total_tokens": 0})()

    def to_messages(self, user_input, *, neutralize: bool = False):
        return [{"role": "user", "content": user_input}]


class FakeStore:
    async def load(self, memory_id, limit=20):
        return []

    async def save(self, memory_id, messages):
        return None

    async def close(self):
        return None


class FakeRegistry:
    def get(self, name):
        return object()

    def list_definitions(self):
        return []


class RecordingExecutor:
    def __init__(self, result=None):
        self.result = result or ToolResult.ok("OK")
        self.calls: List[Dict[str, Any]] = []

    async def execute(self, call, tool):
        self.calls.append(dict(call.args))
        return self.result


def make_engine(**kwargs):
    metrics = MetricsCollector()
    engine = AgentEngine(
        chat_model=FakeChatModel(),
        tool_registry=FakeRegistry(),
        secure_executor=RecordingExecutor(),
        conversation_store=FakeStore(),
        context_assembler=FakeAssembler(),
        system_prompt="系统提示",
        metrics=metrics,
        **kwargs,
    )
    return engine, metrics


def make_call(name="查询是否有号源", call_id="call-1", args=None):
    return ToolCall(
        id=call_id, name=name, arguments=dict(args or {"dept": "呼吸内科"})
    )


def make_response(*calls, text="我来处理"):
    return ChatTurn(text=text, tool_calls=list(calls))


def seq(events):
    return [e.event_type.value for e in events]


async def collect(engine, *, response, state=None):
    state = state or TurnState()
    result = AgentEngineResultHolder()
    gen = engine._tool_coordinator.execute_tools(
        response=response,
        messages=[],
        state=state,
        user_id="anonymous",
        trace_id="t-b3-2",
        result=result,
        metrics=engine.metrics,
        max_tool_calls=engine.max_tool_calls,
        tool_failure_threshold=engine.tool_failure_threshold,
        tool_registry=engine.tool_registry,
        task_tracker=engine.task_tracker,
        secure_executor=engine.secure_executor,
    )
    events = [e async for e in gen]
    return events, state, result


# ---------------------------------------------------------------- 失败注入桩


class FailedStartTracker:
    """``try_tool_started`` 永远 FAILED 的追踪器（关键记录不可写）。"""

    def __init__(self):
        self.finished_calls: List[Any] = []

    def try_tool_started(self, tool_call_id, tool_name, args):
        return ToolStartResult(ToolStartStatus.FAILED)

    def tool_finished(self, exec_id, tool_name, *, success):
        self.finished_calls.append((exec_id, tool_name, success))


class FinishedFailsRepo(InMemoryTaskRepository):
    """真实 TaskTracker + 收尾落库失败：验证「不假装回滚」是仓储级语义。"""

    def record_tool_finished(self, *args, **kwargs):
        raise RuntimeError("db down")


# ---------------------------------------------------------------- 1. 写工具拒绝


async def test_write_tool_is_rejected_when_start_fails():
    engine, metrics = make_engine()
    tracker = FailedStartTracker()
    engine.task_tracker = tracker

    events, state, _ = await collect(
        engine, response=make_response(make_call(name="预约挂号"))
    )

    # 不进入 Adapter、不产生副作用
    assert engine.secure_executor.calls == []
    assert seq(events) == ["tool_execute", "tool_result"]
    assert events[1].metadata["is_error"] is True
    assert "已阻止写操作" in events[1].metadata["result_preview"]
    # 结构化失败进入失败账本，供模型向用户解释
    assert state.failure_count("预约挂号") == 1
    assert state.last_tool_failure["error_type"] == "TaskStartUnavailable"
    # tool_finished 不被调用（没有 STARTED 记录可收尾）
    assert tracker.finished_calls == []
    counters = metrics.snapshot()["counters"]
    assert counters["task_started_failed_total"] == 1
    assert counters["task_started_failed_write_total"] == 1


async def test_read_only_tool_passes_when_start_fails():
    """D2：只读查询在 STARTED 落库失败时放行，但必须留痕。"""
    engine, metrics = make_engine()
    tracker = FailedStartTracker()
    engine.task_tracker = tracker

    events, state, _ = await collect(
        engine, response=make_response(make_call(name="查询是否有号源"))
    )

    assert engine.secure_executor.calls == [{"dept": "呼吸内科"}]
    assert seq(events) == ["tool_execute", "tool_result"]
    assert events[1].metadata["is_error"] is False
    assert state.failure_count("查询是否有号源") == 0
    # 只读：照常收尾，exec_id=None 时真实 TaskTracker 为 no-op（矩阵「保持 no-op」）
    assert tracker.finished_calls == [(None, "查询是否有号源", True)]
    counters = metrics.snapshot()["counters"]
    assert counters["task_started_failed_total"] == 1
    assert counters.get("task_started_failed_write_total", 0) == 0


async def test_disabled_tracker_keeps_event_sequence_unchanged():
    """D1=A：灰度关闭（tracker=None）时写/只读事件序列与关闭前完全一致。"""
    for name in ("预约挂号", "查询是否有号源"):
        engine, metrics = make_engine()
        engine.task_tracker = None

        events, _, _ = await collect(
            engine, response=make_response(make_call(name=name))
        )

        assert seq(events) == ["tool_execute", "tool_result"]
        assert len(engine.secure_executor.calls) == 1
        counters = metrics.snapshot()["counters"]
        assert counters.get("task_started_failed_total", 0) == 0


async def test_mixed_round_rejects_only_the_write_tool():
    """按单个 call 判定：同一轮里只读执行、写被拒、循环继续。"""
    engine, metrics = make_engine()
    tracker = FailedStartTracker()
    engine.task_tracker = tracker

    events, state, _ = await collect(
        engine,
        response=make_response(
            make_call(name="查询是否有号源", call_id="call-read"),
            make_call(name="预约挂号", call_id="call-write"),
        ),
    )

    assert seq(events) == [
        "tool_execute", "tool_result", "tool_execute", "tool_result",
    ]
    # 只读执行了一次，写从未进入 Adapter
    assert len(engine.secure_executor.calls) == 1
    assert state.failure_count("预约挂号") == 1
    assert state.failure_count("查询是否有号源") == 0
    counters = metrics.snapshot()["counters"]
    assert counters["task_started_failed_total"] == 2
    assert counters["task_started_failed_write_total"] == 1
    # 只读照常收尾（no-op），被拒的写工具不产生任何收尾调用
    assert tracker.finished_calls == [(None, "查询是否有号源", True)]


# ---------------------------------------------------------------- 2. 收尾不回滚


async def test_tool_finished_failure_does_not_mask_tool_result():
    """矩阵第 5 行：FINISHED 落库失败不得让已提交的副作用「看起来没发生」。

    用**真实 TaskTracker**（收尾异常在 tracker 内部吞掉并告警）驱动完整链路，
    断言工具结果照常返回、执行器确实被调用过。
    """
    engine, _ = make_engine()
    engine.task_tracker = TaskTracker(FinishedFailsRepo())

    events, state, _ = await collect(
        engine, response=make_response(make_call(name="预约挂号"))
    )

    assert engine.secure_executor.calls == [{"dept": "呼吸内科"}]  # 副作用已提交
    assert seq(events) == ["tool_execute", "tool_result"]
    assert events[1].metadata["is_error"] is False
    assert state.tool_call_count == 1


# ---------------------------------------------------------------- 3. 三态 API


def test_try_tool_started_three_states():
    """RECORDED / DISABLED / FAILED 三态可区分，旧入口保持向后兼容。"""
    # RECORDED
    repo = InMemoryTaskRepository()
    tracker = TaskTracker(repo)
    task_id, execution_id = tracker.begin(subject_id="alice", session_id="s1")
    assert task_id and execution_id
    recorded = tracker.try_tool_started("call-1", "预约挂号", {})
    assert recorded.status is ToolStartStatus.RECORDED
    assert recorded.tool_execution_id
    assert tracker.tool_started("call-1", "预约挂号", {})  # 旧入口 → id

    # DISABLED：未 begin（等价于灰度关闭 / 未装配）
    disabled_tracker = TaskTracker(InMemoryTaskRepository())
    disabled = disabled_tracker.try_tool_started("call-1", "预约挂号", {})
    assert disabled.status is ToolStartStatus.DISABLED
    assert disabled.tool_execution_id is None
    assert disabled_tracker.tool_started("call-1", "预约挂号", {}) is None

    # FAILED：仓储不可写
    class BrokenRepo(InMemoryTaskRepository):
        def record_tool_started(self, record):
            raise RuntimeError("db down")

    broken = TaskTracker(BrokenRepo())
    broken.begin(subject_id="alice", session_id="s1")
    failed = broken.try_tool_started("call-1", "预约挂号", {})
    assert failed.status is ToolStartStatus.FAILED
    assert failed.tool_execution_id is None
    assert broken.tool_started("call-1", "预约挂号", {}) is None


# ---------------------------------------------------------------- 4. 启动期校验


@pytest.mark.parametrize(
    "task_tracking_enabled,require_started,should_raise",
    [
        (False, False, False),  # 默认灰度关闭：允许启动 + WARNING
        (True, False, False),
        (True, True, False),  # 规则 8 全局成立的合法组合
        (False, True, True),  # 强保证无法生效 → 启动即失败
    ],
)
def test_require_started_for_writes_startup_validation(
    task_tracking_enabled, require_started, should_raise
):
    settings = Settings(
        task_tracking_enabled=task_tracking_enabled,
        require_started_for_writes=require_started,
    )
    if should_raise:
        with pytest.raises(RuntimeError, match="require_started_for_writes"):
            validate_task_tracking_settings(settings)
    else:
        validate_task_tracking_settings(settings)


# ---------------------------------------------------------------- 5. 单一来源


def test_write_tool_names_single_source():
    """写工具清单只允许 ``runtime/write_tools.py`` 一个真相来源。"""
    assert WRITE_TOOL_NAMES == frozenset({"预约挂号", "取消预约挂号"})

    # state 入账范围与清单一致
    state = TurnState()
    state.record_write_result("预约挂号", ToolResult.ok("ok"))
    state.record_write_result("查询是否有号源", ToolResult.ok("ok"))
    assert [r["tool"] for r in state.write_results] == ["预约挂号"]

    # tracker 的副作用判定与清单一致
    repo = InMemoryTaskRepository()
    tracker = TaskTracker(repo)
    tracker.begin(subject_id="alice", session_id="s1")
    exec_id = tracker.tool_started("c1", "预约挂号", {})
    tracker.tool_finished(exec_id, "预约挂号", success=True)
    assert repo.get_tool_execution(exec_id).side_effect_committed is True
