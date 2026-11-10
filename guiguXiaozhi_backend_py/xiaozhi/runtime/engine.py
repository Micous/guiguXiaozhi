"""智能体循环 —— 对齐参考项目 mini_harness/runtime/engine.py。

流程（对应 Java 版 @AiService 的行为）：
    组装上下文(系统提示+知识+历史) → 调模型 → 有工具调用则执行并回填 → 再调模型 → 输出

每一步都产生事件（Event）与追踪跨度（Span），这就是"完整跟踪流程"的数据来源。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional

from xiaozhi.core.event import (
    AgentEndEvent,
    AgentStartEvent,
    DegradedEvent,
    ErrorEvent,
    Event,
    TextResponseEvent,
    ToolExecuteEvent,
    ToolResultEvent,
    TurnEndEvent,
    TurnStartEvent,
)
from xiaozhi.core.message import Message, Role, ToolCall
from xiaozhi.core.tool import Tool, ToolResult
from xiaozhi.memory.context import ContextAssembler
from xiaozhi.memory.store import ConversationStore
from xiaozhi.reliability.tracing import get_trace_id, trace_span
from xiaozhi.runtime.intent import should_inject_knowledge
from xiaozhi.security.secure_executor import SecureToolExecutor, ToolCall as SecureToolCall
from xiaozhi.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


@dataclass
class EngineResult:
    """一次完整对话的结果"""

    text: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    turns: int = 0
    degraded: bool = False
    error: Optional[str] = None


class AgentEngine:
    """智能体运行时"""

    def __init__(
        self,
        chat_model,
        tool_registry: ToolRegistry,
        secure_executor: SecureToolExecutor,
        conversation_store: ConversationStore,
        context_assembler: ContextAssembler,
        system_prompt: str,
        retriever=None,
        max_turns: int = 6,
        max_tool_calls: int = 8,
    ) -> None:
        self.chat_model = chat_model
        self.tool_registry = tool_registry
        self.secure_executor = secure_executor
        self.conversation_store = conversation_store
        self.context_assembler = context_assembler
        self.system_prompt = system_prompt
        self.retriever = retriever
        self.max_turns = max_turns
        self.max_tool_calls = max_tool_calls

    async def run(
        self,
        memory_id: str,
        user_input: str,
        user_id: str = "anonymous",
    ) -> AsyncIterator[Event]:
        """跑一轮对话，产出事件流"""
        trace_id = get_trace_id()
        result = AgentEngineResultHolder()

        with trace_span("http.request", trace_id=trace_id) as span:
            span.set_attribute("memory_id", memory_id)
            span.set_attribute("message_preview", user_input[:120])

            yield AgentStartEvent(trace_id=trace_id, metadata={"input_length": len(user_input)})

            # ---- 读历史 ----
            with trace_span("memory.load", trace_id=trace_id) as memory_span:
                history = await self.conversation_store.load(memory_id, limit=20)
                memory_span.set_attribute("message_count", len(history))

            # ---- 检索知识 ----
            # 操作类请求（预约/取消/查号源/查专家）不注入知识：
            # 实测注入后模型会把请求当成"知识题"直接答题，工具调用率从 5/5 掉到 0/5。
            knowledge = ""
            inject_knowledge = should_inject_knowledge(user_input)
            if self.retriever is not None and inject_knowledge:
                with trace_span("retrieval.search", trace_id=trace_id) as retrieval_span:
                    hits = await self.retriever.retrieve(user_input, top_k=2)
                    retrieval_span.set_attribute("hit_count", len(hits))
                    retrieval_span.set_attribute("injected", True)
                    if hits:
                        knowledge = "\n\n".join(h.text for h in hits)
                        retrieval_span.set_attribute(
                            "hits",
                            [
                                {
                                    "score": round(h.score, 5),
                                    "heading_path": h.metadata.get("heading_path", ""),
                                }
                                for h in hits
                            ],
                        )

            # ---- 组装上下文 ----
            with trace_span("context.assemble", trace_id=trace_id) as context_span:
                context = self.context_assembler.assemble(
                    self.system_prompt, user_input, history, knowledge=knowledge
                )
                context_span.set_attribute("history_kept", len(context.history))
                context_span.set_attribute("dropped_items", context.dropped_items)
                context_span.set_attribute("total_tokens", context.total_tokens)

            messages = context.to_messages(user_input)
            tools = self.tool_registry.list_definitions()

            turn = 0
            tool_call_count = 0
            tool_results: List[Message] = []

            while turn < self.max_turns:
                yield TurnStartEvent(trace_id=trace_id, metadata={"turn_number": turn})

                # ---- 调模型（流式，文本实时下发给前端）----
                with trace_span("llm.chat", trace_id=trace_id) as llm_span:
                    llm_span.set_attribute("model", getattr(self.chat_model, "model", "?"))
                    llm_span.set_attribute("turn", turn)
                    holder: Dict[str, Any] = {}
                    try:
                        async for delta in self.chat_model.stream_chat(
                            messages, tools=tools, result_holder=holder
                        ):
                            result.text += delta
                            yield TextResponseEvent(trace_id=trace_id, metadata={"delta": delta})
                        response = holder.get("turn")
                    except Exception as exc:
                        llm_span.set_error(f"{type(exc).__name__}: {exc}")
                        result.error = str(exc)
                        yield ErrorEvent(
                            trace_id=trace_id,
                            metadata={"error": f"{type(exc).__name__}: {exc}", "turn": turn},
                        )
                        break

                    if response is None:
                        response = _empty_turn()

                    llm_span.set_attribute("text_length", len(response.text))
                    llm_span.set_attribute("tool_call_count", len(response.tool_calls))
                    llm_span.set_attribute("prompt_tokens", response.prompt_tokens)
                    llm_span.set_attribute("eval_tokens", response.eval_tokens)

                # ---- 没有工具调用 → 结束 ----
                if not response.tool_calls:
                    break

                # ---- 执行工具 ----
                assistant_message = Message.assistant(response.text, tool_calls=response.tool_calls)
                messages.append(self._assistant_to_ollama(assistant_message))

                for call in response.tool_calls:
                    if tool_call_count >= self.max_tool_calls:
                        logger.warning("工具调用次数达到上限，停止本轮工具调用")
                        tool_results.append(
                            Message.tool_result(
                                "本轮工具调用次数已达上限，请直接给出建议。",
                                call.id,
                                call.name,
                                is_error=True,
                            )
                        )
                        break

                    tool_call_count += 1
                    yield ToolExecuteEvent(
                        trace_id=trace_id,
                        metadata={"tool_name": call.name, "tool_id": call.id},
                    )

                    with trace_span("tool.execute", trace_id=trace_id) as tool_span:
                        tool_span.set_attribute("tool_name", call.name)
                        tool = self.tool_registry.get(call.name)
                        if tool is None:
                            tool_span.set_error("工具未注册")
                            tool_result = ToolResult.fail(
                                f"工具【{call.name}】不存在。", error_type="ToolNotFound"
                            )
                        else:
                            secure_call = SecureToolCall(
                                tool_name=call.name,
                                args=call.arguments,
                                user_id=user_id,
                                trace_id=trace_id,
                            )
                            tool_result = await self.secure_executor.execute(secure_call, tool)
                            tool_span.set_attribute("success", tool_result.success)
                            tool_span.set_attribute(
                                "result_preview", tool_result.content[:160]
                            )
                            tool_span.set_attribute(
                                "execution_time_ms", round(tool_result.execution_time * 1000, 2)
                            )

                    yield ToolResultEvent(
                        trace_id=trace_id,
                        metadata={
                            "tool_id": call.id,
                            "tool_name": call.name,
                            "is_error": tool_result.is_error,
                            "content_length": len(tool_result.content),
                        },
                    )

                    tool_results.append(
                        Message.tool_result(
                            tool_result.content,
                            call.id,
                            call.name,
                            is_error=tool_result.is_error,
                            error_type=tool_result.error_type,
                        )
                    )
                    result.tool_calls.append(call)

                # ---- 工具结果回填，继续下一轮 ----
                for message in tool_results:
                    messages.append(self._tool_to_ollama(message))
                tool_results = []

                turn += 1
                yield TurnEndEvent(
                    trace_id=trace_id,
                    metadata={"turn_number": turn, "has_tool_calls": True},
                )

            # ---- 落库（失败不阻断） ----
            with trace_span("memory.write", trace_id=trace_id) as write_span:
                try:
                    new_messages = list(history)
                    new_messages.append(Message.user(user_input))
                    new_messages.append(
                        Message.assistant(result.text, tool_calls=result.tool_calls)
                    )
                    await self.conversation_store.save(memory_id, new_messages)
                except Exception as exc:
                    write_span.set_error(f"{type(exc).__name__}: {exc}")
                    result.degraded = True
                    yield DegradedEvent(
                        trace_id=trace_id,
                        metadata={"component": "memory", "error": str(exc)},
                    )

            span.set_attribute("turns", turn)
            span.set_attribute("degraded", result.degraded)

        yield AgentEndEvent(
            trace_id=trace_id,
            metadata={
                "turns": turn,
                "tool_calls": [c.name for c in result.tool_calls],
                "text_length": len(result.text),
                "degraded": result.degraded,
            },
        )

    # ---------- 消息格式转换 ----------

    @staticmethod
    def _assistant_to_ollama(message: Message) -> Dict[str, Any]:
        return {
            "role": Role.ASSISTANT.value,
            "content": message.content,
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                }
                for call in message.tool_calls
            ],
        }

    @staticmethod
    def _tool_to_ollama(message: Message) -> Dict[str, Any]:
        return {
            "role": Role.TOOL.value,
            "content": message.content,
            "tool_call_id": message.tool_call_id,
            "tool_name": message.tool_name,
        }


def _empty_turn():
    """空响应兜底"""
    from xiaozhi.models.ollama import ChatTurn

    return ChatTurn()


class AgentEngineResultHolder:
    """内部用的结果收集器（避免用可变默认值）"""

    def __init__(self) -> None:
        self.text = ""
        self.tool_calls: List[ToolCall] = []
        self.turns = 0
        self.degraded = False
        self.error: Optional[str] = None
