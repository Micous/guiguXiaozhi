"""模型输出质量门控单测（纯函数，不依赖外部服务）。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from xiaozhi.core.message import ToolCall  # noqa: E402
from xiaozhi.models.ollama import ChatTurn  # noqa: E402
from xiaozhi.models.quality import (  # noqa: E402
    FALSE_CONFIRMATION_NOTICE,
    detect_false_confirmation,
    detect_textual_tool_call,
    inspect_turn,
    is_empty_turn,
    strip_textual_tool_call,
)


def test_empty_turn_is_detected():
    finding = inspect_turn(ChatTurn())
    assert finding.empty
    assert finding.needs_retry
    assert not finding.ok
    assert "空响应" in finding.reasons[0]


def test_normal_answer_passes():
    finding = inspect_turn(ChatTurn(text="建议挂神经内科，注意休息。"))
    assert finding.ok
    assert not finding.needs_retry


def test_false_confirmation_detected_without_tool_call():
    finding = inspect_turn(ChatTurn(text="已为您预约成功，请准时就诊。"))
    assert finding.false_confirmation
    assert "预约" in finding.reasons[-1]
    assert FALSE_CONFIRMATION_NOTICE.strip()


def test_no_false_positive_when_tool_was_called():
    call = ToolCall(id="1", name="预约挂号", arguments={"username": "张三"})
    finding = inspect_turn(ChatTurn(text="已为您预约成功", tool_calls=[call]))
    assert not finding.false_confirmation
    assert detect_false_confirmation("已为您预约成功", has_tool_calls=True) is False


def test_textual_tool_call_detected():
    text = '预约挂号\n{"username": "张三"}'
    assert detect_textual_tool_call(text)
    assert inspect_turn(ChatTurn(text=text)).textual_tool_call
    assert strip_textual_tool_call("好的" + text) == "好的"
    assert "预约挂号" not in strip_textual_tool_call(text)


def test_unregistered_tool_and_bad_arguments():
    call = ToolCall(id="1", name="不存在的工具", arguments="not-a-dict")
    finding = inspect_turn(ChatTurn(tool_calls=[call]), ["预约挂号"])
    assert any("未注册" in reason for reason in finding.invalid_tool_calls)
    assert any("参数不是对象" in reason for reason in finding.invalid_tool_calls)
    assert not finding.empty  # 有工具调用就不算空响应


def test_is_empty_turn_helper():
    assert is_empty_turn("  ", [])
    assert not is_empty_turn("", [ToolCall(id="1", name="x", arguments={})])
    assert not is_empty_turn("内容", [])


# ---- B1 联调发现的两个缺陷：取消措辞漏报、工具已执行后的总结轮误报 ----

def test_cancel_confirmation_phrasings_are_detected():
    for text in (
        "您的预约已成功取消。",
        "已成功取消预约",
        "已经为您取消了预约",
        "取消预约成功",
        "成功取消了您的挂号",
    ):
        assert detect_false_confirmation(text), text
        assert inspect_turn(ChatTurn(text=text)).false_confirmation, text


def test_cancel_intent_is_not_treated_as_confirmation():
    for text in (
        "我想取消预约",
        "如何取消预约？",
        "取消预约需要本人身份证吗",
        "已收到您要取消预约的请求，请稍等",
    ):
        assert not detect_false_confirmation(text), text


def test_tool_executed_suppresses_false_positive_on_summary_turn():
    """工具执行后模型会再生成一轮没有 tool_calls 的总结文本，不该被判成幻觉。"""
    assert detect_false_confirmation("预约成功，详情如下", tool_executed=True) is False
    assert detect_false_confirmation("您的预约已成功取消", tool_executed=True) is False
    assert detect_false_confirmation("您的预约已成功取消", tool_executed=False) is True
    finding = inspect_turn(ChatTurn(text="已为您预约成功"), tool_executed=True)
    assert not finding.false_confirmation
    assert finding.ok
