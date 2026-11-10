"""智能体循环的可靠性行为单测 —— 全部用 fake，不依赖 Ollama / Milvus / 数据库。

覆盖：空响应重试、已下发文本时不重试、工具级连续失败熔断、模型熔断降级、
幻觉承诺提示、指标埋点。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from xiaozhi.core.event import EventType  # noqa: E402
from xiaozhi.core.message import Message, Role, ToolCall  # noqa: E402
from xiaozhi.core.tool import ToolResult  # noqa: E402
from xiaozhi.models.ollama import ChatTurn  # noqa: E402
from xiaozhi.models.quality import FALSE_CONFIRMATION_NOTICE  # noqa: E402
from xiaozhi.ops.metrics import MetricsCollector  # noqa: E402
from xiaozhi.reliability.resilience import CircuitBreaker, RetryConfig  # noqa: E402
from xiaozhi.runtime.engine import AgentEngine  # noqa: E402


class FakeChatModel:
    """脚本化模型：每个元素 {"chunks": [...], "turn": ChatTurn, "error": Exception}"""

    model = "fake-model"

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    async def stream_chat(self, messages, tools=None, result_holder=None):
        self.calls += 1
        spec = self.script.pop(0) if self.script else {"turn": ChatTurn(text="兜底回答")}
        for chunk in spec.get("chunks", []):
            yield chunk
        if spec.get("error"):
            raise spec["error"]
        if result_holder is not None:
            result_holder["turn"] = spec.get("turn", ChatTurn())


class FakeAssembled:
    history = []
    dropped_items = 0
    total_tokens = 0

    def to_messages(self, user_input, *, neutralize_history: bool = False):
        self.neutralize_history = neutralize_history
        return [{"role": "user", "content": user_input}]


class FakeAssembler:
    def assemble(self, *args, **kwargs):
        return FakeAssembled()


class FakeStore:
    def __init__(self, history=None):
        self.history = history or []

    async def load(self, memory_id, limit=20):
        return list(self.history)

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
    def __init__(self, result=None):
        self.result = result
        self.calls = 0

    async def execute(self, call, tool):
        self.calls += 1
        return self.result if self.result is not None else ToolResult.ok("OK")


def make_engine(model, **kwargs):
    metrics = kwargs.pop("metrics", None) or MetricsCollector()
    engine = AgentEngine(
        chat_model=model,
        tool_registry=FakeRegistry(),
        secure_executor=kwargs.pop("executor", None) or FakeExecutor(),
        conversation_store=kwargs.pop("store", None) or FakeStore(),
        context_assembler=FakeAssembler(),
        system_prompt="系统提示",
        retriever=None,
        metrics=metrics,
        retry_config=kwargs.pop("retry_config", None) or RetryConfig(max_attempts=1),
        circuit_breaker=kwargs.pop("breaker", None),
        tool_failure_threshold=kwargs.pop("tool_failure_threshold", 3),
    )
    return engine, metrics


async def collect(engine, message="你好"):
    parts = []
    events = []
    async for event in engine.run("memory-1", message):
        events.append(event)
        if event.event_type == EventType.TEXT_RESPONSE:
            parts.append(event.metadata.get("delta", ""))
    return "".join(parts), events


async def test_empty_response_is_retried_without_duplicate_text():
    answer = "你好，请问有什么可以帮您？"
    model = FakeChatModel(
        [
            {"turn": ChatTurn()},  # Ollama 已知行为：200 但内容为空
            {"chunks": [answer], "turn": ChatTurn(text=answer)},
        ]
    )
    engine, metrics = make_engine(
        model, retry_config=RetryConfig(max_attempts=3, base_delay=0, jitter=0)
    )
    text, _ = await collect(engine)

    assert text == answer  # 没有重复文本
    assert model.calls == 2
    assert metrics.snapshot()["counters"]["empty_response_retries"] == 1


async def test_no_retry_once_text_has_been_streamed():
    model = FakeChatModel(
        [
            {"chunks": ["部分内容"], "error": RuntimeError("流中断")},
            {"chunks": ["重试内容"], "turn": ChatTurn(text="重试内容")},
        ]
    )
    engine, metrics = make_engine(
        model, retry_config=RetryConfig(max_attempts=3, base_delay=0, jitter=0)
    )
    text, _ = await collect(engine)

    assert model.calls == 1
    assert "部分内容" in text
    assert "重试内容" not in text
    assert metrics.snapshot()["counters"]["degraded_total"] == 1


async def test_tool_is_circuit_broken_after_consecutive_failures():
    calls = [
        ToolCall(id=str(i), name="查询是否有号源", arguments={}) for i in range(3)
    ]
    model = FakeChatModel(
        [
            {"turn": ChatTurn(tool_calls=calls)},
            {"chunks": ["已处理"], "turn": ChatTurn(text="已处理")},
        ]
    )
    executor = FakeExecutor(ToolResult.fail("号源服务异常", "UpstreamError"))
    engine, metrics = make_engine(
        model, executor=executor, tool_failure_threshold=2
    )
    await collect(engine, "查号源")

    assert executor.calls == 2  # 第三次被工具级熔断短路
    counters = metrics.snapshot()["counters"]
    assert counters["tool_calls_total"] == 3
    assert counters["tool_calls_failed"] == 3


async def test_open_model_circuit_returns_rule_based_fallback():
    breaker = CircuitBreaker("fake", failure_threshold=1, recovery_timeout=100)
    breaker.record_failure()
    model = FakeChatModel([{"chunks": ["不该被调用"], "turn": ChatTurn(text="不该被调用")}])
    engine, metrics = make_engine(model, breaker=breaker)

    text, _ = await collect(engine, "我最近头痛")
    assert model.calls == 0
    assert "神经内科" in text
    assert "预约成功" not in text
    assert metrics.snapshot()["counters"]["degraded_total"] == 1


async def test_false_confirmation_falls_back_to_honest_answer_after_retry():
    """纠正重试后模型给出诚实回答（未调工具、未编造） → 不追加免责提示"""
    model = FakeChatModel(
        [{"chunks": ["已为您预约成功"], "turn": ChatTurn(text="已为您预约成功")}]
    )
    engine, metrics = make_engine(model)
    text, _ = await collect(engine)

    assert "已为您预约成功" in text
    assert FALSE_CONFIRMATION_NOTICE not in text
    counters = metrics.snapshot()["counters"]
    assert counters["false_confirmation_retries"] == 1
    assert counters["hallucination_suspected"] == 1


async def test_false_confirmation_corrective_retry_forces_real_tool_call():
    """真实前端联调（2026-11-10）暴露的场景：模型口头说"预约挂号中"却不调工具，
    下一轮还编造"预约挂号结果显示成功"。纠正重试应逼出真正的工具调用。"""
    model = FakeChatModel(
        [
            {"chunks": ["已为您预约成功"], "turn": ChatTurn(text="已为您预约成功")},
            # 收到纠正提示后，模型真正发起工具调用
            {
                "turn": ChatTurn(
                    tool_calls=[ToolCall(id="1", name="预约挂号", arguments={})]
                )
            },
            {"chunks": ["预约手续已完成"], "turn": ChatTurn(text="预约手续已完成")},
        ]
    )
    engine, metrics = make_engine(model)
    text, _ = await collect(engine, "帮我预约神经内科")

    assert "已为您预约成功" in text  # 第一轮假话已流式下发，无法撤回
    assert "预约手续已完成" in text  # 纠正后真正执行了工具
    assert FALSE_CONFIRMATION_NOTICE not in text  # 纠正成功 → 不再追加免责提示
    counters = metrics.snapshot()["counters"]
    assert counters["false_confirmation_retries"] == 1
    assert counters["hallucination_suspected"] == 1


async def test_corrective_retry_still_false_then_notice_appended():
    """纠正重试后模型仍编造成功 → 追加免责提示兜底"""
    model = FakeChatModel(
        [
            {"chunks": ["已为您预约成功"], "turn": ChatTurn(text="已为您预约成功")},
            {
                "chunks": ["真的，您的挂号已经预约好了"],
                "turn": ChatTurn(text="真的，您的挂号已经预约好了"),
            },
        ]
    )
    engine, metrics = make_engine(model)
    text, _ = await collect(engine, "帮我预约神经内科")

    assert FALSE_CONFIRMATION_NOTICE in text
    counters = metrics.snapshot()["counters"]
    assert counters["false_confirmation_retries"] == 1
    assert counters["hallucination_suspected"] == 2  # 两轮都被判定


async def test_prior_booking_in_history_suppresses_notice_and_retry():
    """上一个请求里真的挂过号（有成功标记）→ "已经预约好了"是真话，不应误报也不应纠正重试"""
    history = [
        Message(
            role=Role.ASSISTANT,
            content="预约成功",
            tool_calls=[ToolCall(id="9", name="预约挂号", arguments={})],
            metadata={"booking_executed": True},
        )
    ]
    model = FakeChatModel(
        [
            {
                "chunks": ["您的挂号已经预约好了"],
                "turn": ChatTurn(text="您的挂号已经预约好了"),
            }
        ]
    )
    engine, metrics = make_engine(model, store=FakeStore(history))
    text, _ = await collect(engine, "挂号成功了吗？")

    assert "已经预约好了" in text
    assert FALSE_CONFIRMATION_NOTICE not in text
    assert model.calls == 1  # 没有纠正重试
    assert metrics.snapshot()["counters"]["hallucination_suspected"] == 0


async def test_cancelled_booking_in_history_still_flags_false_confirmation():
    """取消过号之后再说"已经预约好了" → 仍然是幻觉"""
    history = [
        Message(
            role=Role.ASSISTANT,
            content="预约成功",
            tool_calls=[ToolCall(id="9", name="预约挂号", arguments={})],
            metadata={"booking_executed": True},
        ),
        Message(
            role=Role.ASSISTANT,
            content="已取消",
            tool_calls=[ToolCall(id="10", name="取消预约挂号", arguments={})],
            metadata={"cancel_executed": True},
        ),
    ]
    model = FakeChatModel(
        [
            {
                "chunks": ["您的挂号已经预约好了"],
                "turn": ChatTurn(text="您的挂号已经预约好了"),
            },
            {
                "turn": ChatTurn(
                    tool_calls=[ToolCall(id="11", name="预约挂号", arguments={})]
                )
            },
            {"chunks": ["已重新预约"], "turn": ChatTurn(text="已重新预约")},
        ]
    )
    engine, metrics = make_engine(model, store=FakeStore(history))
    text, _ = await collect(engine, "挂号成功了吗？")

    assert FALSE_CONFIRMATION_NOTICE not in text  # 纠正重试成功
    assert metrics.snapshot()["counters"]["false_confirmation_retries"] == 1


async def test_request_metrics_are_recorded():
    model = FakeChatModel([{"chunks": ["好的"], "turn": ChatTurn(text="好的")}])
    engine, metrics = make_engine(model)
    await collect(engine)

    snapshot = metrics.snapshot()
    assert snapshot["counters"]["requests_total"] == 1
    assert snapshot["counters"]["llm_calls_total"] == 1
    assert snapshot["histograms"]["request_duration_ms"]["count"] == 1
