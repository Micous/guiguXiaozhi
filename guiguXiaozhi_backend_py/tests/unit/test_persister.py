"""ConversationPersister 单测 —— E5 拆分后落库组装的独立覆盖。

快照基线 = 第七轮三段式设计 + 第十轮 final_reply_text 修正
（与 test_engine_characterization 的端到端落库快照互为印证）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from xiaozhi.core.message import Message, Role, ToolCall  # noqa: E402
from xiaozhi.runtime.persister import ConversationPersister  # noqa: E402
from xiaozhi.runtime.state import TurnState  # noqa: E402


def build(state=None, tool_calls=None, fallback_text="", history=None):
    state = state or TurnState()
    return ConversationPersister().build(
        history or [], "帮我预约呼吸内科", state, tool_calls or [],
        fallback_text=fallback_text,
    )


def test_three_segment_structure_with_tool_calls():
    """叙述轮 + 工具结果 + 最终回复的三段式快照。"""
    state = TurnState()
    state.set_narration("好的，我马上为您查询。")
    state.set_final_reply("呼吸内科上午已约满。")
    call = ToolCall(id="c1", name="查询是否有号源", arguments={})
    state.add_tool_result(
        Message.tool_result("上午约满", "c1", "查询是否有号源")
    )

    msgs = build(state, tool_calls=[call])

    assert [m.role for m in msgs] == [
        Role.USER, Role.ASSISTANT, Role.TOOL, Role.ASSISTANT,
    ]
    assert msgs[1].content == "好的，我马上为您查询。"
    assert msgs[1].tool_calls == [call]
    assert msgs[2].tool_call_id == "c1"
    assert msgs[3].content == "呼吸内科上午已约满。"
    assert msgs[3].tool_calls == []


def test_text_only_request_persists_final_round_only():
    """纯文本请求：叙述段（纠正重试前轮）不进历史，只落最后一轮。"""
    state = TurnState()
    state.set_final_reply("抱歉，本次预约没有办理成功。")

    msgs = build(state, fallback_text="第一轮幻觉文本\n\n抱歉，本次预约没有办理成功。")

    assert [m.role for m in msgs] == [Role.USER, Role.ASSISTANT]
    assert msgs[1].content == "抱歉，本次预约没有办理成功。"
    # final_reply_text 优先于跨轮累积的 fallback_text
    assert "第一轮幻觉文本" not in msgs[1].content


def test_final_reply_falls_back_to_streamed_text_when_empty():
    """final_reply_text 为空（轮数耗尽全是工具轮）→ 退回流式累积文本。"""
    state = TurnState()
    state.set_narration("查询中……")
    call = ToolCall(id="c1", name="查询是否有号源", arguments={})

    msgs = build(state, tool_calls=[call], fallback_text="查询中……超时。")

    closing = msgs[-1]
    assert closing.role == Role.ASSISTANT
    assert closing.content == "查询中……超时。"


def test_outcome_meta_flags_written_on_closing_message():
    state = TurnState()
    state.set_final_reply("预约手续已办结。")
    state.record_tool_success("预约挂号")

    msgs = build(state, fallback_text="预约手续已办结。")

    assert msgs[-1].metadata == {"booking_executed": True}


def test_empty_closing_text_omitted_for_tool_calls():
    """有工具调用但没有最终回复文本 → 不追加空 closing 消息。"""
    state = TurnState()
    state.set_narration("查询中……")
    call = ToolCall(id="c1", name="查询是否有号源", arguments={})
    state.add_tool_result(Message.tool_result("结果", "c1", "查询是否有号源"))

    msgs = build(state, tool_calls=[call], fallback_text="  ")

    assert [m.role for m in msgs] == [Role.USER, Role.ASSISTANT, Role.TOOL]


def test_history_preserved_and_new_entries_validated():
    """历史原样保留在前，新增消息全部通过序列校验。"""
    history = [Message.user("之前的对话"), Message(role=Role.ASSISTANT, content="好的")]
    state = TurnState()
    state.set_final_reply("新的回答。")

    msgs = build(state, fallback_text="新的回答。", history=history)

    assert msgs[:2] == history
    assert all(m.validate() is None for m in msgs[2:])
