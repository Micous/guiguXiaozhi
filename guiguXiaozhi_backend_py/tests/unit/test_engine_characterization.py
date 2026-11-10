"""P1-E1 特征测试（characterization tests）—— engine 重构前把现行为钉死。

这些测试描述的是**当前**引擎的可观测行为（事件、指标、落库、下发文本），
重构（E2-E6）过程中一个字不改；重构后它们全部原样通过即是行为等价的证明。

覆盖缺口（2026-11-10 审计结论，此前均为 0 覆盖）：
  * 流式回看窗口：承诺词逐字跨 chunk 到达时截断点正确；
  * _trim_to_sentence 切割边界；
  * 纠正重试至多一次（二次编造走兜底不再重试）；
  * 请求级时间预算触发降级；
  * 指标埋点 narrated_tool_call_retries / false_confirmation_truncated /
    textual_tool_call_truncated；
  * 落库 JSON 结构整体快照（叙述轮+工具轮+最终轮）。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from xiaozhi.core.event import EventType  # noqa: E402
from xiaozhi.core.message import Message, Role, ToolCall  # noqa: E402
from xiaozhi.core.tool import ToolResult  # noqa: E402
from xiaozhi.models.ollama import ChatTurn  # noqa: E402
from xiaozhi.models.quality import FALLBACK_BOOKING_NOT_DONE  # noqa: E402
from xiaozhi.ops.metrics import MetricsCollector  # noqa: E402
from xiaozhi.reliability.resilience import RetryConfig  # noqa: E402
from xiaozhi.runtime.engine import AgentEngine, _trim_to_sentence  # noqa: E402

from test_engine_reliability import (  # noqa: E402
    FakeChatModel,
    FakeExecutor,
    FakeRegistry,
    FakeStore,
    FakeAssembler,
    collect,
)


def make_engine(model, **kwargs):
    metrics = kwargs.pop("metrics", None) or MetricsCollector()
    engine = AgentEngine(
        chat_model=model,
        tool_registry=kwargs.pop("registry", None) or FakeRegistry(),
        secure_executor=kwargs.pop("executor", None) or FakeExecutor(),
        conversation_store=kwargs.pop("store", None) or FakeStore(),
        context_assembler=kwargs.pop("assembler", None) or FakeAssembler(),
        system_prompt="系统提示",
        retriever=kwargs.pop("retriever", None),
        metrics=metrics,
        retry_config=kwargs.pop("retry_config", None) or RetryConfig(max_attempts=1),
        circuit_breaker=kwargs.pop("breaker", None),
        request_timeout=kwargs.pop("request_timeout", None),
        tool_failure_threshold=kwargs.pop("tool_failure_threshold", 3),
    )
    assert not kwargs, f"未消费的参数: {kwargs}"
    return engine, metrics


# ---------- 1. 流式回看窗口：逐字 chunk ----------


async def test_lookbehind_catches_promise_streamed_char_by_char():
    """承诺词按 1-2 字粒度跨 chunk 到达时，一个字都不能漏给用户。"""
    promise = "已为您预约成功"
    model = FakeChatModel(
        [
            {
                "chunks": [promise[i : i + 2] for i in range(0, len(promise), 2)],
                "turn": ChatTurn(text=promise),
            },
            {
                "chunks": ["抱歉，本次预约没有办理成功。"],
                "turn": ChatTurn(text="抱歉，本次预约没有办理成功。"),
            },
        ]
    )
    engine, metrics = make_engine(model)
    text, _ = await collect(engine, "帮我预约神经内科")

    assert "已为您预约成功" not in text
    assert "预约成功" not in text  # 半截也不行
    assert "抱歉，本次预约没有办理成功。" in text
    assert metrics.snapshot()["counters"]["false_confirmation_truncated"] == 1


async def test_lookbehind_flushes_safe_prefix_before_promise():
    """承诺前的无害前缀要正常下发（只截掉承诺段，不能把整轮都吞了）。"""
    prefix = "好的，您提供的预约信息已经收到，我马上为您安排办理相关手续，请稍等片刻哦。"
    model = FakeChatModel(
        [
            {
                # 前缀超过 LOOKBEHIND(40) 字，承诺分片后到；前缀必须先流出
                "chunks": [prefix, "已为您预约", "成功"],
                "turn": ChatTurn(text=prefix + "已为您预约成功"),
            },
            {
                "chunks": ["很抱歉，刚才的预约没有实际执行。"],
                "turn": ChatTurn(text="很抱歉，刚才的预约没有实际执行。"),
            },
        ]
    )
    engine, metrics = make_engine(model)
    text, _ = await collect(engine, "帮我预约神经内科")

    assert "您提供的预约信息已经收到" in text  # 无害前缀正常下发
    assert "已为您预约成功" not in text  # 承诺被截
    assert metrics.snapshot()["counters"]["false_confirmation_truncated"] == 1


async def test_lookbehind_conditional_context_is_not_truncated():
    """现行为特征（钉住，不是赞同）：承诺前 10 字窗口内含条件标记
    （"不"/"没有"/"是否"…）时承诺**不拦截** —— 防止拦腰截断
    "如果…还没预约成功…请…"这类指导语。

    副作用：前缀里带"不"字（如"不要走开"）也会放行承诺。
    重构（StreamFilter）必须原样保留该语义，不得顺手"修复"。
    """
    prefix = "好的，您提供的预约信息已经收到，我马上为您安排办理，请稍等片刻不要走开。"
    model = FakeChatModel(
        [
            {
                "chunks": [prefix, "已为您预约成功"],
                "turn": ChatTurn(text=prefix + "已为您预约成功"),
            },
            {
                "chunks": ["很抱歉，刚才的预约没有实际执行。"],
                "turn": ChatTurn(text="很抱歉，刚才的预约没有实际执行。"),
            },
        ]
    )
    engine, metrics = make_engine(model)
    text, _ = await collect(engine, "帮我预约神经内科")

    # "不要走开"的"不"落进承诺前 10 字窗口 → 条件语境 → 不截断（现行为）
    assert "已为您预约成功" in text
    counters = metrics.snapshot()["counters"]
    assert counters.get("false_confirmation_truncated", 0) == 0


# ---------- 2. _trim_to_sentence 切割边界 ----------


def test_trim_to_sentence_cuts_at_sentence_end():
    assert _trim_to_sentence("很遗憾，您在2026年11月11日上午。之后的安排") == (
        "很遗憾，您在2026年11月11日上午。"
    )


def test_trim_to_sentence_falls_back_to_clause():
    """没有句末标点时裁到最近逗号（保留标点），至少话说到一半是通的。"""
    assert _trim_to_sentence("很遗憾，您在上午查询") == "很遗憾，"


def test_trim_to_sentence_discards_when_no_boundary():
    """连逗号都没有 → 整段丢弃（半句话比不完整更糟）。"""
    assert _trim_to_sentence("没有任何标点的文本") == ""
    assert _trim_to_sentence("") == ""


# ---------- 3. 纠正重试至多一次 ----------


async def test_corrective_retry_happens_at_most_once():
    """轮1编造→纠正重试；轮2仍编造→兜底替换并结束，不再二次重试。"""
    model = FakeChatModel(
        [
            {"chunks": ["已为您预约成功"], "turn": ChatTurn(text="已为您预约成功")},
            {"chunks": ["您的挂号已经预约好了"], "turn": ChatTurn(text="您的挂号已经预约好了")},
        ]
    )
    engine, metrics = make_engine(model)
    text, _ = await collect(engine, "帮我预约神经内科")

    assert model.calls == 2  # 初次 + 一次纠正，没有第三次
    assert FALLBACK_BOOKING_NOT_DONE in text
    assert metrics.snapshot()["counters"]["hallucination_fallbacks"] == 1


# ---------- 4. 请求级时间预算 ----------


class SlowChatModel(FakeChatModel):
    """在指定调用次序（1 起）拖慢流式输出的假模型。"""

    def __init__(self, script, slow_call: int, delay: float = 0.3):
        super().__init__(script)
        self.slow_call = slow_call
        self.delay = delay

    async def stream_chat(self, messages, tools=None, result_holder=None):
        if self.calls + 1 == self.slow_call:
            await asyncio.sleep(self.delay)
        async for delta in super().stream_chat(messages, tools, result_holder):
            yield delta


async def test_request_timeout_degrades_with_budget_hint():
    """时间预算到期：先补发已生成内容，再追加提示并降级收口。

    预算检查在**轮顶**触发：轮 0、轮 1 都带工具调用（会继续下一轮），
    轮 1 的流式输出被拖慢 → 轮 2 顶部检查命中 → 追加提示并 break。
    """
    model = SlowChatModel(
        [
            # 轮0：叙述 + 工具调用（快）
            {
                "chunks": ["好的，我马上为您查询。"],
                "turn": ChatTurn(
                    text="好的，我马上为您查询。",
                    tool_calls=[ToolCall(id="1", name="查询是否有号源", arguments={})],
                ),
            },
            # 轮1：叙述 + 工具调用（拖慢 0.3s）
            {
                "chunks": ["仍在查询中。"],
                "turn": ChatTurn(
                    text="仍在查询中。",
                    tool_calls=[ToolCall(id="2", name="查询科室专家", arguments={})],
                ),
            },
            # 轮2 顶部预算到期，走不到这里
            {"chunks": ["不该出现"], "turn": ChatTurn(text="不该出现")},
        ],
        slow_call=2,
    )
    engine, metrics = make_engine(model, request_timeout=0.05)
    text, events = await collect(engine, "帮我查呼吸内科号源")

    # 已生成的内容全部下发，之后追加预算提示
    assert "好的，我马上为您查询。" in text
    assert "仍在查询中。" in text
    assert "不该出现" not in text
    assert "（响应已达时间上限，先返回已生成的内容。）" in text
    reasons = [
        e.metadata.get("reason")
        for e in events
        if e.event_type == EventType.DEGRADED
    ]
    assert "time_budget_exceeded" in reasons
    end = [e for e in events if e.event_type == EventType.AGENT_END][0]
    assert end.metadata["degraded"] is True
    assert metrics.snapshot()["counters"]["degraded_total"] == 1


# ---------- 5. 指标埋点 ----------


async def test_metric_narrated_tool_call_retries_increments():
    """叙述承诺（零工具调用）→ narrated_tool_call_retries 自增且逼真调一轮。"""
    narration = "好的，我将为您查询呼吸内科的号源。"
    model = FakeChatModel(
        [
            {"chunks": [narration], "turn": ChatTurn(text=narration)},
            {
                "turn": ChatTurn(
                    tool_calls=[ToolCall(id="1", name="查询是否有号源", arguments={})]
                )
            },
            {"chunks": ["呼吸内科上午已约满。"], "turn": ChatTurn(text="呼吸内科上午已约满。")},
        ]
    )
    engine, metrics = make_engine(model)
    text, _ = await collect(engine, "帮我预约呼吸内科")

    assert metrics.snapshot()["counters"]["narrated_tool_call_retries"] == 1
    assert "呼吸内科上午已约满。" in text


async def test_metric_textual_tool_call_truncated_increments():
    """纯文本 JSON 工具调用 → 下发前截断 + textual_tool_call_truncated 自增。"""
    textual = '预约挂号\n{"username": "张三", "id_card": "610121199401032831"}'
    model = FakeChatModel(
        [
            {"chunks": [textual], "turn": ChatTurn(text=textual)},
            # 纠正重试轮：结构化工具调用
            {
                "turn": ChatTurn(
                    tool_calls=[ToolCall(id="2", name="预约挂号", arguments={})]
                )
            },
            {"chunks": ["预约手续已办理。"], "turn": ChatTurn(text="预约手续已办理。")},
        ]
    )
    engine, metrics = make_engine(model)
    text, _ = await collect(engine, "帮我预约呼吸内科")

    assert metrics.snapshot()["counters"]["textual_tool_call_truncated"] == 1
    assert "610121199401032831" not in text  # 原始 JSON 参数一个字都不漏
    assert "预约挂号" not in text
    assert "预约手续已办理。" in text


# ---------- 6. 落库 JSON 结构快照 ----------


async def test_persisted_snapshot_narration_tool_final_rounds():
    """叙述轮+工具轮+最终轮 → 落库三段式结构整体快照。

    预期结构（第七轮设计 + 第十轮 final_reply_text 修正的最终形态）：
        [user 请求] → [assistant(tool_calls, 叙述段)] → [tool 结果]
        → [assistant(最终回复, outcome_meta)]
    叙述段不进最终回复、工具结果紧跟发起调用的消息、最终回复只含最后一轮。
    """
    narration = "好的，我马上为您查询呼吸内科的号源。"
    final = "呼吸内科2026-11-11上午已约满，建议改约其他时段。"
    model = FakeChatModel(
        [
            {
                "chunks": [narration],
                "turn": ChatTurn(
                    text=narration,
                    tool_calls=[
                        ToolCall(
                            id="c1",
                            name="查询是否有号源",
                            arguments={"department": "呼吸内科"},
                        )
                    ],
                ),
            },
            {"chunks": [final], "turn": ChatTurn(text=final)},
        ]
    )
    executor = FakeExecutor(ToolResult.ok("呼吸内科 2026-11-11 上午：约满"))
    engine, _ = make_engine(model, executor=executor)
    store = FakeStore()
    engine.conversation_store = store
    await collect(engine, "帮我预约呼吸内科")

    saved = store.saved
    # 整体快照：只允许新增 4 条（user / assistant(tc+叙述) / tool / assistant 最终）
    assert [m.role for m in saved] == [
        Role.USER,
        Role.ASSISTANT,
        Role.TOOL,
        Role.ASSISTANT,
    ]
    assert saved[0].content == "帮我预约呼吸内科"
    # 叙述段独立存进 assistant(tool_calls)，不混进最终回复
    assert saved[1].content == narration
    assert [c.name for c in saved[1].tool_calls] == ["查询是否有号源"]
    assert saved[1].tool_calls[0].id == "c1"
    # 工具结果紧跟发起调用的 assistant，call_id 配对
    assert saved[2].tool_call_id == "c1"
    assert saved[2].content == "呼吸内科 2026-11-11 上午：约满"
    # 最终回复只含最后一轮文本，且带 outcome 元数据
    assert saved[3].content == final
    assert saved[3].tool_calls == []
    # 序列校验：全部落库消息无问题
    assert all(m.validate() is None for m in saved)
