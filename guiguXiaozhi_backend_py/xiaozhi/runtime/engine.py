"""智能体循环 —— 对齐参考项目 mini_harness/runtime/engine.py。

流程（对应 Java 版 @AiService 的行为）：
    组装上下文(系统提示+知识+历史) → 调模型 → 有工具调用则执行并回填 → 再调模型 → 输出

每一步都产生事件（Event）与追踪跨度（Span），这就是"完整跟踪流程"的数据来源。

可靠性（对应 PLAN.md 3.4.4）：
    * 空响应 / 连接异常 → 指数退避重试（**只在还没向用户吐出任何文本时**才允许重试，
      否则前端会出现重复文字）；
    * 模型连续失败 → 熔断器打开，直接返回本地规则分导诊建议；
    * 单个工具连续失败 → 本轮熔断该工具，防死循环；
    * 请求级时间预算 → 超时返回已生成内容；
    * 没调工具却说"已预约成功" → 追加提示，并在 trace / metrics 留痕。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional

from xiaozhi.core.event import (
    AgentEndEvent,
    AgentStartEvent,
    DegradedEvent,
    Event,
    TextResponseEvent,
    ToolExecuteEvent,
    ToolResultEvent,
    TurnEndEvent,
    TurnStartEvent,
)
from xiaozhi.core.message import Message, Role, ToolCall
from xiaozhi.core.tool import ToolResult
from xiaozhi.memory.context import ContextAssembler
from xiaozhi.memory.store import ConversationStore
from xiaozhi.models.quality import (
    CORRECTIVE_PROMPT,
    FALSE_CONFIRMATION_NOTICE,
    booking_executed_in_history,
    inspect_turn,
)
from xiaozhi.ops.metrics import MetricsCollector
from xiaozhi.reliability.resilience import (
    CircuitBreaker,
    RetryConfig,
    degraded_reply,
)
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
        *,
        metrics: Optional[MetricsCollector] = None,
        retry_config: Optional[RetryConfig] = None,
        circuit_breaker: Optional[CircuitBreaker] = None,
        request_timeout: Optional[float] = None,
        tool_failure_threshold: int = 3,
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
        self.metrics = metrics or MetricsCollector()
        # max_attempts=1 表示不重试（保持旧行为，测试可显式关掉）
        self.retry_config = retry_config or RetryConfig(max_attempts=1)
        self.circuit_breaker = circuit_breaker
        self.request_timeout = request_timeout
        self.tool_failure_threshold = max(1, tool_failure_threshold)

    async def run(
        self,
        memory_id: str,
        user_input: str,
        user_id: str = "anonymous",
    ) -> AsyncIterator[Event]:
        """跑一轮对话，产出事件流"""
        trace_id = get_trace_id()
        result = AgentEngineResultHolder()
        started = time.monotonic()
        self.metrics.inc("requests_total")

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
            # 操作类请求：历史里的完成态回复会误导小模型跳过工具调用（B1 实测）
            operation_request = not inject_knowledge
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
                context_span.set_attribute(
                    "operation_history_neutralized", operation_request
                )

            messages = context.to_messages(
                user_input, neutralize_history=operation_request
            )
            tools = self.tool_registry.list_definitions()
            known_tool_names = [d.name for d in tools]

            turn = 0
            tool_call_count = 0
            successful_tool_calls = 0  # 请求级：用于抑制"工具已执行后的总结轮"误报
            tool_results: List[Message] = []
            tool_failures: Dict[str, int] = {}
            # 历史里是否真的执行过预约（本次会话早前请求成功挂过号且未取消）。
            # 是真话依据：模型说"已经预约好了"时不算幻觉，也不做纠正重试。
            prior_booking = booking_executed_in_history(history)
            corrective_used = 0     # 已做过的纠正重试次数（每请求至多 1 次）
            corrective_pending = False  # 本轮检测到幻觉且待纠正

            while turn < self.max_turns:
                # ---- 请求级时间预算 ----
                if (
                    self.request_timeout is not None
                    and time.monotonic() - started > self.request_timeout
                ):
                    result.degraded = True
                    yield DegradedEvent(
                        trace_id=trace_id,
                        metadata={"component": "request", "reason": "time_budget_exceeded"},
                    )
                    hint = "\n\n（响应已达时间上限，先返回已生成的内容。）"
                    result.text += hint
                    yield TextResponseEvent(trace_id=trace_id, metadata={"delta": hint})
                    break

                # ---- 模型熔断：打开时直接本地降级 ----
                if self.circuit_breaker is not None and not self.circuit_breaker.allow():
                    result.degraded = True
                    fallback = degraded_reply(user_input)
                    result.text += fallback
                    yield TextResponseEvent(trace_id=trace_id, metadata={"delta": fallback})
                    yield DegradedEvent(
                        trace_id=trace_id,
                        metadata={"component": "model", "reason": "circuit_open"},
                    )
                    logger.warning("模型熔断器打开，返回规则应答：trace=%s", trace_id)
                    break

                yield TurnStartEvent(trace_id=trace_id, metadata={"turn_number": turn})

                # ---- 调模型（流式，文本实时下发给前端）----
                with trace_span("llm.chat", trace_id=trace_id) as llm_span:
                    llm_span.set_attribute("model", getattr(self.chat_model, "model", "?"))
                    llm_span.set_attribute("turn", turn)

                    response = None
                    call_error: Optional[Exception] = None
                    emitted_this_turn = False
                    llm_started = time.monotonic()

                    for attempt in range(self.retry_config.max_attempts):
                        holder: Dict[str, Any] = {}
                        emitted = False
                        attempt_error: Optional[Exception] = None
                        try:
                            async for delta in self.chat_model.stream_chat(
                                messages, tools=tools, result_holder=holder
                            ):
                                if delta:
                                    emitted = True
                                    emitted_this_turn = True
                                    result.text += delta
                                    yield TextResponseEvent(
                                        trace_id=trace_id, metadata={"delta": delta}
                                    )
                            candidate = holder.get("turn") or _empty_turn()
                        except Exception as exc:
                            attempt_error = exc
                            candidate = _empty_turn()

                        finding = inspect_turn(
                            candidate,
                            known_tool_names,
                            tool_executed=successful_tool_calls > 0 or prior_booking,
                        )
                        # 关键：只有"一个字都没下发"时才允许重试，避免前端出现重复文本
                        retryable = (not emitted) and (
                            attempt_error is not None or finding.needs_retry
                        )
                        if retryable and attempt < self.retry_config.max_attempts - 1:
                            self.metrics.inc("empty_response_retries")
                            llm_span.set_attribute("empty_response_retries", attempt + 1)
                            logger.warning(
                                "模型响应为空/异常，重试 %s/%s：trace=%s reason=%s",
                                attempt + 1,
                                self.retry_config.max_attempts - 1,
                                trace_id,
                                ";".join(finding.reasons) or type(attempt_error).__name__,
                            )
                            await asyncio.sleep(self.retry_config.delay_for(attempt + 1))
                            continue

                        if attempt_error is not None:
                            call_error = attempt_error
                            llm_span.set_error(
                                f"{type(attempt_error).__name__}: {attempt_error}"
                            )
                        else:
                            response = candidate
                            llm_span.set_attribute("text_length", len(response.text))
                            llm_span.set_attribute("tool_call_count", len(response.tool_calls))
                            llm_span.set_attribute("prompt_tokens", response.prompt_tokens)
                            llm_span.set_attribute("eval_tokens", response.eval_tokens)
                            llm_span.set_attribute(
                                "quality_reasons", ";".join(finding.reasons)
                            )
                            if finding.textual_tool_call:
                                logger.warning(
                                    "模型把工具调用写成了纯文本：trace=%s", trace_id
                                )
                            if finding.false_confirmation:
                                self.metrics.inc("hallucination_suspected")
                                llm_span.set_attribute("hallucination_suspected", True)
                                if corrective_used == 0 and not prior_booking:
                                    # 先不追加免责提示——注入纠正提示重试一轮，
                                    # 逼迫模型真正调用工具（比事后提示更能纠错）
                                    corrective_pending = True
                                    llm_span.set_attribute(
                                        "corrective_retry_scheduled", True
                                    )
                                    logger.warning(
                                        "检测到未调工具的预约/取消承诺，准备纠正重试：trace=%s",
                                        trace_id,
                                    )
                                else:
                                    result.text += FALSE_CONFIRMATION_NOTICE
                                    yield TextResponseEvent(
                                        trace_id=trace_id,
                                        metadata={"delta": FALSE_CONFIRMATION_NOTICE},
                                    )
                        break

                    self.metrics.record_llm((time.monotonic() - llm_started) * 1000)

                # ---- 模型调用失败：记录熔断并给可读降级文案（不暴露内部错误）----
                if call_error is not None:
                    if self.circuit_breaker is not None:
                        self.circuit_breaker.record_failure()
                    result.error = str(call_error)
                    result.degraded = True
                    logger.warning(
                        "模型调用失败：trace=%s error=%s", trace_id, call_error
                    )
                    if not emitted_this_turn:
                        fallback = degraded_reply(user_input)
                        result.text += fallback
                        yield TextResponseEvent(trace_id=trace_id, metadata={"delta": fallback})
                    yield DegradedEvent(
                        trace_id=trace_id,
                        metadata={"component": "model", "error": str(call_error)},
                    )
                    break

                if self.circuit_breaker is not None:
                    self.circuit_breaker.record_success()

                if response is None:
                    response = _empty_turn()

                # ---- 没有工具调用 → 结束（或纠正重试一轮） ----
                if not response.tool_calls:
                    if corrective_pending:
                        corrective_pending = False
                        corrective_used += 1
                        self.metrics.inc("false_confirmation_retries")
                        messages.append({"role": "user", "content": CORRECTIVE_PROMPT})
                        turn += 1
                        yield TurnEndEvent(
                            trace_id=trace_id,
                            metadata={"turn_number": turn, "corrective_retry": True},
                        )
                        continue
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
                        elif (
                            tool_failures.get(call.name, 0) >= self.tool_failure_threshold
                        ):
                            tool_span.set_attribute("circuit_open", True)
                            tool_result = ToolResult.fail(
                                f"工具【{call.name}】本轮连续失败多次，已暂时停用，请换一种方式回答。",
                                error_type="ToolCircuitOpen",
                            )
                        else:
                            secure_call = SecureToolCall(
                                tool_name=call.name,
                                args=call.arguments,
                                user_id=user_id,
                                trace_id=trace_id,
                            )
                            tool_result = await self.secure_executor.execute(secure_call, tool)

                        if tool_result.success:
                            tool_failures[call.name] = 0
                            successful_tool_calls += 1
                        else:
                            tool_failures[call.name] = tool_failures.get(call.name, 0) + 1

                        self.metrics.record_tool(call.name, tool_result.success)
                        self.metrics.observe(
                            "tool_duration_ms", tool_result.execution_time * 1000
                        )
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

        # ---- 指标收口 ----
        self.metrics.observe("request_duration_ms", (time.monotonic() - started) * 1000)
        if result.error:
            self.metrics.inc("errors_total")
        if result.degraded:
            self.metrics.inc("degraded_total")

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
