"""B2 第一批护栏：``ModelRunner`` 抽取不改行为、不改事件顺序。

整改方案 §4 P1-1.1 / §6 批次 B2 要求「先抽协作者、再迁移方法、最后删 mixin」
且「迁移前后保持对外事件顺序兼容」。这批断言钉住：

1. 委托壳与直接调用协作者逐字等价（事件、metadata、outcome、文本账本）；
2. 完整事件序列快照（此前全仓库只有首尾事件与存在性断言）；
3. 三个确定性短路完全不经过 ModelRunner；
4. 运行时依赖不在构造期快照（既有 ``engine.chat_model = ...`` 注入方式）；
5. 请求级判定 Port 每轮现取，不在构造期固化。
"""

from __future__ import annotations

import sys
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
from xiaozhi.runtime.model_runner import ModelRunner  # noqa: E402
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


def make_engine(model=None):
    metrics = MetricsCollector()
    engine = AgentEngine(
        chat_model=model or FakeChatModel(),
        tool_registry=FakeRegistry(),
        secure_executor=FakeExecutor(),
        conversation_store=FakeStore(),
        context_assembler=FakeAssembler(),
        system_prompt="系统提示",
        metrics=metrics,
    )
    return engine, metrics


async def collect(engine, message="查询号源"):
    return [event async for event in engine.run("memory-1", message)]


def seq(events):
    return [e.event_type.value for e in events]


# ---------------------------------------------------------------- 装配契约


def test_runner_is_assembled_without_snapshots_of_runtime_dependencies():
    """构造期不能快照 chat_model / retry_config / metrics。

    既有装配与 ``test_approval_auto_execution.py`` 会在构造后赋值
    ``engine.chat_model = FakeChatModel(...)``；快照会让这些注入静默失效。
    """
    engine, _ = make_engine()
    assert isinstance(engine._model_runner, ModelRunner)
    fields = vars(engine._model_runner)
    assert "chat_model" not in fields
    assert "retry_config" not in fields
    assert "metrics" not in fields


async def test_model_replaced_after_construction_is_still_used():
    engine, _ = make_engine()
    replacement = FakeChatModel("替换后的回答")
    engine.chat_model = replacement

    events = await collect(engine)

    deltas = [
        e.metadata["delta"]
        for e in events
        if e.event_type == EventType.TEXT_RESPONSE
    ]
    assert deltas == ["替换后的回答"]
    assert replacement.calls == 1


async def test_request_level_ports_are_evaluated_per_turn():
    """审批 token / 出口护栏是请求级判定，必须每轮现取（不在构造期固化）。"""
    engine, _ = make_engine()
    calls = {"approval": 0, "guard": 0}

    def approval_probe():
        calls["approval"] += 1
        return False

    def guard_probe():
        calls["guard"] += 1
        return True

    engine._model_runner = ModelRunner(
        has_pending_approval_token=approval_probe,
        output_guard_enabled=guard_probe,
    )

    await collect(engine)
    await collect(engine)

    assert calls["approval"] == 2
    assert calls["guard"] == 2


# ---------------------------------------------------------------- 事件序列快照


async def test_normal_run_event_sequence_snapshot():
    """正常链路完整序列：B2 抽取后逐字不变。"""
    engine, _ = make_engine()
    events = await collect(engine, "查询号源")

    assert seq(events) == [
        "agent_start",
        "turn_start",
        "text_response",
        "agent_end",
    ]
    assert sum(e.event_type == EventType.AGENT_END for e in events) == 1


@pytest.mark.parametrize("message", ["你好", "我爸胸痛晕倒了怎么办"])
async def test_short_circuits_never_reach_model_runner(message):
    """问候 / 医疗安全短路不进模型，也不改变首尾事件语义。"""
    model = FakeChatModel()
    engine, _ = make_engine(model=model)

    events = await collect(engine, message)

    assert model.calls == 0
    assert seq(events) == ["agent_start", "text_response", "agent_end"]


async def test_model_error_event_sequence_snapshot():
    """模型抛错时降级文案与 degraded 仍按原顺序出现。"""
    model = FakeChatModel(error=RuntimeError("模型炸了"))
    engine, _ = make_engine(model=model)

    events = await collect(engine)

    assert seq(events) == [
        "agent_start",
        "turn_start",
        "text_response",
        "degraded",
        "agent_end",
    ]
    assert sum(e.event_type == EventType.AGENT_END for e in events) == 1


# ---------------------------------------------------------------- 委托等价


async def _collect_chat_turn(engine, *, use_delegate: bool):
    """同一份输入，分别经委托壳与协作者本体执行，用于逐字对照。"""
    model = FakeChatModel("回答内容")
    result = AgentEngineResultHolder()
    state = TurnState()
    outcome = {}
    span = SimpleNamespace(set_attribute=lambda *args, **kwargs: None)
    kwargs = dict(
        messages=[{"role": "user", "content": "查询号源"}],
        tools=[],
        known_tool_names=[],
        state=state,
        user_input="查询号源",
        trace_id="t-b2",
        turn_number=0,
        request_span=span,
        result=result,
        outcome=outcome,
    )
    if use_delegate:
        engine.chat_model = model
        gen = engine._chat_turn(**kwargs)
    else:
        runner = ModelRunner(
            has_pending_approval_token=engine._has_pending_approval_token,
            output_guard_enabled=engine._output_guard_enabled,
        )
        gen = runner.chat_turn(
            chat_model=model,
            retry_config=engine.retry_config,
            metrics=engine.metrics,
            **kwargs,
        )
    events = [e async for e in gen]
    return events, outcome, result, model


async def test_delegate_and_runner_are_behavior_equivalent():
    """委托壳只是透传：事件、metadata、outcome、文本账本、模型调用次数全一致。"""
    engine, _ = make_engine()
    d_events, d_outcome, d_result, d_model = await _collect_chat_turn(
        engine, use_delegate=True
    )
    events, outcome, result, model = await _collect_chat_turn(
        engine, use_delegate=False
    )

    assert seq(d_events) == seq(events)
    assert [e.metadata for e in d_events] == [e.metadata for e in events]
    assert d_result.text == result.text
    assert d_result.degraded == result.degraded
    assert d_outcome.keys() == outcome.keys()
    assert d_outcome["emitted_this_turn"] == outcome["emitted_this_turn"]
    assert d_outcome["held_deltas"] == outcome["held_deltas"]
    assert d_outcome["call_error"] == outcome["call_error"]
    assert d_outcome["response"].text == outcome["response"].text
    assert d_model.calls == model.calls == 1
