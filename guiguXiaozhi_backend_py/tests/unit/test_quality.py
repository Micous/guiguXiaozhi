"""模型输出质量门控单测（纯函数，不依赖外部服务）。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from xiaozhi.core.message import Message, Role, ToolCall  # noqa: E402
from xiaozhi.models.ollama import ChatTurn  # noqa: E402
from xiaozhi.models.quality import (  # noqa: E402
    CORRECTIVE_PROMPT,
    FALLBACK_BOOKING_NOT_DONE,
    FALLBACK_CANCEL_NOT_DONE,
    fallback_for,
    FALSE_CONFIRMATION_NOTICE,
    booking_executed_in_history,
    claim_span,
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


# ---- 纠正重试（2026-11-10 真实前端联调新增） ----

def test_booking_executed_in_history():
    """历史里真的**成功执行过**预约（有 metadata 标记）→ "已经预约好了"是真话"""
    assert not booking_executed_in_history([])
    assert not booking_executed_in_history(None)

    booked = [
        Message(
            role=Role.ASSISTANT,
            content="预约成功",
            tool_calls=[ToolCall(id="1", name="预约挂号", arguments={})],
            metadata={"booking_executed": True},
        )
    ]
    assert booking_executed_in_history(booked)

    # 预约后又取消 → 不算已预约
    booked_then_cancelled = booked + [
        Message(
            role=Role.ASSISTANT,
            content="已取消",
            tool_calls=[ToolCall(id="2", name="取消预约挂号", arguments={})],
            metadata={"cancel_executed": True},
        )
    ]
    assert not booking_executed_in_history(booked_then_cancelled)

    # 普通文本历史不含标记 → False
    assert not booking_executed_in_history([Message.assistant("已为您预约成功")])


def test_failed_booking_calls_in_history_do_not_suppress_gate():
    """2026-11-10 事故回归：失败的工具调用不该被当成"已预约"。

    旧实现只看 tool_calls —— 失败的预约调用也把 prior_booking 置 True，
    幻觉检测被整体抑制，模型谎报"预约已经成功"畅通无阻。
    新实现只认 engine 打的成功标记，无标记一律视为未预约（宁可信其无）。
    """
    failed_booking_history = [
        Message.user("内科，王新勃，610121199401032831，2026.11.11，上午"),
        Message.assistant(
            "您好，经过查询…您的预约已经成功。",
            tool_calls=[ToolCall(id="c1", name="预约挂号", arguments={"date": {"date": "2026-11-11"}})],
        ),
        Message.user("预约成功了吗？有没有预约信息"),
        Message.assistant("您好，我为您预约了2026年11月11日上午的内科…"),
    ]
    assert not booking_executed_in_history(failed_booking_history)


def test_new_completion_claim_phrasings_detected():
    """「我为您预约了…以下是预约信息」这类不带"成功/已"的完成态声明也要拦。"""
    for text in (
        "您好，我为您预约了2026年11月11日上午的内科，以下是您的预约信息",
        "已经帮您预约了神经内科门诊",
        "您的预约手续都预约好了，请按时就诊",
        "您好，经过查询，您的预约已经成功",
    ):
        assert detect_false_confirmation(text), text


def test_completion_claim_questions_not_flagged():
    """疑问句不是完成态声明，不能触发纠正重试。"""
    for text in (
        "您是想问预约好了吗？",
        "等您预约好了再告诉我",
        "需要我为您预约吗？",
        "我可以帮您预约，请提供信息",
    ):
        assert not detect_false_confirmation(text), text


def test_corrective_prompt_exists():
    """纠正提示必须点名具体工具、且禁止输出结论性文字。

    旧措辞「不得再声称成功」会让 7B 模型顺着这句话再编一遍
    （它会重复"你声称成功"这句话），实测纠正重试完全无效。
    """
    assert "工具" in CORRECTIVE_PROMPT
    assert "预约挂号" in CORRECTIVE_PROMPT
    assert "不要输出任何结论性文字" in CORRECTIVE_PROMPT


def test_claim_span_locates_earliest_claim():
    """流式截断需要知道承诺在文本中的位置，且取**最早**出现的那一处"""
    span = claim_span("您好，经过查询，我为您预约了2026年11月11日上午的神经内科")
    assert span is not None
    kind, start, end = span
    assert kind == "book"
    assert "为您预约了" in "您好，经过查询，我为您预约了2026年11月11日上午的神经内科"[start:end]

    cancel = claim_span("您的预约已成功取消")
    assert cancel is not None and cancel[0] == "cancel"


def test_fallback_wording_never_claims_success():
    for text in (FALLBACK_BOOKING_NOT_DONE, FALLBACK_CANCEL_NOT_DONE):
        assert "成功" not in text.replace("没有办理成功", "").replace("没有执行成功", "")


def test_fallback_reflects_real_tool_failure():
    """兜底文案要反映工具的真实反馈，不能笼统说"没有生成记录"。

    用户明明已经约过号（工具返回 DuplicateAppointment）时说"没有生成记录"
    同样是误导。
    """
    failure = {
        "tool": "预约挂号",
        "error_type": "DuplicateAppointment",
        "content": "您在神经内科 2026-11-11 上午已有预约（医生：王笠），请勿重复预约。",
    }
    text = fallback_for("book", failure)
    assert "已经有一条预约记录" in text
    assert "没有生成对应的预约记录" not in text

    slot_failure = {
        "tool": "预约挂号",
        "error_type": "NoSlotAvailable",
        "content": "医生【王笠】在 2026-11-11 上午不出诊…",
    }
    text2 = fallback_for("book", slot_failure)
    assert "不出诊" in text2 or "约满" in text2

    # 没有工具失败信息时用通用文案
    assert fallback_for("book", None) == FALLBACK_BOOKING_NOT_DONE
    assert fallback_for("cancel", None) == FALLBACK_CANCEL_NOT_DONE


# ---- 编造查询结果检测（2026-11-10 第五轮联调新增） ----

def test_query_result_claim_is_detected_with_kind_query():
    """没调工具却报"查询结果显示…" → kind=query，流式截断依据"""
    span = claim_span("好的，查询结果显示，内科暂时没有可预约的号源。")
    assert span is not None and span[0] == "query"
    assert claim_span("查询结果：神经内科有号源")[0] == "query"
    assert claim_span("神经内科的号源已约满")[0] == "query"


def test_query_question_is_not_a_claim():
    """疑问/条件语境不能误伤："有没有号源吗"里包含"没有(可预约的)号源"的近形"""
    for text in (
        "请问您想查询内科有没有号源吗？",
        "您是想问神经内科有没有号源吗？",
        "如果号源已约满，可以换个时段。",
    ):
        assert claim_span(text) is None, text


def test_query_corrective_uses_action_prompt():
    from xiaozhi.models.quality import corrective_prompt

    assert corrective_prompt("query") == corrective_prompt("action")
    assert "工具" in corrective_prompt("query")


def test_query_fallback_reflects_no_execution():
    from xiaozhi.models.quality import FALLBACK_QUERY_NOT_EXECUTED

    assert fallback_for("query", None) == FALLBACK_QUERY_NOT_EXECUTED
    assert "没有实际执行" in FALLBACK_QUERY_NOT_EXECUTED


def test_narrated_query_promise_is_detected():
    """「我将查询…号源」这种口头承诺也要被 narrated 检测抓住"""
    from xiaozhi.models.quality import detect_narrated_tool_call

    assert detect_narrated_tool_call("好的，我将查询2026年11月11日内科是否有号源。")
    assert not detect_narrated_tool_call("建议您挂神经内科，注意休息。")
