"""B2 第二批护栏：``LoopController`` 抽取不改行为、不改事件顺序。

整改方案 §4 P1-1.5 要求「迁移前后保持对外事件顺序兼容」。主循环是全部
``TURN_START`` / ``TURN_END`` / 降级 / 审批短路 / ``held_deltas`` 补发的唯一
来源，因此本批重点钉住：

1. 委托壳与协作者本体逐字等价；
2. 协作者实例不保存任何请求级状态（共享实例被并发请求复用）；
3. ``max_turns`` / ``circuit_breaker`` 按调用读取，构造后替换仍生效；
4. 正常 / 短路 / 模型错误三条路径的完整事件序列快照。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from xiaozhi.core.event import EventType  # noqa: E402
from xiaozhi.core.tool import ToolResult  # noqa: E402
from xiaozhi.models.ollama import ChatTurn  # noqa: E402
from xiaozhi.ops.metrics import MetricsCollector  # noqa: E402
from xiaozhi.runtime.engine import AgentEngine, AgentEngineResultHolder  # noqa: E402
from xiaozhi.runtime.loop_controller import LoopController, LoopPorts  # noqa: E402
from xiaozhi.runtime.state import TurnState  # noqa: E402


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


class _FakeAssembled:
    history = []
    dropped_items = 0
    total_tokens = 0

    def to_messages(self, user_input, *, neutralize_history: bool = False):
        return [{"role": "user", "content": user_input}]


class FakeAssembler:
    def assemble(self, *args, **kwargs):
        return _FakeAssembled()


class FakeStore:
    async def load(self, memory_id, limit=20):
        return []

    async def save(self, memory_id, messages):
        return None

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


class OpenBreaker:
    """常开熔断器：守门二必须在调模型之前短路。"""

    def __init__(self):
        self.successes = 0
        self.failures = 0

    def allow(self):
        return False

    def record_success(self):
        self.successes += 1

    def record_failure(self):
        self.failures += 1


def make_engine(model=None, **kwargs):
    metrics = MetricsCollector()
    engine = AgentEngine(
        chat_model=model or FakeChatModel(),
        tool_registry=FakeRegistry(),
        secure_executor=FakeExecutor(),
        conversation_store=FakeStore(),
        context_assembler=FakeAssembler(),
        system_prompt="系统提示",
        metrics=metrics,
        **kwargs,
    )
    return engine, metrics


async def collect(engine, message="查询号源"):
    return [e async for e in engine.run("memory-1", message)]


def seq(events):
    return [e.event_type.value for e in events]


# ---------------------------------------------------------------- 装配契约


def test_loop_controller_holds_no_request_state():
    """共享实例上绝不能有 messages/state/result 等请求级数据。"""
    engine, _ = make_engine()
    assert isinstance(engine._loop_controller, LoopController)
    fields = vars(engine._loop_controller)
    assert set(fields) == {"_ports"}
    ports = fields["_ports"]
    assert isinstance(ports, LoopPorts)
    for name in (
        "time_budget_exceeded", "degrade_on_time_budget", "circuit_is_open",
        "degrade_on_circuit_open", "degrade_on_model_error", "chat_turn",
        "execute_approved_operation", "retry_with_approval_hint",
        "schedule_corrective_retry", "execute_tools",
    ):
        assert callable(getattr(ports, name))


async def test_max_turns_zero_skips_model_entirely():
    """max_turns 按调用读取：0 轮时主循环不进，但仍要有完整首尾事件。"""
    model = FakeChatModel()
    engine, _ = make_engine(model=model, max_turns=0)

    events = await collect(engine)

    assert seq(events) == ["agent_start", "agent_end"]
    assert model.calls == 0
    assert sum(e.event_type == EventType.AGENT_END for e in events) == 1


async def test_max_turns_replaced_after_construction_still_takes_effect():
    """构造后替换 max_turns 必须生效（不能在构造期快照）。"""
    model = FakeChatModel()
    engine, _ = make_engine(model=model, max_turns=6)
    engine.max_turns = 0

    events = await collect(engine)

    assert seq(events) == ["agent_start", "agent_end"]
    assert model.calls == 0


async def test_circuit_open_degrades_before_any_model_call():
    """熔断守门在 TURN_START 之前，降级不进模型。"""
    model = FakeChatModel()
    breaker = OpenBreaker()
    engine, _ = make_engine(model=model, circuit_breaker=breaker)

    events = await collect(engine)

    types = seq(events)
    assert model.calls == 0
    assert types[0] == "agent_start" and types[-1] == "agent_end"
    assert "turn_start" not in types
    assert "degraded" in types
    assert breaker.successes == 0


# ---------------------------------------------------------------- 事件序列快照


@pytest.mark.parametrize(
    "message, model, expected",
    [
        (
            "查询号源", None,
            ["agent_start", "turn_start", "text_response", "agent_end"],
        ),
        (
            "你好", None,
            ["agent_start", "text_response", "agent_end"],
        ),
        (
            "查询号源", "error",
            ["agent_start", "turn_start", "text_response", "degraded", "agent_end"],
        ),
    ],
)
async def test_event_sequences_unchanged_after_loop_extraction(message, model, expected):
    err_model = FakeChatModel(error=RuntimeError("模型炸了")) if model == "error" else None
    engine, _ = make_engine(model=err_model)

    assert seq(await collect(engine, message)) == expected


# ---------------------------------------------------------------- 委托等价


async def _collect_loop(engine, *, use_delegate: bool):
    model = FakeChatModel("回答内容")
    engine.chat_model = model
    result = AgentEngineResultHolder()
    outcome = {}
    kwargs = dict(
        messages=[{"role": "user", "content": "查询号源"}],
        tools=[],
        known_tool_names=[],
        state=TurnState(),
        user_input="查询号源",
        user_id="anonymous",
        trace_id="t-loop",
        request_span=SimpleNamespace(set_attribute=lambda *a, **k: None),
        started=time.monotonic(),
        result=result,
        outcome=outcome,
        budget=None,
        max_turns=engine.max_turns,
        circuit_breaker=engine.circuit_breaker,
    )
    if use_delegate:
        # 委托壳自己从 self 补 max_turns / circuit_breaker，不接受这两个 kwarg；
        # 直接调用协作者则必须显式传入 —— 这里正是要验证两者结果一致。
        shell_kwargs = {
            k: v for k, v in kwargs.items()
            if k not in ("max_turns", "circuit_breaker")
        }
        gen = engine._run_loop(**shell_kwargs)
    else:
        gen = LoopController(engine._loop_controller._ports).run_loop(**kwargs)
    events = [e async for e in gen]
    return events, outcome, result, model


async def test_delegate_and_loop_controller_are_behavior_equivalent():
    """委托壳只是透传：事件、metadata、轮数、文本账本、模型调用次数全一致。"""
    engine, _ = make_engine()
    d_events, d_outcome, d_result, d_model = await _collect_loop(engine, use_delegate=True)
    events, outcome, result, model = await _collect_loop(engine, use_delegate=False)

    assert seq(d_events) == seq(events)
    assert [e.metadata for e in d_events] == [e.metadata for e in events]
    assert d_outcome["turns"] == outcome["turns"]
    assert d_result.text == result.text
    assert d_result.degraded == result.degraded
    # fake 无 tool_calls → 主循环 1 轮即 break（不跑满 max_turns）
    assert d_model.calls == model.calls == 1
