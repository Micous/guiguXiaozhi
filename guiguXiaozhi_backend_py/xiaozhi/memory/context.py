"""上下文组装 —— 按优先级 + token 预算拼装最终提示词。

优先级（高 → 低）：
    系统提示词  >  长期画像  >  RAG 知识  >  短期历史  >  当前输入
超预算时从低优先级开始裁剪，保证系统提示词与用户输入永远保留。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from xiaozhi.core.message import Message, Role
from xiaozhi.models.parser import estimate_tokens_approx

logger = logging.getLogger(__name__)

# 历史回复里的"操作已完成"断言会让 qwen2.5:7b 误以为本轮不用再调工具。
# 实测（B1 联调 A/B）：原样保留时取消预约工具调用率 0/3；中性化后恢复为 1/1。
# 用"改写措辞"而不是"加标注前缀"——前缀会被模型原样复述到回答里污染用户可见文本。
_COMPLETION_CLAIM_RE = re.compile(
    r"已经成功啦|已经成功|已成功取消|已为您取消|已经帮您取消|成功取消|取消成功"
    r"|预约成功|挂号成功|成功啦|已成功|已完成|已取消|已退号|已办理|成功"
)


NEUTRAL_HISTORY_REPLY = "好的，我已了解您的需求。"


def neutralize_history_reply(text: str) -> str:
    """操作类请求：把历史里"操作已完成"的回复替换为中性短句。

    只影响发给模型的上下文，用户看到的回答与持久化记忆都不变。
    * 保留原完成态文本 → 模型跳过工具调用（0/3）；
    * 只做措辞替换但保留"预约详情/按时就诊" → 仍会跳过（0/1）；
    * 整条删除 → 模型会连"预约挂号"一起调（有误操作风险）；
    * 换成中性短句 → 稳定只调当前需要的工具（B1 实测通过）。
    不含完成态断言的历史（如分导诊建议）原样保留，不损失上下文。
    """
    if _COMPLETION_CLAIM_RE.search(text or ""):
        return NEUTRAL_HISTORY_REPLY
    return text


@dataclass
class AssembledContext:
    """组装结果"""

    system_prompt: str
    knowledge: str = ""
    history: List[Message] = field(default_factory=list)
    dropped_items: int = 0
    total_tokens: int = 0

    def to_messages(
        self, user_input: str, *, neutralize_history: bool = False
    ) -> List[Dict[str, Any]]:
        """转成 Ollama /api/chat 的 messages 数组

        顺序必须是：系统提示 → 历史消息 → 当前输入（含知识注入）。
        历史放在当前输入之后会让模型把历史当成"用户刚说的话"，对话就串了。

        ``neutralize_history`` 供**操作类请求**开启：历史里的"预约已经成功啦"这类
        完成态回复会让 qwen2.5:7b 误以为本次操作已办完，从而跳过工具调用
        （真实联调 B1 定位）。把完成态措辞改成中性记录即可恢复，
        既保留上下文，也不会像"加标注前缀"那样被模型复述进回答。
        """
        messages: List[Dict[str, Any]] = [
            {"role": Role.SYSTEM.value, "content": self.system_prompt}
        ]

        for message in self.history:
            messages.append(self._to_ollama_message(message, neutralize_history))

        if self.knowledge:
            messages.append(
                {
                    "role": Role.USER.value,
                    "content": (
                        f"{user_input}\n\n"
                        "请参考以下医院知识库内容回答用户问题"
                        "（若知识库内容与你自身的医学知识冲突，请以医学常识为准）：\n\n"
                        f"{self.knowledge}"
                    ),
                }
            )
        else:
            messages.append({"role": Role.USER.value, "content": user_input})

        return messages

    @staticmethod
    def _to_ollama_message(
        message: Message, neutralize_history: bool = False
    ) -> Dict[str, Any]:
        """转成 Ollama 消息格式（工具调用要保留 tool_calls 结构）"""
        content = message.content
        if neutralize_history and message.role == Role.ASSISTANT:
            content = neutralize_history_reply(content)
        if message.role == Role.TOOL:
            return {
                "role": Role.TOOL.value,
                "content": content,
                "tool_call_id": message.tool_call_id,
                "tool_name": message.tool_name,
            }
        if message.role == Role.ASSISTANT and message.tool_calls:
            return {
                "role": Role.ASSISTANT.value,
                "content": content,
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": call.arguments,
                        },
                    }
                    for call in message.tool_calls
                ],
            }
        return {"role": message.role.value, "content": content}


class ContextAssembler:
    """上下文组装引擎"""

    def __init__(self, token_budget: int = 4000) -> None:
        self.token_budget = token_budget

    def assemble(
        self,
        system_prompt: str,
        user_input: str,
        history: List[Message],
        knowledge: str = "",
        profile: str = "",
    ) -> AssembledContext:
        """按优先级拼装上下文"""
        dropped = 0
        used = estimate_tokens_approx(system_prompt) + estimate_tokens_approx(user_input)

        # 知识片段：超预算就丢弃（历史更重要 —— 它承载对话连续性）
        if knowledge:
            knowledge_tokens = estimate_tokens_approx(knowledge)
            if used + knowledge_tokens <= self.token_budget:
                used += knowledge_tokens
            else:
                knowledge = ""
                dropped += 1

        # 长期画像：优先于历史
        if profile:
            profile_tokens = estimate_tokens_approx(profile)
            if used + profile_tokens <= self.token_budget:
                used += profile_tokens
                system_prompt = f"{system_prompt}\n\n## 用户画像\n{profile}"
            else:
                dropped += 1

        # 短期历史：从最早的开始裁剪
        kept: List[Message] = list(history)
        while kept:
            oldest = kept[0]
            oldest_tokens = estimate_tokens_approx(oldest.content)
            if used + oldest_tokens <= self.token_budget:
                break
            kept.pop(0)
            dropped += 1
        used += sum(estimate_tokens_approx(m.content) for m in kept)

        return AssembledContext(
            system_prompt=system_prompt,
            knowledge=knowledge,
            history=kept,
            dropped_items=dropped,
            total_tokens=used,
        )
