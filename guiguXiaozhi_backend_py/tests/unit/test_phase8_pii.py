"""Phase 8-A：敏感信息掩码与落库收口（P0-06 前半）。

审计 P0-06 的原始证据是「工具返回文本里回显完整身份证号」。
本轮把口径统一到 ``security/masking.py``，并在三个泄露面上收口：

1. 工具结果 → 模型 → 用户（``_describe`` 打码）；
2. 参数校验失败文案（原来把用户输错的身份证号原样回显）；
3. 历史落库（``ConversationPersister`` 对模型产出的文本与 tool_call 参数打码）。

**刻意保留**：用户自己那条消息的 ``content`` 不打码 —— 那是用户亲口说的话，
改写它就等于篡改对话记录，且会让"取消刚才那个预约"失去上下文。
"""

from __future__ import annotations

import pytest

from xiaozhi.core.message import Message, Role, ToolCall
from xiaozhi.runtime.persister import ConversationPersister
from xiaozhi.runtime.state import TurnState
from xiaozhi.security.masking import (
    mask_id_card,
    mask_phone,
    mask_pii_in_text,
    mask_sensitive_args,
    mask_sensitive_value,
)
from xiaozhi.security.validators import validate_id_card

ID_CARD = "610121199401032831"  # 过 GB 11643 校验位
ID_CARD_MASKED = "610121********2831"


# --------------------------------------------------------------------------
# 基础变换
# --------------------------------------------------------------------------


def test_mask_id_card_keeps_region_and_last_four():
    assert mask_id_card(ID_CARD) == ID_CARD_MASKED


def test_mask_id_card_handles_15_digit_legacy():
    assert mask_id_card("610121199401032") == "610121*****1032"


def test_mask_id_card_short_value_is_fully_masked():
    assert mask_id_card("123") == "***"
    assert mask_id_card("") == ""


def test_mask_id_card_lowercase_x_is_preserved():
    masked = mask_id_card("61012119940103283x")
    assert masked.endswith("283x")
    assert "610121" == masked[:6]


def test_mask_phone():
    assert mask_phone("13800138000") == "138****8000"


def test_mask_pii_in_text_replaces_all_occurrences():
    text = f"患者 {ID_CARD}，联系电话 13800138000，已登记。"
    masked = mask_pii_in_text(text)
    assert ID_CARD not in masked
    assert "13800138000" not in masked
    assert "患者" in masked and "已登记" in masked
    assert ID_CARD_MASKED in masked


def test_mask_pii_in_text_is_idempotent():
    once = mask_pii_in_text(f"身份证 {ID_CARD}")
    assert mask_pii_in_text(once) == once


def test_mask_pii_in_text_does_not_touch_short_numbers():
    text = "预约日期 2026-11-20，时段上午"
    assert mask_pii_in_text(text) == text


def test_mask_pii_in_text_handles_non_chinese_boundary():
    """中文与数字之间没有 \\b 词边界 —— 必须能匹配紧贴中文的号码。"""
    assert mask_pii_in_text(f"身份证号{ID_CARD}已核验") == f"身份证号{ID_CARD_MASKED}已核验"


def test_mask_sensitive_value_only_masks_sensitive_keys():
    assert mask_sensitive_value("id_card", ID_CARD) == ID_CARD_MASKED
    assert mask_sensitive_value("身份证", ID_CARD) == ID_CARD_MASKED
    assert mask_sensitive_value("department", "神经内科") == "神经内科"
    # 非字符串值原样返回（盲目打码会改类型）
    assert mask_sensitive_value("id_card", 12345) == 12345


def test_mask_sensitive_args_does_not_mutate_input():
    args = {"id_card": ID_CARD, "department": "神经内科"}
    preview = mask_sensitive_args(args)
    assert preview["id_card"] == ID_CARD_MASKED
    assert args["id_card"] == ID_CARD  # 入参不被改写
    assert preview["department"] == "神经内科"


def test_mask_sensitive_args_keeps_approval_display_contract():
    """审批弹窗的展示口径不能变（前端依赖这个形状）。"""
    assert mask_sensitive_args({"phone": "13800138000"})["phone"] == "138001*8000"


# --------------------------------------------------------------------------
# 校验文案不再回显原值
# --------------------------------------------------------------------------


def test_invalid_id_card_message_does_not_echo_value():
    bad = "610121199401032830"  # 校验位错
    result = validate_id_card(bad)
    assert not result.ok
    assert bad not in result.message()
    assert "校验位" in result.message()


def test_malformed_id_card_message_does_not_echo_value():
    bad = "6101211994010"
    result = validate_id_card(bad)
    assert not result.ok
    assert bad not in result.message()


# --------------------------------------------------------------------------
# 工具层：回给模型的详情必须打码
# --------------------------------------------------------------------------


def test_book_tool_result_hides_full_id_card():
    """端到端最小复现：预约成功后工具返回给模型的文本不含完整身份证号。"""
    import asyncio

    from tests.unit.test_appointment_tools import await_call, make_tool

    tool, _repo, _slot = make_tool()
    result = await_call(
        tool,
        {
            "username": "张三",
            "id_card": ID_CARD,
            "department": "神经内科",
            "date": "2026-11-20",
            "time": "上午",
            "doctor_name": "王笠",
        },
    )
    assert result.success
    assert ID_CARD not in result.content
    assert ID_CARD_MASKED in result.content
    assert asyncio.iscoroutine  # 保持 import 有用（避免 lint 抱怨）


# --------------------------------------------------------------------------
# 落库：模型产出打码，用户原文保留
# --------------------------------------------------------------------------


def _state_with_tool_call(tool_arguments, narrative="好的，正在为您办理。"):
    state = TurnState()
    state.narrative_text = narrative
    from xiaozhi.core.tool import ToolResult

    state.persisted_tool_results = [
        Message(
            role=Role.TOOL,
            content=f"预约成功。身份证号：{ID_CARD}",
            tool_name="预约挂号",
            tool_call_id="call-1",
        )
    ]
    state.tool_results = [ToolResult.ok("ok")]
    return state


def test_persister_masks_tool_call_arguments():
    state = _state_with_tool_call({"id_card": ID_CARD})
    persister = ConversationPersister()
    messages = persister.build(
        history=[],
        user_input="帮我预约",
        state=state,
        tool_calls=[ToolCall(id="call-1", name="预约挂号", arguments={"id_card": ID_CARD, "department": "神经内科"})],
        fallback_text="预约成功",
    )
    tool_call_msg = next(m for m in messages if m.tool_calls)
    assert tool_call_msg.tool_calls[0].arguments["id_card"] == ID_CARD_MASKED


def test_persister_masks_assistant_text_but_keeps_user_text():
    state = _state_with_tool_call({})
    persister = ConversationPersister()
    messages = persister.build(
        history=[],
        user_input=f"我叫张三，身份证 {ID_CARD}",
        state=state,
        tool_calls=[],
        fallback_text=f"已记录身份证 {ID_CARD}",
    )
    user_msg = messages[0]
    assert ID_CARD in user_msg.content  # 用户原文不动
    assistant_msg = messages[-1]
    assert ID_CARD not in assistant_msg.content
    assert ID_CARD_MASKED in assistant_msg.content


def test_persister_does_not_mutate_original_messages():
    """打码必须产生新对象 —— trace/事件里还握着原始 Message。"""
    original = Message(role=Role.ASSISTANT, content=f"身份证 {ID_CARD}")
    persister = ConversationPersister()
    masked = persister.mask_new_entries([], [original])
    assert ID_CARD in original.content
    assert ID_CARD not in masked[0].content
    assert masked[0] is not original


@pytest.mark.parametrize("text", ["", "没有敏感信息的一段话"])
def test_persister_returns_same_object_when_nothing_to_mask(text):
    original = Message(role=Role.ASSISTANT, content=text)
    persister = ConversationPersister()
    masked = persister.mask_new_entries([], [original])
    assert masked[0] is original


# ==========================================================================
# Phase 8-B：落库侧加密 / 指纹 / 掩码
# ==========================================================================

from xiaozhi.repositories.appointment_repo import (  # noqa: E402
    Appointment,
    AppointmentStatus,
    MySQLAppointmentRepository,
)
from xiaozhi.security.pii import (  # noqa: E402
    CIPHER_PREFIX,
    PiiCodec,
    PiiConfigError,
    build_pii_codec,
    is_ciphertext,
)

KEY_A = "w07yDAOyFvLRMKFOyjh9qEGr02tdkt3VWldlrMOh-ac="
KEY_B = "Zx3nQ8bY6mC1vT5rW9pL2kJ7hG4fD0sA6yU8iO3eK1c="


def _codec(key: str = KEY_A) -> PiiCodec:
    return PiiCodec(key)


def test_fingerprint_is_deterministic_and_keyed():
    a, b = _codec(KEY_A), _codec(KEY_B)
    assert a.fingerprint(ID_CARD) == a.fingerprint(ID_CARD)
    # 换密钥 → 指纹变（否则拿不到密钥的人也能拿裸哈希爆破）
    assert a.fingerprint(ID_CARD) != b.fingerprint(ID_CARD)
    assert len(a.fingerprint(ID_CARD)) == 64


def test_fingerprint_is_case_insensitive_on_trailing_x():
    a = _codec()
    assert a.fingerprint("61012119940103283x") == a.fingerprint("61012119940103283X")


def test_fingerprint_of_empty_is_empty():
    assert _codec().fingerprint("") == ""


def test_encrypt_roundtrip_and_prefix():
    codec = _codec()
    token = codec.encrypt(ID_CARD)
    assert token.startswith(CIPHER_PREFIX)
    assert ID_CARD not in token
    assert codec.decrypt(token) == ID_CARD
    assert is_ciphertext(token)


def test_encrypt_is_randomized():
    """Fernet 带随机 IV —— 同一明文两次密文不同。

    这正是"唯一约束不能建在密文列上"的原因，也是引入 HMAC 指纹的动机。
    """
    codec = _codec()
    assert codec.encrypt(ID_CARD) != codec.encrypt(ID_CARD)


def test_decrypt_passes_through_legacy_plaintext():
    assert _codec().decrypt(ID_CARD) == ID_CARD


def test_decrypt_with_wrong_key_raises_config_error():
    token = _codec(KEY_A).encrypt(ID_CARD)
    with pytest.raises(PiiConfigError):
        _codec(KEY_B).decrypt(token)


def test_protect_returns_cipher_fingerprint_mask():
    encrypted, fingerprint, masked = _codec().protect(ID_CARD)
    assert encrypted.startswith(CIPHER_PREFIX)
    assert fingerprint == _codec().fingerprint(ID_CARD)
    assert masked == ID_CARD_MASKED


def test_build_codec_disabled_returns_none():
    assert build_pii_codec(enabled=False, secret_key="") is None
    assert build_pii_codec(enabled=False, secret_key=KEY_A) is None


def test_build_codec_enabled_without_key_raises():
    """开启保护却不给密钥 → 必须拒绝，不能静默降级成明文。"""
    with pytest.raises(PiiConfigError):
        build_pii_codec(enabled=True, secret_key="")
    with pytest.raises(PiiConfigError):
        build_pii_codec(enabled=True, secret_key="not-a-fernet-key")


# --------------------------------------------------------------------------
# 仓储双路径（不连 MySQL：只验证 SQL 形状与编解码边界）
# --------------------------------------------------------------------------


def _repo(codec=None) -> MySQLAppointmentRepository:
    return MySQLAppointmentRepository(
        "127.0.0.1", 3306, "u", "p", "db", pii_codec=codec
    )


def _appointment() -> Appointment:
    return Appointment(
        username="张三",
        id_card=ID_CARD,
        department="神经内科",
        date="2026-11-20",
        time="上午",
        status=AppointmentStatus.CONFIRMED,
    )


def test_repo_without_codec_keeps_legacy_sql():
    """红线：关闭保护时 SQL 与一期逐字一致（未迁移的库也能跑）。"""
    repo = _repo()
    where, params = repo._dedupe_clause(_appointment())
    assert where == "username=%s AND id_card=%s"
    assert params == ("张三", ID_CARD)
    assert "id_card_hash" not in repo._select_columns()
    assert repo._insert_sql().count("%s") == 15
    assert repo._insert_sql() == repo._INSERT_SQL


def test_repo_with_codec_queries_by_fingerprint():
    codec = _codec()
    repo = _repo(codec)
    where, params = repo._dedupe_clause(_appointment())
    assert "id_card_hash=%s" in where
    # 仍带 OR 明文分支：存量行未回填时不能把"已约过的人"当成新患者
    assert "OR id_card=%s" in where
    assert params[0] == "张三"
    assert params[1] == codec.fingerprint(ID_CARD)
    assert params[2] == ID_CARD


def test_repo_with_codec_selects_extra_columns():
    cols = _repo(_codec())._select_columns()
    assert "id_card_hash" in cols and "id_card_masked" in cols


def test_repo_with_codec_inserts_seventeen_params():
    from datetime import datetime

    codec = _codec()
    repo = _repo(codec)
    params = repo._insert_params(_appointment(), datetime(2026, 11, 12, 8, 0))
    assert repo._insert_sql().count("%s") == 17
    assert len(params) == 17
    assert str(params[1]).startswith(CIPHER_PREFIX)  # id_card 存密文
    assert params[2] == codec.fingerprint(ID_CARD)  # id_card_hash
    assert params[3] == ID_CARD_MASKED  # id_card_masked


def test_repo_row_read_decrypts():
    codec = _codec()
    repo = _repo(codec)
    row = {
        "id": 1,
        "username": "张三",
        "id_card": codec.encrypt(ID_CARD),
        "id_card_masked": ID_CARD_MASKED,
        "department": "神经内科",
        "date": "2026-11-20",
        "time": "上午",
        "status": "CONFIRMED",
        "version": 1,
    }
    assert repo._row_to_appointment(row).id_card == ID_CARD


def test_repo_row_read_keeps_legacy_plaintext():
    repo = _repo(_codec())
    row = {"id": 1, "id_card": ID_CARD, "status": "CONFIRMED"}
    assert repo._row_to_appointment(row).id_card == ID_CARD


def test_repo_row_read_bad_key_falls_back_to_mask():
    """换过密钥的库不能让整张列表 500：单条坏记录降级成掩码展示。"""
    token = _codec(KEY_A).encrypt(ID_CARD)
    repo = _repo(_codec(KEY_B))
    row = {"id": 1, "id_card": token, "id_card_masked": ID_CARD_MASKED, "status": "CONFIRMED"}
    assert repo._row_to_appointment(row).id_card == ID_CARD_MASKED
