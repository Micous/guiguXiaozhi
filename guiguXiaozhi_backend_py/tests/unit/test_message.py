"""消息模块单测 —— 容错反序列化与基本约束。

2026-11-10 审计发现：Message.from_dict 遇到未知 role 直接抛 ValueError，
存储层里一条脏消息会让整段会话历史加载失败（load 返回 []，等于失忆）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from xiaozhi.core.message import Message, Role, ToolCall  # noqa: E402


def _valid_dict() -> dict:
    call = ToolCall(id="1", name="预约挂号", arguments={"username": "王新勃"})
    return Message.assistant("预约成功", tool_calls=[call], booking_executed=True).to_dict()


def test_roundtrip_preserves_all_fields():
    message = Message.from_dict(_valid_dict())
    assert message.role == Role.ASSISTANT
    assert message.content == "预约成功"
    assert message.tool_calls[0].name == "预约挂号"
    assert message.metadata.get("booking_executed") is True


def test_from_dict_safe_rejects_unknown_role():
    data = _valid_dict()
    data["role"] = "moderator"  # 不存在的角色
    assert Message.from_dict_safe(data) is None


def test_from_dict_safe_rejects_non_dict_and_missing_role():
    assert Message.from_dict_safe("not a dict") is None
    assert Message.from_dict_safe(None) is None
    assert Message.from_dict_safe({"content": "没有 role"}) is None


def test_from_dict_safe_tolerates_missing_fields():
    message = Message.from_dict_safe({"role": "user", "content": "你好"})
    assert message is not None
    assert message.role == Role.USER
    assert message.content == "你好"
    assert message.tool_calls == []
    assert message.metadata == {}


def test_from_dict_safe_tolerates_corrupt_tool_calls():
    data = _valid_dict()
    data["tool_calls"] = ["不是 dict", {"id": "2", "name": "取消预约挂号"}]
    message = Message.from_dict_safe(data)
    assert message is not None
    assert [c.name for c in message.tool_calls] == ["取消预约挂号"]


def test_from_dict_safe_skips_bad_timestamp():
    """坏 timestamp 视为脏数据 → 整条跳过（由存储层记数告警）"""
    data = _valid_dict()
    data["timestamp"] = "1999-13-45T99:99:99"
    assert Message.from_dict_safe(data) is None


def test_from_dict_strict_still_raises_for_programmatic_errors():
    """严格版保留原有行为：调用方自己保证结构时快速失败"""
    import pytest

    with pytest.raises(ValueError):
        Message.from_dict({"role": "moderator"})


def test_assistant_accepts_metadata():
    """engine 落库要往 assistant 消息上打 outcome 标记（此前只能绕过构造器）"""
    message = Message.assistant("预约成功", booking_executed=True)
    assert message.metadata == {"booking_executed": True}
