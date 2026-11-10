"""意图识别单测 —— 操作类请求必须跳过 RAG 注入。

背景（2026-11-10 真实前端联调）：用户按模型追问补全挂号信息时，消息里
只有"姓名，身份证号…，日期"，没有"预约"字样，被当成知识问答 →
注入知识 + 不中性化历史 → 模型口头调用工具、编造预约成功。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from xiaozhi.runtime.intent import (  # noqa: E402
    is_operation_request,
    is_short_confirmation,
    is_status_inquiry,
    should_inject_knowledge,
)


def test_id_card_message_is_operation():
    """补全挂号信息（含身份证号）必须视为操作类，不注入知识"""
    msg = "王新勃，身份证号610121199401032834，2026-11-20下午，张伟"
    assert is_operation_request(msg)
    assert not should_inject_knowledge(msg)


def test_15_digit_id_card_is_operation():
    msg = "姓名测试，身份证号110101900101123，明天上午"
    assert is_operation_request(msg)


def test_recommend_doctor_is_operation():
    """推荐专家应路由到查询科室专家工具，而不是 RAG 答题"""
    for msg in ("你推荐一位专家", "帮我推荐一位神经内科医生", "消化科医生推荐"):
        assert is_operation_request(msg), msg


def test_department_existence_question_is_operation():
    """问"本院有没有某科室"必须按操作类处理（第八轮事故，2026-11-10）。

    "有内科吗"旧正则漏判 → 注入 RAG → 模型拿通用医学知识答
    "内科是一个较大的科室，包含了多个亚专科"——本院根本没有"内科"。
    """
    for msg in ("有内科吗？", "有内科吗", "你们医院有内科吗", "有没有儿科", "是否有康复科"):
        assert is_operation_request(msg), msg
        assert not should_inject_knowledge(msg), msg


def test_department_smalltalk_not_operation():
    """含"科"的闲聊/比较句不应误判成操作类"""
    for msg in ("有的科室比较偏", "内科和心内科有什么区别？", "怎么选择科室", "我有空吗"):
        assert not is_operation_request(msg), msg


def test_plain_questions_are_not_operation():
    for msg in (
        "医院地址在哪里",
        "头痛挂什么科",
        "抽血检查需要空腹吗",
        "今天天气怎么样",
    ):
        assert not is_operation_request(msg), msg
        assert should_inject_knowledge(msg), msg


def test_greeting_does_not_inject_knowledge():
    """纯问候不注入 RAG（2026-11-10 联调："你好"命中科室知识块，
    模型对着打招呼输出一大段科室分诊科普，答非所问）。"""
    from xiaozhi.runtime.intent import is_greeting

    for msg in ("你好", "您好！", "你好呀", "嗨", "在吗", "早上好", "hi"):
        assert is_greeting(msg), msg
        assert not should_inject_knowledge(msg), msg
    # 带实质内容的消息不是纯问候，照常走知识/操作判定
    for msg in ("你好，我想预约内科", "你好呀，头痛挂什么科", "大家好"):
        assert not is_greeting(msg), msg


def test_existing_operation_keywords_still_match():
    for msg in ("预约挂号", "取消预约", "帮我查一下号源", "神经内科有哪些专家"):
        assert is_operation_request(msg), msg


def test_date_like_numbers_do_not_trigger():
    """日期、电话号不应误判为操作类"""
    assert not is_operation_request("2026-11-20 上午门诊几点开始？")
    assert not is_operation_request("请给我拨打电话13800138000咨询")


def test_short_confirmation_recognized():
    """短确认句是"承接上一轮操作提议"的关键信号"""
    for msg in ("需要", "好的", "好", "行", "嗯", "是的", "可以", "确认", "麻烦你了", "ok"):
        assert is_short_confirmation(msg), msg
    for msg in ("我需要挂号", "帮我预约神经内科的号", "好的谢谢再见"):
        assert not is_short_confirmation(msg), msg


def test_confirmation_after_operation_offer_is_operation():
    """助手问"是否需要我帮您预约"，用户回"需要" → 必须按操作类处理。

    2026-11-10 真实前端联调：这两个字不含任何操作关键词，纯文本正则漏判 →
    注入知识 + 历史不中性化 → 模型照抄历史里的"预约成功啦"，工具一次没调。
    """
    offer = "目前可预约的医生有：王笠、崔丽英。请问您是否需要我帮您预约其中一位医生？"
    assert is_operation_request("需要", offer)
    assert not should_inject_knowledge("需要", offer)


def test_confirmation_after_knowledge_answer_is_not_operation():
    """上一轮是知识问答时，"好的"不应被误判成操作类"""
    answer = "北京协和医院位于北京市东城区帅府园一号，门诊时间为周一至周五。"
    assert not is_operation_request("好的", answer)
    assert should_inject_knowledge("好的", answer)


def test_status_inquiry_detected():
    """询问既有预约结果 ≠ 发起新操作"""
    for msg in (
        "挂号成功了吗？",
        "我的预约还在吗",
        "预约结果怎么样",
        "取消成功了吗",
        "我的预约记录",
    ):
        assert is_status_inquiry(msg), msg
    for msg in ("帮我预约神经内科", "预约挂号", "取消预约"):
        assert not is_status_inquiry(msg), msg
