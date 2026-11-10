"""上下文组装的预算裁剪单测 —— 原子块裁剪与完整 token 计量。

2026-11-10 审计发现两个缺陷：
1. 按单条消息裁剪会把 assistant(tool_calls) 与其 tool 结果切开，
   发给 Ollama 的序列 tool_call_id 配不上；
2. token 计量只算 content，tool_calls 参数 JSON 不计入 → 实际注入超预算。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from xiaozhi.core.message import Message, Role, ToolCall  # noqa: E402
from xiaozhi.memory.context import (  # noqa: E402
    ContextAssembler,
    _build_atomic_blocks,
    _estimate_message_tokens,
)


def _history():
    long_user = Message.user("第一句话，" + "这一段内容比较长用于撑爆预算。" * 20)
    call = ToolCall(id="c1", name="查询是否有号源", arguments={"department": "神经内科"})
    booking = Message.assistant("", tool_calls=[call])
    tool_reply = Message.tool_result("神经内科有号源", "c1", "查询是否有号源")
    text_reply = Message.assistant("查询完成，祝您健康。")
    follow_user = Message.user("第二个问题")
    return [long_user, booking, tool_reply, text_reply, follow_user]


def test_atomic_blocks_group_tool_calls_with_results():
    blocks = _build_atomic_blocks(_history())
    assert len(blocks) == 4
    assert [m.role for m in blocks[0]] == [Role.USER]
    assert [m.role for m in blocks[1]] == [Role.ASSISTANT, Role.TOOL]  # 调用+结果同块
    assert [m.role for m in blocks[2]] == [Role.ASSISTANT]
    assert [m.role for m in blocks[3]] == [Role.USER]


def test_orphan_tool_message_becomes_own_block():
    blocks = _build_atomic_blocks([Message.tool_result("孤儿结果", "x", "工具")])
    assert len(blocks) == 1
    assert blocks[0][0].role == Role.TOOL


def test_consecutive_tool_turns_form_separate_blocks():
    c1 = ToolCall(id="1", name="查询是否有号源", arguments={})
    c2 = ToolCall(id="2", name="查询科室专家", arguments={})
    history = [
        Message.assistant("", tool_calls=[c1]),
        Message.tool_result("r1", "1", "查询是否有号源"),
        Message.assistant("", tool_calls=[c2]),
        Message.tool_result("r2", "2", "查询科室专家"),
    ]
    blocks = _build_atomic_blocks(history)
    assert len(blocks) == 2
    assert blocks[0][0].tool_calls[0].id == "1"
    assert blocks[1][0].tool_calls[0].id == "2"


def test_trimming_never_splits_call_and_result():
    """任何预算下：保留的 tool 消息必须能配上保留的 assistant tool_call"""
    history = _history()
    for budget in (40, 80, 120, 300, 4000):
        assembler = ContextAssembler(token_budget=budget)
        context = assembler.assemble("s", "hi", history)
        call_ids = {
            call.id for m in context.history if m.tool_calls for call in m.tool_calls
        }
        for message in context.history:
            if message.role == Role.TOOL:
                assert message.tool_call_id in call_ids, f"budget={budget} 出现孤儿 tool 消息"


def test_oldest_block_dropped_whole():
    """预算紧张 → 最老的块整体丢弃（调用与结果一起消失），新内容保留"""
    assembler = ContextAssembler(token_budget=20)
    context = assembler.assemble("s", "hi", _history())

    kept_contents = [m.content for m in context.history]
    assert "查询完成，祝您健康。" in kept_contents
    assert "第二个问题" in kept_contents
    # 长历史被整体裁掉；工具调用块也要么整体在、要么整体不在
    assert all("第一句话" not in c for c in kept_contents)
    assert Role.TOOL not in [m.role for m in context.history]


def test_generous_budget_keeps_everything():
    assembler = ContextAssembler(token_budget=4000)
    context = assembler.assemble("s", "hi", _history())
    assert len(context.history) == 5
    assert context.dropped_items == 0


def test_token_estimate_includes_tool_call_arguments():
    """tool_calls 参数 JSON 必须计入预算（只算 content 会低估）"""
    big_args = ToolCall(
        id="1",
        name="预约挂号",
        arguments={
            "username": "王新勃",
            "id_card": "110101199005054322",
            "department": "神经内科",
            "date": "2026-11-20",
            "time": "上午",
            "note": "x" * 400,
        },
    )
    with_calls = Message.assistant("", tool_calls=[big_args])
    plain = Message.assistant("")
    assert _estimate_message_tokens(with_calls) > 100
    assert _estimate_message_tokens(plain) == _estimate_message_tokens(plain)  # 恒真对齐


def test_big_tool_call_arguments_get_trimmed():
    """参数很大的工具消息会被预算裁掉 —— content 为空不再等于零成本"""
    big_args = ToolCall(id="1", name="预约挂号", arguments={"note": "x" * 800})
    history = [Message.assistant("", tool_calls=[big_args]), Message.user("问题")]
    assembler = ContextAssembler(token_budget=50)
    context = assembler.assemble("s", "hi", history)
    assert all(not m.tool_calls for m in context.history)  # 超预算被裁
    assert context.history and context.history[-1].content == "问题"
