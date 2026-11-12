"""纯问候确定性快路径 —— 首句秒回，不进检索、不进模型、不调工具。

回归背景（2026-11-12）：前端「开启新会话」会 reload 并在 onMounted 里自动
发一句"你好"（ChatWindow.vue → hello()）。这句没有任何待办信息，却走完整
7B 生成：trace 9ef10dc2 的 llm.chat = 69447ms，占整轮 99.8%，期间用户只看
到一个空白气泡。寒暄文案不该交给概率生成。

契约：
  * 纯问候 → 固定话术、``model.calls == 0``、话术仍落库（上下文不断）；
  * 医疗安全判定优先于快路径（"你好，我胸痛"仍走急症分流）；
  * 任何夹带业务/身份信息的句子照常进模型与工具链路；
  * 固定话术本身必须过 Phase 9-A 医疗出口扫描（不越界）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from xiaozhi.core.event import EventType  # noqa: E402
from xiaozhi.models.ollama import ChatTurn  # noqa: E402
from xiaozhi.runtime.intent import GREETING_REPLY, is_greeting  # noqa: E402
from xiaozhi.security.medical_safety import EMERGENCY_REPLY  # noqa: E402
from xiaozhi.security.output_guard import scan_medical_output  # noqa: E402

from test_engine_reliability import (  # noqa: E402
    FakeChatModel,
    FakeStore,
    collect,
    make_engine,
)


@pytest.mark.parametrize("greeting", ["你好", "您好", "嗨", "Hello", "在吗"])
async def test_pure_greeting_uses_fast_path_without_model(greeting):
    """纯问候一次模型都不调 —— 这是"首句秒回"的硬约束。"""
    model = FakeChatModel([])
    store = FakeStore()
    engine, metrics = make_engine(model, store=store)

    text, events = await collect(engine, greeting)

    assert text == GREETING_REPLY
    assert model.calls == 0
    counters = metrics.snapshot()["counters"]
    assert counters["greeting_fast_path_total"] == 1
    assert counters["llm_calls_total"] == 0
    assert counters["requests_total"] == 1
    # 固定话术一次性下发（不是逐字流），前端拿到第一个 chunk 就能显示完整句子
    deltas = [e for e in events if e.event_type == EventType.TEXT_RESPONSE]
    assert len(deltas) == 1
    assert deltas[0].metadata["delta"] == GREETING_REPLY


async def test_greeting_reply_is_persisted_as_two_messages():
    """固定话术必须落库；否则下一轮"刚才那个…"的上下文是断的。"""
    store = FakeStore()
    engine, _ = make_engine(FakeChatModel([]), store=store)

    await collect(engine, "你好")

    saved = getattr(store, "saved", None)
    assert saved is not None, "快路径没有落库"
    assert len(saved) == 2  # user + assistant，无工具轮
    assert any(str(getattr(m, "content", "")) == GREETING_REPLY for m in saved)


async def test_medical_safety_takes_priority_over_greeting():
    """夹带急症主诉时走医疗分流，不能被寒暄快路径吞掉。"""
    model = FakeChatModel([])
    engine, metrics = make_engine(model)

    text, _ = await collect(engine, "你好，我胸痛")

    assert text == EMERGENCY_REPLY
    assert model.calls == 0
    counters = metrics.snapshot()["counters"]
    assert counters["greeting_fast_path_total"] == 0
    assert counters["medical_emergency_total"] == 1


async def test_non_greeting_still_reaches_model():
    """零误伤：正常业务问法不能被快路径拦截。"""
    answer = "好的，我来帮您查一下。"
    model = FakeChatModel([{"chunks": [answer], "turn": ChatTurn(text=answer)}])
    engine, metrics = make_engine(model)

    text, _ = await collect(engine, "查询号源")

    assert model.calls == 1
    assert answer in text
    assert GREETING_REPLY not in text
    assert metrics.snapshot()["counters"]["greeting_fast_path_total"] == 0


@pytest.mark.parametrize(
    "text",
    [
        "你好，我胸痛",
        "你好帮我预约挂号",
        "你好我想查呼吸内科",
        "610121199401032831",
        "你好你好你好你好你好",
    ],
)
def test_is_greeting_rejects_non_pure_greetings(text):
    """全匹配短句才算问候；夹带任何业务/身份信息都不进快路径。"""
    assert is_greeting(text) is False


def test_greeting_reply_passes_medical_output_guard():
    """固定话术自身不能越界（不诊断、不处方、不给剂量）。"""
    assert not scan_medical_output(GREETING_REPLY).hit
    assert "剂量" not in GREETING_REPLY
    assert "诊断" not in GREETING_REPLY
