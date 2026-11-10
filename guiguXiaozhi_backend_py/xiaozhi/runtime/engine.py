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

import logging
import time
from typing import Any, AsyncIterator, Dict, List, Optional

from xiaozhi.core.event import (
    AgentEndEvent,
    AgentStartEvent,
    DegradedEvent,
    Event,
    TextResponseEvent,
    TurnEndEvent,
    TurnStartEvent,
)
from xiaozhi.core.message import Role, ToolCall
from xiaozhi.memory.context import ContextAssembler
from xiaozhi.memory.store import ConversationStore
from xiaozhi.models.quality import booking_executed_in_history, corrective_prompt
from xiaozhi.ops.metrics import MetricsCollector
from xiaozhi.reliability.resilience import (
    CircuitBreaker,
    RetryConfig,
    degraded_reply,
)
from xiaozhi.reliability.tracing import get_trace_id, trace_span
from xiaozhi.runtime.engine_phases import EnginePhases
from xiaozhi.runtime.persister import ConversationPersister
from xiaozhi.runtime.state import TurnState
from xiaozhi.runtime.stream_filter import trim_to_sentence as _trim_to_sentence
from xiaozhi.security.secure_executor import SecureToolExecutor
from xiaozhi.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

# 句子裁剪已迁至 runtime/stream_filter.py（E3，`_trim_to_sentence` 为兼容别名）；
# 执行阶段（上下文/模型回合/工具执行）已迁至 runtime/engine_phases.py（E6）。


class AgentEngine(EnginePhases):
    """智能体运行时（执行阶段见 EnginePhases mixin）"""

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
        self.persister = ConversationPersister()

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

            # ---- 检索知识 + 组装上下文（见 _prepare_messages）----
            prep: Dict[str, Any] = {}
            async for _ev in self._prepare_messages(
                user_input=user_input,
                history=history,
                trace_id=trace_id,
                outcome=prep,
            ):
                yield _ev
            messages = prep["messages"]
            tools = prep["tools"]
            known_tool_names = prep["known_tool_names"]
            operation_request = prep["operation_request"]

            turn = 0
            # ---- 请求级跨轮状态（E2 收敛）----
            # 工具执行跟踪、纠正重试、文本账本全部收进 TurnState：
            # 写入只经方法（不变量在方法内维护），读取直接访问字段。
            # 第十轮"纠正重试拼接"事故的根源就是状态没有单一事实来源。
            # 历史里是否真的执行过预约（本次会话早前请求成功挂过号且未取消）。
            # 是真话依据：模型说"已经预约好了"时不算幻觉，也不做纠正重试。
            prior_booking = booking_executed_in_history(history)
            state = TurnState(
                prior_booking=prior_booking,
                operation_request=operation_request,
            )

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

                # ---- 调模型（流式 + 质量拦截器链，见 _chat_turn）----
                turn_outcome: Dict[str, Any] = {}
                async for _ev in self._chat_turn(
                    messages=messages,
                    tools=tools,
                    known_tool_names=known_tool_names,
                    state=state,
                    user_input=user_input,
                    trace_id=trace_id,
                    turn_number=turn,
                    request_span=span,
                    result=result,
                    outcome=turn_outcome,
                ):
                    yield _ev
                response = turn_outcome.get("response")
                call_error = turn_outcome.get("call_error")
                emitted_this_turn = turn_outcome.get("emitted_this_turn", False)

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
                    state.set_final_reply(response.text)
                    # （narrated 叙述承诺检测已收敛进拦截器链，见上方 gate_verdict）

                    corrective_fired, corrective_kind = state.consume_corrective()
                    if corrective_fired:
                        if corrective_kind != "action":
                            self.metrics.inc("false_confirmation_retries")
                        messages.append(
                            {
                                "role": "user",
                                "content": corrective_prompt(corrective_kind),
                            }
                        )
                        turn += 1
                        yield TurnEndEvent(
                            trace_id=trace_id,
                            metadata={"turn_number": turn, "corrective_retry": True},
                        )
                        continue
                    break

                # ---- 执行工具 + 结果回填（见 _execute_tools）----
                async for _ev in self._execute_tools(
                    response=response,
                    messages=messages,
                    state=state,
                    user_id=user_id,
                    trace_id=trace_id,
                    result=result,
                ):
                    yield _ev

                turn += 1
                yield TurnEndEvent(
                    trace_id=trace_id,
                    metadata={"turn_number": turn, "has_tool_calls": True},
                )

            # ---- 落库（失败不阻断；组装逻辑见 runtime/persister.py）----
            with trace_span("memory.write", trace_id=trace_id) as write_span:
                try:
                    new_messages = self.persister.build(
                        history,
                        user_input,
                        state,
                        result.tool_calls,
                        fallback_text=result.text,
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
                "corrective_retries": state.corrective_used,
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
