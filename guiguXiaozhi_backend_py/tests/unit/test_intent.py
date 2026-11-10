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


def test_plain_questions_are_not_operation():
    for msg in (
        "你好",
        "医院地址在哪里",
        "头痛挂什么科",
        "抽血检查需要空腹吗",
        "今天天气怎么样",
    ):
        assert not is_operation_request(msg), msg
        assert should_inject_knowledge(msg)


def test_existing_operation_keywords_still_match():
    for msg in ("预约挂号", "取消预约", "帮我查一下号源", "神经内科有哪些专家"):
        assert is_operation_request(msg), msg


def test_date_like_numbers_do_not_trigger():
    """日期、电话号不应误判为操作类"""
    assert not is_operation_request("2026-11-20 上午门诊几点开始？")
    assert not is_operation_request("请给我拨打电话13800138000咨询")
