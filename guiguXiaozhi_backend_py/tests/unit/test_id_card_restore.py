"""掩码身份证在执行前还原为真值 —— 护栏单测。

真机事故（2026-11-12）：用户首轮给完整身份证号，落库按 PII 契约打码成
``610121********2831``；下一轮用户只说"下午"，7B 从历史照抄掩码当参数，
``validate_appointment`` 报「身份证号格式有误，应为18位」，预约永远约不成。

纪律：
  * **落库/展示仍掩码**（PII 契约不动，test_phase8_pii 继续钉住）；
  * **执行用真值**（本模块保证），且必须在审批门控之前还原，使
    「审批哈希 = 实际执行参数 = 真值」一致；
  * 找不到真值时**不伪造**，让校验如实失败并提示用户。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from xiaozhi.core.event import EventType  # noqa: E402
from xiaozhi.core.message import Message, Role, ToolCall  # noqa: E402
from xiaozhi.core.tool import ToolResult  # noqa: E402
from xiaozhi.models.ollama import ChatTurn  # noqa: E402
from xiaozhi.ops.metrics import MetricsCollector  # noqa: E402
from xiaozhi.reliability.resilience import RetryConfig  # noqa: E402
from xiaozhi.runtime.appointment_guard import (  # noqa: E402
    extract_latest_id_card,
    is_masked_value,
    restore_masked_id_cards,
)
from xiaozhi.runtime.engine import AgentEngine  # noqa: E402

TRUTH = "610121199401032831"
MASKED = "610121********2831"


# ==========================================================================
# 纯函数
# ==========================================================================


def test_extract_from_current_input_takes_priority():
    assert extract_latest_id_card([], f"王新勃，{TRUTH}，内科，2026.11.14") == TRUTH


def test_extract_falls_back_to_history_user_messages():
    history = [Message.user(f"我叫王新勃 {TRUTH}")]
    assert extract_latest_id_card(history, "下午") == TRUTH


def test_extract_ignores_masked_assistant_messages():
    """模型产出里的掩码不能被当真值取回来（否则还原等于没还原）。"""
    history = [
        Message.user(f"王新勃 {TRUTH}"),
        Message(role=Role.ASSISTANT, content=f"请确认：身份证号：{MASKED}"),
    ]
    assert extract_latest_id_card(history, "下午") == TRUTH


def test_extract_returns_empty_when_only_masked_history():
    history = [Message(role=Role.ASSISTANT, content=MASKED)]
    assert extract_latest_id_card(history, "下午") == ""


def test_extract_prefers_latest_mention():
    history = [Message.user("旧的 110101199003078515")]
    assert extract_latest_id_card(history, f"新的 {TRUTH}") == TRUTH


@pytest.mark.parametrize("value,expected", [(MASKED, True), (TRUTH, False), ("", False)])
def test_is_masked_value(value, expected):
    assert is_masked_value(value) is expected


def test_restore_masked_id_card():
    args = {"id_card": MASKED, "department": "呼吸内科"}
    restored, changed = restore_masked_id_cards(args, TRUTH)
    assert changed is True
    assert restored["id_card"] == TRUTH
    # 不原地修改入参（trace/事件里还握着原始 dict）
    assert args["id_card"] == MASKED


def test_restore_leaves_real_value_untouched():
    assert restore_masked_id_cards({"id_card": TRUTH}, TRUTH) == (
        {"id_card": TRUTH},
        False,
    )


def test_restore_noop_without_truth():
    assert restore_masked_id_cards({"id_card": MASKED}, "") == (
        {"id_card": MASKED},
        False,
    )


def test_restore_ignores_non_id_card_keys():
    assert restore_masked_id_cards({"name": "张*"}, TRUTH) == ({"name": "张*"}, False)


def test_restore_supports_chinese_key():
    restored, changed = restore_masked_id_cards({"身份证号": MASKED}, TRUTH)
    assert changed is True
    assert restored["身份证号"] == TRUTH


# ==========================================================================
# 引擎级：执行前还原
# ==========================================================================


class FakeChatModel:
    model = "fake-model"

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    async def stream_chat(self, messages, tools=None, result_holder=None):
        self.calls += 1
        spec = self.script.pop(0) if self.script else {"turn": ChatTurn(text="兜底")}
        for chunk in spec.get("chunks", []):
            yield chunk
        if result_holder is not None:
            result_holder["turn"] = spec.get("turn", ChatTurn())


class FakeAssembled:
    history: list = []
    dropped_items = 0
    total_tokens = 0

    def to_messages(self, user_input, *, neutralize_history: bool = False):
        return [{"role": "user", "content": user_input}]


class FakeAssembler:
    def assemble(self, *args, **kwargs):
        return FakeAssembled()


class FakeStore:
    def __init__(self, history=None):
        self.history = history or []
        self.saved = None

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


class RecordingExecutor:
    """记录执行器实际收到的**参数**（审批门控之后、工具调用之前）。"""

    def __init__(self):
        self.received = []
        self.result = ToolResult.ok("已受理")

    async def execute(self, call, tool):
        self.received.append(dict(call.args or {}))
        return self.result


def _make_engine(script, *, history):
    executor = RecordingExecutor()
    model = FakeChatModel(script)
    metrics = MetricsCollector()
    engine = AgentEngine(
        chat_model=model,
        tool_registry=FakeRegistry(),
        secure_executor=executor,
        conversation_store=FakeStore(history=history),
        context_assembler=FakeAssembler(),
        system_prompt="系统提示",
        metrics=metrics,
        retry_config=RetryConfig(max_attempts=1),
    )
    return engine, model, executor, metrics


def _booking_script(id_card):
    return [
        {
            "chunks": [],
            "turn": ChatTurn(
                text="",
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="预约挂号",
                        arguments={
                            "patient_name": "王新勃",
                            "id_card": id_card,
                            "department": "呼吸内科",
                            "visit_date": "2026-11-14",
                            "period": "下午",
                        },
                    )
                ],
            ),
        },
        {"chunks": ["好的。"], "turn": ChatTurn(text="好的。")},
    ]


async def _run(engine, message="下午"):
    events = []
    async for event in engine.run("memory-1", message):
        events.append(event)
    return events


@pytest.mark.asyncio
async def test_masked_id_card_is_restored_before_execution():
    history = [Message.user(f"王新勃，{TRUTH}，呼吸内科，2026-11-14")]
    engine, _model, executor, metrics = _make_engine(
        _booking_script(MASKED), history=history
    )

    await _run(engine, "下午")

    assert executor.received, "工具应当被执行（参数已还原）"
    assert executor.received[0]["id_card"] == TRUTH
    # 其他字段不被改动
    assert executor.received[0]["department"] == "呼吸内科"
    assert metrics.snapshot()["counters"]["id_card_restored_total"] == 1


@pytest.mark.asyncio
async def test_real_id_card_is_not_rewritten():
    history = [Message.user(f"王新勃，{TRUTH}，呼吸内科，2026-11-14")]
    engine, _model, executor, metrics = _make_engine(
        _booking_script(TRUTH), history=history
    )

    await _run(engine, "下午")

    assert executor.received[0]["id_card"] == TRUTH
    assert metrics.snapshot()["counters"]["id_card_restored_total"] == 0


@pytest.mark.asyncio
async def test_no_truth_source_does_not_fabricate():
    """历史里完全没有真值时，掩码原样下传 —— 让校验如实失败，绝不伪造号码。"""
    engine, _model, executor, metrics = _make_engine(
        _booking_script(MASKED), history=[]
    )

    await _run(engine, "下午")

    assert executor.received[0]["id_card"] == MASKED
    assert metrics.snapshot()["counters"]["id_card_restored_total"] == 0


@pytest.mark.asyncio
async def test_persisted_tool_call_stays_masked():
    """存储侧契约不变：落库的 tool_call 参数仍按 PII 打码。

    执行用真值，存储用掩码 —— 两条口径各司其职，不能互相污染。
    """
    history = [Message.user(f"王新勃，{TRUTH}，呼吸内科，2026-11-14")]
    engine, _model, _executor, _metrics = _make_engine(
        _booking_script(MASKED), history=history
    )

    await _run(engine, "下午")

    # 落库走 persister：tool_call 参数按 PII 契约仍被掩码（存储安全不变）
    saved = engine.conversation_store.saved or []
    tool_msgs = [m for m in saved if m.tool_calls]
    assert tool_msgs, "落库应包含 assistant(tool_calls) 消息"
    assert tool_msgs[0].tool_calls[0].arguments["id_card"] == MASKED
