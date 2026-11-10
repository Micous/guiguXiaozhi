"""B1 联调缺陷回归：历史里的完成态回复会抑制后续操作的工具调用。

修复方式：操作类请求组装上下文时，把含"预约成功 / 已取消"断言的历史
assistant 回复替换成一句中性短句；普通历史原样保留。
（加前缀会被模型复述进回答，整条删除会诱发误调预约工具，实测均不可取。）
"""

from __future__ import annotations

from xiaozhi.core.message import Message, ToolCall
from xiaozhi.memory.context import (
    NEUTRAL_HISTORY_REPLY,
    AssembledContext,
    neutralize_history_reply,
)
from xiaozhi.runtime.intent import is_operation_request


def _context() -> AssembledContext:
    return AssembledContext(
        system_prompt="系统提示",
        history=[
            Message.user("帮我预约神经内科"),
            Message.assistant(
                "您的预约已经成功啦！预约详情：神经内科 / 2026-11-13 / 上午"
            ),
        ],
    )


def test_operation_request_replaces_completion_claim_history():
    messages = _context().to_messages(
        "请帮我取消刚才的预约", neutralize_history=True
    )
    assert messages[0] == {"role": "system", "content": "系统提示"}
    assert messages[1]["role"] == "user"
    assert messages[1]["content"] == "帮我预约神经内科"  # user 历史原样保留
    assert messages[2]["role"] == "assistant"
    assert messages[2]["content"] == NEUTRAL_HISTORY_REPLY
    assert "成功" not in messages[2]["content"]
    assert messages[-1]["content"] == "请帮我取消刚才的预约"  # 当前输入不变


def test_normal_question_keeps_history_plain():
    messages = _context().to_messages("头痛挂什么科", neutralize_history=False)
    assert "已经成功啦" in messages[2]["content"]


def test_non_completion_history_is_preserved():
    """分导诊建议这类不含完成态断言的历史不该被改写。"""
    ctx = AssembledContext(
        system_prompt="s",
        history=[Message.assistant("建议挂神经内科，注意休息。")],
    )
    messages = ctx.to_messages("帮我预约", neutralize_history=True)
    assert messages[1]["content"] == "建议挂神经内科，注意休息。"


def test_no_prefix_leaks_into_history():
    messages = _context().to_messages("取消预约", neutralize_history=True)
    assert not messages[2]["content"].startswith("【")
    assert "不代表本次请求" not in messages[2]["content"]


def test_tool_call_history_is_neutralized_too():
    call = ToolCall(id="1", name="预约挂号", arguments={"username": "张三"})
    ctx = AssembledContext(
        system_prompt="s",
        history=[Message.assistant("已成功取消预约", tool_calls=[call])],
    )
    messages = ctx.to_messages("取消预约", neutralize_history=True)
    assert messages[1]["content"] == NEUTRAL_HISTORY_REPLY
    assert messages[1]["tool_calls"][0]["function"]["name"] == "预约挂号"


def test_neutralize_helper_covers_booking_and_cancellation():
    for text in (
        "您的预约已经成功啦！",
        "预约成功，医生王笠",
        "您的预约已成功取消。",
        "已为您取消预约",
        "取消成功",
    ):
        assert neutralize_history_reply(text) == NEUTRAL_HISTORY_REPLY, text
    assert neutralize_history_reply("建议挂神经内科") == "建议挂神经内科"


def test_intent_switch_matches_operation_requests():
    assert is_operation_request("请帮我取消预约")
    assert is_operation_request("神经内科有哪些专家")
    assert not is_operation_request("头痛应该挂什么科")
    assert not is_operation_request("抽血要空腹吗")
