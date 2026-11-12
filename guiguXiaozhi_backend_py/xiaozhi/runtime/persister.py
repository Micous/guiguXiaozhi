"""会话落库组装 —— engine 重构 E5 的产物。

三段式落库结构（第七轮设计 + 第十轮 final_reply_text 修正的最终形态）：

    [user 请求]
    → [assistant(tool_calls, content=叙述段)]     有工具调用的轮
    → [tool 结果 ×N]                               tool_call_id 按序配对
    → [assistant(最终回复, outcome_meta)]          只含最后一轮无工具调用的文本

职责：从 (history, user_input, state, tool_calls) 组装出**经过序列校验**的
落库消息列表。存储写入（store.save）与失败降级事件仍由 engine 负责——
这里只管"组装出正确的数据"，不碰 IO。
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any, Dict, List, Sequence

from xiaozhi.core.message import Message, Role, ToolCall
from xiaozhi.runtime.state import TurnState
from xiaozhi.security.masking import mask_pii_in_text, mask_sensitive_args

logger = logging.getLogger(__name__)


class ConversationPersister:
    """把一次请求的产出组装成落库消息列表（无状态，可共享）。"""

    def build(
        self,
        history: Sequence[Message],
        user_input: str,
        state: TurnState,
        tool_calls: Sequence[ToolCall],
        fallback_text: str = "",
    ) -> List[Message]:
        new_messages = list(history)
        new_messages.append(Message.user(user_input))

        outcome_meta: Dict[str, Any] = {}
        if state.booking_executed_success:
            outcome_meta["booking_executed"] = True
        if state.cancel_executed_success:
            outcome_meta["cancel_executed"] = True

        if tool_calls:
            # 叙述段独立存进 assistant(tool_calls)（第七轮：叙述句进历史
            # 会教模型下一轮照抄句式零工具调用）
            new_messages.append(
                Message(
                    role=Role.ASSISTANT,
                    content=state.narrative_text,
                    tool_calls=list(tool_calls),
                )
            )
        else:
            # 第十轮：纠正重试后 result.text 累积了两轮文本（第一轮幻觉承诺 +
            # 重试轮回答）。落库只存最后一轮回复（state.final_reply_text 在每个
            # 无 tool_calls 轮更新）；第一轮文本已流出无法撤回，但绝不能再进历史。
            # final_reply_text 为空（如轮数耗尽全是工具轮）时退回流式累积文本。
            new_messages.append(
                Message(
                    role=Role.ASSISTANT,
                    content=state.final_or(fallback_text),
                    metadata=outcome_meta,
                )
            )

        # 工具结果按序落库：assistant(tool_calls) 之后必须跟得上对应的 tool
        # 消息（tool_call_id 配对），否则下一请求组装出的历史是模型无法
        # 理解的残缺序列
        new_messages.extend(state.persisted_tool_results)

        if tool_calls:
            closing_text = state.final_or(fallback_text)
            if closing_text.strip():
                new_messages.append(
                    Message(
                        role=Role.ASSISTANT,
                        content=closing_text,
                        metadata=outcome_meta,
                    )
                )

        self.validate_new_entries(history, new_messages)
        return self.mask_new_entries(history, new_messages)

    @staticmethod
    def mask_new_entries(
        history: Sequence[Message], new_messages: List[Message]
    ) -> List[Message]:
        """落库前对**模型产出**做 PII 打码（P0-06）。

        为什么要在这里做：历史不只是给模型看的，它还是一份长期存储的个人信息。
        模型的叙述、工具调用参数（``{"id_card": "610121..."}``）、工具结果
        都带着身份证号 —— 不在这里收口，加密预约表也挡不住会话库里的明文。

        **不改用户自己的消息**：``content`` 是用户亲口说的话，改写它就等于
        篡改对话记录（审计与举证时"用户到底说了什么"必须可还原），而且会让
        "取消刚才那个预约"这类多轮请求彻底失去上下文。用户原文的留存与
        加密属于会话存储侧的策略问题，不在本函数的职责内。
        """
        masked: List[Message] = list(new_messages[: len(history)])
        for message in new_messages[len(history) :]:
            if message.role == Role.USER:
                masked.append(message)
                continue
            content = mask_pii_in_text(message.content)
            tool_calls = message.tool_calls
            if tool_calls:
                tool_calls = [
                    ToolCall(
                        id=call.id,
                        name=call.name,
                        arguments=mask_sensitive_args(call.arguments),
                    )
                    for call in tool_calls
                ]
            if content == message.content and tool_calls is message.tool_calls:
                masked.append(message)
            else:
                masked.append(
                    replace(message, content=content, tool_calls=tool_calls)
                )
        return masked

    @staticmethod
    def validate_new_entries(
        history: Sequence[Message], new_messages: Sequence[Message]
    ) -> None:
        """序列校验只针对本次新增的消息（历史是已落库的事实，不翻旧账）。"""
        for appended in new_messages[len(history) :]:
            problem = appended.validate()
            if problem:
                logger.warning("落库消息未通过序列校验：%s", problem)
