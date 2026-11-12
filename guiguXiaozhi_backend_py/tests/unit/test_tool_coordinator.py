"""B2 第三批护栏：``ToolCoordinator`` 抽取不改行为、不改事件顺序。

整改方案 §4 P1-1.1 / §6 批次 B2 要求「先抽协作者、再迁移方法、最后删 mixin」
且「迁移前后保持对外事件顺序兼容」。``_execute_tools`` 是工具执行、审批短路、
PII 真值还原、任务生命周期 STARTED/FINISHED 与 ``messages`` 回填的唯一来源，
因此本批重点钉住：

1. 委托壳与协作者本体逐字等价（事件、metadata、messages、state、result）；
2. 协作者实例不保存任何请求级状态（共享实例被并发请求复用）；
3. 运行时依赖按调用读取，构造后替换 ``secure_executor`` / ``max_tool_calls``
   仍生效（既有装配与单测有构造后注入的用法）；
4. 成功 / 审批短路 / 未注册 / 次数上限 / PII 还原五条路径的事件与回填语义；
5. Phase 5 ``tool_started`` / ``tool_finished`` 成对且顺序不变。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from xiaozhi.core.message import Role, ToolCall  # noqa: E402
from xiaozhi.core.tool import ToolResult  # noqa: E402
from xiaozhi.models.ollama import ChatTurn  # noqa: E402
from xiaozhi.ops.metrics import MetricsCollector  # noqa: E402
from xiaozhi.runtime.engine import AgentEngine, AgentEngineResultHolder  # noqa: E402
from xiaozhi.runtime.state import TurnState  # noqa: E402
from xiaozhi.runtime.tool_coordinator import ToolCoordinator  # noqa: E402


# ---------------------------------------------------------------- 引擎级 fake


class FakeChatModel:
    model = "fake-model"

    async def stream_chat(self, messages, tools=None, result_holder=None):
        yield ""


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
    def __init__(self, *, missing=False):
        self.missing = missing

    def list_definitions(self):
        return []

    def get(self, name):
        return None if self.missing else object()


class RecordingExecutor:
    """记录每次执行时真实拿到的参数（用于验证 PII 还原发生在执行之前）。"""

    def __init__(self, result=None):
        self.result = result or ToolResult.ok("OK")
        self.calls = []

    async def execute(self, call, tool):
        self.calls.append(dict(call.args))
        return self.result


class RecordingTracker:
    """Phase 5：STARTED 必须在执行前、FINISHED 必须在执行后，且 exec_id 一致。"""

    def __init__(self):
        self.records = []
        self._seq = 0

    def tool_started(self, tool_call_id, tool_name, args):
        self._seq += 1
        exec_id = f"exec-{self._seq}"
        self.records.append(("started", exec_id, tool_name, True))
        return exec_id

    def tool_finished(self, exec_id, tool_name, *, success):
        self.records.append(("finished", exec_id, tool_name, success))


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
        id=call_id,
        name=name,
        arguments=dict(args if args is not None else {"dept": "呼吸内科"}),
    )


def make_response(*calls, text="我来查询"):
    return ChatTurn(text=text, tool_calls=list(calls))


def seq(events):
    return [e.event_type.value for e in events]


def _persisted_digest(state):
    return [
        (m.role.value, m.content, m.tool_call_id, m.tool_name, m.is_error)
        for m in state.persisted_tool_results
    ]


async def _collect_tools(
    engine,
    *,
    use_delegate: bool,
    response=None,
    state=None,
    messages=None,
    result=None,
):
    """同一份输入，分别经委托壳与协作者本体执行，用于逐字对照。"""
    state = TurnState() if state is None else state
    messages = [] if messages is None else messages
    result = AgentEngineResultHolder() if result is None else result
    response = make_response(make_call()) if response is None else response
    kwargs = dict(
        response=response,
        messages=messages,
        state=state,
        user_id="anonymous",
        trace_id="t-b2-tool",
        result=result,
    )
    if use_delegate:
        # 委托壳按调用从 self 现取运行时依赖，不接受这些 kwarg。
        gen = engine._execute_tools(**kwargs)
    else:
        gen = engine._tool_coordinator.execute_tools(
            **kwargs,
            metrics=engine.metrics,
            max_tool_calls=engine.max_tool_calls,
            tool_failure_threshold=engine.tool_failure_threshold,
            tool_registry=engine.tool_registry,
            task_tracker=engine.task_tracker,
            secure_executor=engine.secure_executor,
        )
    events = [e async for e in gen]
    return events, messages, state, result


# ---------------------------------------------------------------- 装配契约


def test_tool_coordinator_holds_no_request_state():
    """共享实例上只能有两个纯函数 Port，不能缓存任何请求级/运行时依赖。"""
    engine, _ = make_engine()
    assert isinstance(engine._tool_coordinator, ToolCoordinator)
    fields = vars(engine._tool_coordinator)
    assert set(fields) == {"_assistant_to_ollama", "_tool_to_ollama"}
    assert all(callable(v) for v in fields.values())
    for forbidden in (
        "metrics", "tool_registry", "secure_executor", "task_tracker",
        "max_tool_calls", "tool_failure_threshold", "messages", "state", "result",
    ):
        assert forbidden not in fields


async def test_replaced_secure_executor_after_construction_is_used():
    """构造后替换 secure_executor 必须生效（不能在构造期快照）。"""
    engine, _ = make_engine()
    replacement = RecordingExecutor(ToolResult.ok("替换执行器的结果"))
    engine.secure_executor = replacement

    events, _, _, _ = await _collect_tools(engine, use_delegate=True)

    assert len(replacement.calls) == 1
    assert seq(events) == ["tool_execute", "tool_result"]
    assert events[1].metadata["result_preview"] == "替换执行器的结果"


async def test_max_tool_calls_is_read_per_call():
    """构造后把 max_tool_calls 改成 0：不执行工具、不发事件，仍回填提示消息。"""
    engine, _ = make_engine(max_tool_calls=8)
    engine.max_tool_calls = 0
    executor = engine.secure_executor

    events, messages, state, _ = await _collect_tools(engine, use_delegate=True)

    assert executor.calls == []
    assert events == []
    assert state.tool_call_count == 0
    assert any("次数已达上限" in m["content"] for m in messages)
    assert [m["role"] for m in messages] == [Role.ASSISTANT.value, Role.TOOL.value]


# ---------------------------------------------------------------- 执行路径语义


async def test_success_path_events_and_message_backfill():
    """成功一轮：事件顺序、messages 回填、state 计数、text 账本全部逐字不变。"""
    engine, _ = make_engine()
    call = make_call()

    events, messages, state, result = await _collect_tools(
        engine, use_delegate=False, response=make_response(call)
    )

    assert seq(events) == ["tool_execute", "tool_result"]
    assert events[0].metadata == {"tool_name": call.name, "tool_id": call.id}
    assert events[1].metadata["is_error"] is False
    assert [m["role"] for m in messages] == [Role.ASSISTANT.value, Role.TOOL.value]
    assert messages[0]["tool_calls"][0]["id"] == call.id
    assert messages[1]["tool_call_id"] == call.id
    assert state.tool_call_count == 1
    assert state.successful_tool_calls == 1
    assert state.narrative_text == "我来查询"
    assert result.tool_calls == [call]
    assert state.tool_results == []  # 本轮回填后清空
    assert len(state.persisted_tool_results) == 1  # 落库累积不动


async def test_approval_gate_short_circuits_round():
    """审批门控：不发 TOOL_RESULT，改发可读文案 + APPROVAL_REQUIRED 后 break。"""
    approval = {"message": "请确认是否预约", "token": "appr-1"}
    engine, _ = make_engine()
    engine.secure_executor = RecordingExecutor(ToolResult.ok("待确认", approval=approval))

    events, _, state, result = await _collect_tools(engine, use_delegate=False)

    assert seq(events) == ["tool_execute", "text_response", "approval_required"]
    assert events[1].metadata["delta"] == "请确认是否预约"
    assert events[2].metadata == approval
    assert "tool_result" not in seq(events)
    assert result.text == "请确认是否预约"
    assert state.approval_required is True
    assert state.approval_detail == approval
    assert state.final_reply_text == "请确认是否预约"


async def test_unregistered_tool_yields_structured_error():
    """工具未注册：结构化失败结果仍按原顺序发出，且计入失败账本。"""
    engine, _ = make_engine()
    engine.tool_registry = FakeRegistry(missing=True)

    events, _, state, _ = await _collect_tools(engine, use_delegate=False)

    assert seq(events) == ["tool_execute", "tool_result"]
    assert events[1].metadata["is_error"] is True
    assert "不存在" in events[1].metadata["result_preview"]
    assert state.failure_count("查询是否有号源") == 1
    assert state.last_tool_failure["error_type"] == "ToolNotFound"


async def test_task_tracker_started_finished_bracket_execution():
    """Phase 5：STARTED 先于执行、FINISHED 收尾，exec_id 与工具名一致。"""
    engine, _ = make_engine()
    tracker = RecordingTracker()
    engine.task_tracker = tracker

    await _collect_tools(
        engine,
        use_delegate=False,
        response=make_response(make_call(name="预约挂号")),
    )

    assert [r[0] for r in tracker.records] == ["started", "finished"]
    assert tracker.records[0][1] == tracker.records[1][1]
    assert tracker.records[0][2] == tracker.records[1][2] == "预约挂号"
    assert tracker.records[1][3] is True


async def test_masked_id_card_is_restored_before_execution():
    """PII 真值还原必须先于审批/校验/执行，且原地更新同一份参数。"""
    engine, metrics = make_engine()
    call = make_call(name="预约挂号", args={"id_card": "610121********2831"})
    state = TurnState(known_id_card="610121199001012831")
    executor = engine.secure_executor

    _, messages, _, _ = await _collect_tools(
        engine, use_delegate=False, response=make_response(call), state=state
    )

    assert executor.calls == [{"id_card": "610121199001012831"}]
    assert call.arguments["id_card"] == "610121199001012831"
    assert (
        messages[0]["tool_calls"][0]["function"]["arguments"]["id_card"]
        == "610121199001012831"
    )
    assert metrics.snapshot()["counters"]["id_card_restored_total"] == 1


# ---------------------------------------------------------------- 委托等价


async def test_delegate_and_coordinator_are_behavior_equivalent():
    """委托壳只是透传：事件、metadata、messages、state、文本账本全一致。"""
    engine, _ = make_engine()

    def build():
        return dict(
            response=make_response(make_call()),
            state=TurnState(),
            messages=[],
            result=AgentEngineResultHolder(),
        )

    d_events, d_messages, d_state, d_result = await _collect_tools(
        engine, use_delegate=True, **build()
    )
    c_events, c_messages, c_state, c_result = await _collect_tools(
        engine, use_delegate=False, **build()
    )

    assert seq(d_events) == seq(c_events)
    assert [e.metadata for e in d_events] == [e.metadata for e in c_events]
    assert d_messages == c_messages
    assert d_result.text == c_result.text
    assert [x.name for x in d_result.tool_calls] == [
        x.name for x in c_result.tool_calls
    ]
    assert d_state.tool_call_count == c_state.tool_call_count
    assert d_state.successful_tool_calls == c_state.successful_tool_calls
    assert d_state.narrative_text == c_state.narrative_text
    assert _persisted_digest(d_state) == _persisted_digest(c_state)
