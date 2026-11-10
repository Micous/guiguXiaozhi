"""上下文组装 —— 按优先级 + token 预算拼装最终提示词。

优先级（高 → 低）：
    系统提示词  >  长期画像  >  RAG 知识  >  短期历史  >  当前输入
超预算时从低优先级开始裁剪，保证系统提示词与用户输入永远保留。
"""

from __future__ import annotations

import json
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


def _estimate_message_tokens(message: Message) -> int:
    """**完整消息**的 token 估算（content + tool_calls 参数 JSON）。

    只算 content 会低估带工具调用的消息——参数 JSON（身份证号、科室、日期）
    同样占预算；低估会让实际注入超出模型上下文窗口。
    """
    total = estimate_tokens_approx(message.content)
    if message.tool_calls:
        try:
            payload = json.dumps(
                [call.to_dict() for call in message.tool_calls],
                ensure_ascii=False,
                default=str,
            )
            total += estimate_tokens_approx(payload)
        except (TypeError, ValueError):  # pragma: no cover - 防御性
            pass
    return total


def _build_atomic_blocks(history: List[Message]) -> List[List[Message]]:
    """把历史切成**原子块**：``assistant(tool_calls)`` 与其后的 tool 结果不可拆分。

    发给 Ollama 的序列里，tool 结果必须紧跟在发起调用的 assistant 之后
    （靠 tool_call_id 配对）。按单条消息裁剪会把两者切开，
    产生模型无法理解的残缺序列 —— 这是 2026-11-10 审计发现的裁剪缺陷。

    块的划分：
      * ``assistant`` 带 tool_calls → 开启新块，其后连续的 ``tool`` 消息并入；
      * 其余（user / 纯文本 assistant / 孤儿 tool）各自成块。
    """
    blocks: List[List[Message]] = []
    pending: List[Message] = []  # 当前工具调用块（assistant + 其 tool 结果）
    for message in history:
        if message.role == Role.ASSISTANT and message.tool_calls:
            if pending:
                blocks.append(pending)
                pending = []
            pending.append(message)
        elif message.role == Role.TOOL and pending:
            pending.append(message)
        else:
            if pending:
                blocks.append(pending)
                pending = []
            blocks.append([message])
    if pending:
        blocks.append(pending)
    return blocks


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


def rule_based_summary(messages: List[Message]) -> str:
    """离线规则摘要：保留操作类信息（工具调用与结论），压缩寒暄与重复内容。

    不接 LLM —— 作为 ``ContextAssembler`` summarizer 钩子的默认实现，
    后续可无缝替换成 LLM 摘要（接口相同：List[Message] -> str）。
    """
    parts: List[str] = []
    for message in messages:
        if message.tool_calls:
            metadata = message.metadata or {}
            if metadata.get("booking_executed"):
                outcome = "（预约已成功）"
            elif metadata.get("cancel_executed"):
                outcome = "（已取消预约）"
            else:
                outcome = ""
            names = list(dict.fromkeys(call.name for call in message.tool_calls))
            parts.append("已执行工具：" + "、".join(names) + outcome)
        elif message.role == Role.TOOL:
            if message.content.strip():
                parts.append("工具结论：" + message.content[:80])
        elif message.role == Role.USER and message.content.strip():
            parts.append("用户问过：" + message.content[:60])
        elif message.role == Role.ASSISTANT and message.content.strip():
            parts.append("助手答过：" + message.content[:60])
    return "；".join(parts)[:400]


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

    def __init__(
        self,
        token_budget: int = 4000,
        summarizer: Optional[Any] = None,
    ) -> None:
        """
        Args:
            token_budget: 注入模型的总 token 预算
            summarizer:   可选的**历史压缩钩子**（List[Message] -> str）。
                          裁剪丢弃最老消息时，用摘要把它们"浓缩"回窗口头部，
                          保留更早的上下文。传 ``rule_based_summary`` 用离线
                          规则实现，或自接 LLM 摘要；不传则维持纯裁剪行为。
        """
        self.token_budget = token_budget
        self.summarizer = summarizer

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

        # 短期历史：按**原子块**从最早开始裁剪。
        # assistant(tool_calls) 与其 tool 结果永不拆开 —— 拆开会发给 Ollama
        # 一个 tool_call_id 配不上的残缺序列（2026-11-10 审计修复）。
        # token 计量含 tool_calls 参数 JSON，不再只算 content（防低估超预算）。
        blocks = _build_atomic_blocks(history)
        sizes = [sum(_estimate_message_tokens(m) for m in block) for block in blocks]
        suffix = sum(sizes)
        start = len(blocks)
        for index, size in enumerate(sizes):
            if used + suffix <= self.token_budget:
                start = index
                break
            suffix -= size
        kept = [message for block in blocks[start:] for message in block]
        dropped += sum(len(block) for block in blocks[:start])
        used += suffix

        # 长历史压缩：被裁掉的最老消息若可摘要且预算放得下，
        # 浓缩成一条摘要消息塞回窗口头部（比"直接丢失"保留更多上下文）
        dropped_messages = [message for block in blocks[:start] for message in block]
        if dropped_messages and self.summarizer is not None:
            summary = self._build_summary(dropped_messages)
            if summary is not None:
                summary_tokens = _estimate_message_tokens(summary)
                if used + summary_tokens <= self.token_budget:
                    kept = [summary] + kept
                    used += summary_tokens
                else:
                    logger.debug("摘要放不下预算，本次跳过压缩")

        return AssembledContext(
            system_prompt=system_prompt,
            knowledge=knowledge,
            history=kept,
            dropped_items=dropped,
            total_tokens=used,
        )

    def _build_summary(self, dropped_messages: List[Message]) -> Optional[Message]:
        """生成被裁历史的一条摘要消息；钩子异常或空摘要时返回 None（跳过）"""
        try:
            text = self.summarizer(dropped_messages)
        except Exception:
            logger.warning("历史摘要生成失败，本次跳过压缩", exc_info=True)
            return None
        if not text:
            return None
        return Message.system(f"以下是本次对话更早部分的摘要：{text}")
