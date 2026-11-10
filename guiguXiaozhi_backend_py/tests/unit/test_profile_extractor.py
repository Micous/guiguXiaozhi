"""ProfileExtractor 单测 —— P2-M1。

重点：JSON schema 门控（解析失败/字段越界/畸形条目 → 整批丢弃）、
confidence 门槛、规则路确定性抽取、LLM 失败不影响主流程。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from xiaozhi.core.message import Message  # noqa: E402
from xiaozhi.profile.extractor import (  # noqa: E402
    CONFIDENCE_FLOOR,
    FactEntry,
    ProfileExtractor,
)


def user(text):
    return Message.user(text)


# ---------- 规则路 ----------


@pytest.mark.asyncio
async def test_rules_extract_id_card_name_slot_department():
    ex = ProfileExtractor()
    facts = await ex.extract(
        [
            user("王新勃，610121199401032831，帮我预约呼吸内科，上午"),
        ],
        authoritative_departments=["呼吸内科", "心内科"],
    )
    by_key = {f.key: f.value for f in facts}
    assert by_key.get("id_card") == "610121199401032831"
    # "王新勃，"没有显式姓名陈述词 → 不抽姓名（避免把任何词都当姓名）
    assert "name" not in by_key
    assert by_key.get("preferred_department") == "呼吸内科"
    assert by_key.get("preferred_time_slot") == "上午"


@pytest.mark.asyncio
async def test_rules_extract_explicit_name():
    ex = ProfileExtractor()
    facts = await ex.extract([user("我叫李小明，最近头痛")])
    by_key = {f.key: f.value for f in facts}
    assert by_key.get("name") == "李小明"


@pytest.mark.asyncio
async def test_department_not_in_authoritative_list_is_skipped():
    """名单外的泛称（"内科"）不进画像 —— 权威名单是铁律。"""
    ex = ProfileExtractor()
    facts = await ex.extract(
        [user("帮我挂内科")], authoritative_departments=["呼吸内科"]
    )
    assert not any(f.key == "preferred_department" for f in facts)


# ---------- LLM 路：JSON schema 门控 ----------


@pytest.mark.asyncio
async def test_llm_valid_facts_pass_gate():
    async def llm(prompt):
        return '{"facts": [{"key": "allergy", "value": "青霉素", "confidence": 0.95}]}'

    ex = ProfileExtractor(llm)
    facts = await ex.extract([user("我对青霉素过敏")])
    assert any(f.key == "allergy" and f.value == "青霉素" for f in facts)


@pytest.mark.asyncio
async def test_llm_malformed_json_discards_whole_batch():
    """解析失败 → 整批丢弃（宁缺勿错）。"""
    async def llm(prompt):
        return '好的，抽取结果如下：{"facts": [{"key": "allergy", "value": "青霉素"}]}'

    ex = ProfileExtractor(llm)
    facts = await ex.extract([user("我对青霉素过敏")])
    assert facts == []


@pytest.mark.asyncio
async def test_llm_out_of_schema_key_discards_whole_batch():
    """越界 key → 整批丢弃。"""
    async def llm(prompt):
        return '{"facts": [{"key": "bank_card", "value": "6222...", "confidence": 0.99}]}'

    ex = ProfileExtractor(llm)
    facts = await ex.extract([user("我的银行卡是6222...")])
    assert facts == []


@pytest.mark.asyncio
async def test_llm_low_confidence_dropped():
    async def llm(prompt):
        return (
            '{"facts": ['
            '{"key": "allergy", "value": "青霉素", "confidence": 0.95},'
            '{"key": "chronic_disease", "value": "高血压", "confidence": 0.5}'
            "]}"
        )

    ex = ProfileExtractor(llm)
    facts = await ex.extract([user("对青霉素过敏，可能有高血压")])
    keys = {f.key for f in facts}
    assert "allergy" in keys
    assert "chronic_disease" not in keys  # confidence < 0.7 丢弃
    assert CONFIDENCE_FLOOR == 0.7


@pytest.mark.asyncio
async def test_llm_exception_never_raises():
    """LLM 挂掉 → 返回规则路结果，绝不影响主流程。"""
    async def llm(prompt):
        raise RuntimeError("ollama down")

    ex = ProfileExtractor(llm)
    facts = await ex.extract(
        [user("王新勃，610121199401032831")], authoritative_departments=[]
    )
    assert any(f.key == "id_card" for f in facts)  # 规则路不受影响


def test_fact_entry_gate():
    assert not FactEntry(key="bank_card", value="x").is_valid()  # 越界 key
    assert not FactEntry(key="allergy", value="  ").is_valid()  # 空值
    assert not FactEntry(key="allergy", value="x", confidence=0.5).is_valid()
    assert FactEntry(key="allergy", value="青霉素", confidence=0.9).is_valid()
