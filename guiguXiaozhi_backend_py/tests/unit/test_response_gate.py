"""Phase 4 写操作 Response Gate 验收测试 —— 审计 P0-05 的 6 条用例逐条对号。

P0-05 验收（Python_Harness_生产化深度审计与分阶段整改设计方案.md）：
  1. 模型在首个 delta 就输出"已为您预约成功"，但没有工具调用：
     前端在核验前不得收到这句话；
  2. 工具执行失败：最终输出不得出现"预约成功"；
  3. 工具调用成功但 HIS 状态未知：输出"结果核实中"，不能显示成功；
  4. HIS 确认成功且本地持久化成功：才允许输出成功确认；
  5. 纠正重试后仍编造成功：由确定性 ResponseGate 拦截（历史残留场景：
     不带 token 重发同一句，模型复述"预约成功"——本次请求什么都没做）；
  6. 取消失败或超时：不得输出"已取消"。

外加 response_gate 纯函数（is_write_operation / curate_final_reply）的单测。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from xiaozhi.core.event import EventType  # noqa: E402
from xiaozhi.core.message import Message, Role, ToolCall  # noqa: E402
from xiaozhi.core.tool import ToolResult  # noqa: E402
from xiaozhi.models.ollama import ChatTurn  # noqa: E402
from xiaozhi.models.quality import (  # noqa: E402
    FALLBACK_BOOKING_NOT_DONE,
    FALLBACK_CANCEL_NOT_DONE,
)
from xiaozhi.ops.metrics import MetricsCollector  # noqa: E402
from xiaozhi.reliability.resilience import RetryConfig  # noqa: E402
from xiaozhi.runtime.response_gate import (  # noqa: E402
    UNKNOWN_NOTICE,
    curate_final_reply,
    is_write_operation,
)
from xiaozhi.runtime.state import TurnState  # noqa: E402

from test_engine_reliability import (  # noqa: E402
    FakeChatModel,
    FakeExecutor,
    FakeRegistry,
    FakeStore,
    FakeAssembler,
    collect,
)
from test_engine_characterization import make_engine  # noqa: E402


# ---------- is_write_operation ----------


def test_write_operation_detection():
    assert is_write_operation("帮我预约呼吸内科")
    assert is_write_operation("取消我的预约")
    # 含身份证号（补全信息的消息往往没有"预约"字样）
    assert is_write_operation("王新勃，610121199401032831，内科，2026-11-12上午")
    # 短确认句承接上一轮写操作提议
    assert is_write_operation("需要", last_assistant_text="要我帮您预约呼吸内科吗？")
    # 查询类不进写模式（读取类请求继续流式输出）
    assert not is_write_operation("帮我查呼吸内科2026-11-20上午有没有号源")
    assert not is_write_operation("呼吸内科有哪些专家")
    # 询问既有结果不是发起新写操作（可以引用历史事实）
    assert not is_write_operation("我的挂号成功了吗")
    assert not is_write_operation("你好")
    assert not is_write_operation("")


# ---------- curate_final_reply 纯函数 ----------


def _state(**kwargs) -> TurnState:
    state = TurnState(operation_request=True, write_operation=True)
    for key, value in kwargs.items():
        setattr(state, key, value)
    return state


def test_curate_unknown_replaces_with_fixed_notice():
    state = _state()
    state.record_write_result(
        "预约挂号",
        ToolResult.fail("结果核实中", error_type="UnknownState",
                        authoritative_status="UNKNOWN"),
    )
    curated, reason = curate_final_reply(state, "已为您预约成功！")
    assert curated == UNKNOWN_NOTICE
    assert reason == "unknown_write"


def test_curate_write_success_passes_model_text_through():
    state = _state()
    state.record_write_result("预约挂号", ToolResult.ok("预约成功。预约详情：…"))
    curated, reason = curate_final_reply(state, "已为您预约成功，医生徐国兴。")
    assert reason is None
    assert curated == "已为您预约成功，医生徐国兴。"


def test_curate_blocks_claim_when_no_write_executed():
    state = _state()
    curated, reason = curate_final_reply(state, "已为您预约成功！")
    assert reason == "claim_blocked:book"
    assert FALLBACK_BOOKING_NOT_DONE in curated
    assert "已为您预约成功" not in curated


def test_curate_passes_claim_free_text_without_write():
    """无写执行且无承诺（问科室/问日期）→ 放行，不误伤澄清对话"""
    state = _state()
    curated, reason = curate_final_reply(state, "请问您想预约哪一天？")
    assert reason is None
    assert curated == "请问您想预约哪一天？"


# ---------- 引擎级：P0-05 六条验收 ----------


async def test_acceptance_1_first_delta_claim_never_reaches_user():
    """验收1：首 delta 虚报成功且无工具调用 → 用户核验前收不到这句话。"""
    model = FakeChatModel(
        [
            {"chunks": ["已为您预约成功"], "turn": ChatTurn(text="已为您预约成功")},
            {
                "chunks": ["请问您想预约哪一天？"],
                "turn": ChatTurn(text="请问您想预约哪一天？"),
            },
        ]
    )
    engine, metrics = make_engine(model)
    text, _ = await collect(engine, "帮我预约神经内科")

    assert "已为您预约成功" not in text
    assert "预约成功" not in text  # 半截也不行
    assert metrics.snapshot()["counters"]["false_confirmation_truncated"] == 1


async def test_acceptance_2_tool_failure_never_announces_success():
    """验收2：工具执行失败 → 最终输出不得出现"预约成功"，兜底要说真实原因。"""
    model = FakeChatModel(
        [
            {
                "turn": ChatTurn(
                    tool_calls=[ToolCall(id="1", name="预约挂号", arguments={})]
                )
            },
            {"chunks": ["已为您预约成功"], "turn": ChatTurn(text="已为您预约成功")},
            {"chunks": ["预约成功了"], "turn": ChatTurn(text="预约成功了")},
        ]
    )
    executor = FakeExecutor(
        ToolResult.fail("该医生在您选择的日期与时段不出诊。", error_type="NoSlotAvailable")
    )
    engine, _metrics = make_engine(model, executor=executor)
    text, _ = await collect(engine, "帮我预约呼吸内科")

    assert "预约成功" not in text
    assert "预约成功了" not in text
    # 兜底文案反映工具的真实反馈（不是笼统的"没有生成记录"）
    assert "不出诊" in text or "没有完成" in text


async def test_acceptance_3_unknown_state_shows_fixed_notice_only():
    """验收3：HIS 状态未知 → 只输出"结果核实中，请勿重复提交"，不得显示成功。"""
    model = FakeChatModel(
        [
            {
                "turn": ChatTurn(
                    tool_calls=[ToolCall(id="1", name="预约挂号", arguments={})]
                )
            },
            {"chunks": ["已为您预约成功"], "turn": ChatTurn(text="已为您预约成功")},
            {"chunks": ["预约成功了"], "turn": ChatTurn(text="预约成功了")},
        ]
    )
    executor = FakeExecutor(
        ToolResult.fail(
            "结果核实中，请勿重复提交。",
            error_type="UnknownState",
            authoritative_status="UNKNOWN",
        )
    )
    engine, metrics = make_engine(model, executor=executor)
    text, events = await collect(engine, "帮我预约呼吸内科")

    assert "预约成功" not in text
    assert "预约成功了" not in text
    assert UNKNOWN_NOTICE in text
    # 门控确实出手了（确定性收口，不是模型自觉）
    assert metrics.snapshot()["counters"]["response_gate_blocks"] == 1
    reasons = [
        e.metadata.get("reason")
        for e in events
        if e.event_type == EventType.DEGRADED
    ]
    assert "unknown_write" in reasons


async def test_acceptance_4_success_announced_only_after_real_execution():
    """验收4：写操作真实执行成功 → 才允许成功确认（模型如实转述放行）。"""
    model = FakeChatModel(
        [
            {
                "chunks": ["好的，马上为您办理。"],
                "turn": ChatTurn(
                    text="好的，马上为您办理。",
                    tool_calls=[ToolCall(id="1", name="预约挂号", arguments={})],
                ),
            },
            {
                "chunks": ["已为您预约成功，医生徐国兴。"],
                "turn": ChatTurn(text="已为您预约成功，医生徐国兴。"),
            },
        ]
    )
    executor = FakeExecutor(ToolResult.ok("预约成功。预约详情：呼吸内科 2026-11-20 上午"))
    engine, metrics = make_engine(model, executor=executor)
    text, _ = await collect(engine, "帮我预约呼吸内科")

    assert "已为您预约成功" in text
    assert metrics.snapshot()["counters"].get("response_gate_blocks", 0) == 0


async def test_acceptance_5_replayed_request_claim_intercepted_deterministically():
    """验收5（真机残留场景）：不带 token 重发同一句，历史里真挂过号 →
    旧判据判真话、模型复述"预约成功"原样下发。写模式下 prior_booking
    不再为承诺背书 → 确定性拦截（截断 + 兜底替换），且**不进纠正重试**
    （prior_booking 分支直接 fallback，不会逼模型重复预约）。"""
    history = [
        Message.user("帮我预约呼吸内科 2026-11-12上午"),
        Message(
            role=Role.ASSISTANT,
            content="预约成功。医生：徐国兴。",
            metadata={"booking_executed": True},
        ),
    ]
    model = FakeChatModel(
        [
            {
                "chunks": ["已为您预约成功！"],
                "turn": ChatTurn(text="已为您预约成功！"),
            },
            {"chunks": ["不该出现"], "turn": ChatTurn(text="不该出现")},
        ]
    )
    engine, metrics = make_engine(model, store=FakeStore(history))
    text, _ = await collect(engine, "帮我预约呼吸内科 2026-11-12上午")

    assert "已为您预约成功" not in text
    assert "不该出现" not in text  # prior_booking 分支直接兜底，不再重试
    assert FALLBACK_BOOKING_NOT_DONE in text
    assert model.calls == 1


async def test_acceptance_6_cancel_failure_never_announces_cancelled():
    """验收6：取消失败 → 不得输出"已取消"，兜底按取消类文案。"""
    model = FakeChatModel(
        [
            {
                "turn": ChatTurn(
                    tool_calls=[ToolCall(id="1", name="取消预约挂号", arguments={})]
                )
            },
            {"chunks": ["已成功取消您的预约"], "turn": ChatTurn(text="已成功取消您的预约")},
            {"chunks": ["取消成功了"], "turn": ChatTurn(text="取消成功了")},
        ]
    )
    executor = FakeExecutor(
        ToolResult.fail("未找到可取消的预约记录。", error_type="NotFound")
    )
    engine, _metrics = make_engine(model, executor=executor)
    text, _ = await collect(engine, "帮我取消呼吸内科的预约")

    assert "取消您的预约" not in text
    assert "已成功取消" not in text
    assert "取消成功了" not in text
    assert FALLBACK_CANCEL_NOT_DONE in text


async def test_approval_short_circuit_bypasses_gate():
    """审批短路轮不走 gate（clean 文案本就是结构化结果），双下发绝不出现。"""
    model = FakeChatModel(
        [
            {
                "chunks": ["好的，马上为您办理。"],
                "turn": ChatTurn(
                    text="好的，马上为您办理。",
                    tool_calls=[ToolCall(id="1", name="预约挂号", arguments={})],
                ),
            },
        ]
    )
    executor = FakeExecutor(
        ToolResult.fail("该操作需要您确认后才会执行。", error_type="ApprovalRequired",
                        **{"approval": {"message": "请核对预约信息并确认。"}})
    )
    engine, metrics = make_engine(model, executor=executor)
    text, _ = await collect(engine, "帮我预约呼吸内科")

    assert text.count("请核对预约信息并确认。") == 1
    assert metrics.snapshot()["counters"].get("response_gate_blocks", 0) == 0


async def test_gate_replacement_never_enters_history():
    """被 gate 拦下的承诺文本绝不能进历史（进历史就是教模型照抄）。"""
    model = FakeChatModel(
        [
            {
                "turn": ChatTurn(
                    tool_calls=[ToolCall(id="1", name="预约挂号", arguments={})]
                )
            },
            {"chunks": ["已为您预约成功"], "turn": ChatTurn(text="已为您预约成功")},
            {"chunks": ["预约成功了"], "turn": ChatTurn(text="预约成功了")},
        ]
    )
    executor = FakeExecutor(
        ToolResult.fail(
            "结果核实中，请勿重复提交。",
            error_type="UnknownState",
            authoritative_status="UNKNOWN",
        )
    )
    engine, _metrics = make_engine(model, executor=executor)
    store = FakeStore()
    engine.conversation_store = store
    await collect(engine, "帮我预约呼吸内科")

    final = store.saved[-1]
    assert final.role == Role.ASSISTANT
    assert "预约成功" not in final.content
    assert UNKNOWN_NOTICE in final.content
