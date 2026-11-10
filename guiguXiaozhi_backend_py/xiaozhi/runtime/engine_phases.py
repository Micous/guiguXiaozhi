"""engine 的三个执行阶段（上下文组装 / 模型回合 / 工具执行）—— E6 拆分。

以 mixin 形式供 AgentEngine 继承：方法通过 self 访问共享设施
（chat_model / metrics / retry_config / 拦截器链 / StreamFilter…），
事件实时 yield 给 run() 透传。逻辑自 engine.py 原样平移，行为零变更。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, AsyncIterator, Dict, List, Optional

from xiaozhi.core.event import (
    DegradedEvent,
    Event,
    RetrievalEvent,
    TextResponseEvent,
    ToolExecuteEvent,
    ToolResultEvent,
)
from xiaozhi.core.message import Message
from xiaozhi.core.tool import ToolResult
from xiaozhi.models.quality import inspect_turn
from xiaozhi.runtime.engine_helpers import (
    _TurnView,
    _empty_turn,
    _last_assistant_text,
)
from xiaozhi.reliability.tracing import trace_span
from xiaozhi.runtime.intent import should_inject_knowledge
from xiaozhi.runtime.interceptors import TurnFacts, quality_gate
from xiaozhi.runtime.state import TurnState
from xiaozhi.runtime.stream_filter import StreamFilter
from xiaozhi.security.secure_executor import ToolCall as SecureToolCall

logger = logging.getLogger(__name__)


class EnginePhases:
    # ---------- 内部阶段（上下文/模型回合/工具执行）----------

    async def _prepare_messages(
        self,
        *,
        user_input: str,
        history: List[Message],
        trace_id: str,
        outcome: Dict[str, Any],
    ) -> AsyncIterator[Event]:
        """检索知识 + 组装上下文（检索事件实时下发，结果写入 outcome）。"""
        # ---- 检索知识 ----
        # 操作类请求（预约/取消/查号源/查专家）不注入知识：
        # 实测注入后模型会把请求当成"知识题"直接答题，工具调用率从 5/5 掉到 0/5。
        #
        # 关键：判定必须**带上上下文**。2026-11-10 真实联调发现，
        # 助手问"是否需要我帮您预约？"，用户回"需要" —— 这两个字没有任何
        # 操作关键词，纯文本正则漏判 → 注入知识 + 历史不中性化 →
        # 模型直接照抄历史里的"预约成功啦"，工具一次都没调。
        knowledge = ""
        last_assistant_text = _last_assistant_text(history)
        inject_knowledge = should_inject_knowledge(user_input, last_assistant_text)
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
                    # 结构化事件：此前检索步骤只进 trace，
                    # events 通道里看不到"知识是什么时候注入的"
                    yield RetrievalEvent(
                        trace_id=trace_id,
                        metadata={
                            "hit_count": len(hits),
                            "top_heading": hits[0].metadata.get("heading_path", ""),
                        },
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
        outcome["messages"] = messages
        outcome["tools"] = tools
        outcome["known_tool_names"] = known_tool_names
        outcome["operation_request"] = operation_request

    async def _chat_turn(
        self,
        *,
        messages: List[Dict[str, Any]],
        tools: List[Any],
        known_tool_names: List[str],
        state: TurnState,
        user_input: str,
        trace_id: str,
        turn_number: int,
        request_span: Any,
        result: "AgentEngineResultHolder",
        outcome: Dict[str, Any],
    ) -> AsyncIterator[Event]:
        """调模型一轮（流式 + StreamFilter + 拦截器链 + 纠正重试调度）。

        文本实时下发；response / call_error / emitted_this_turn 写入 outcome。
        """
        # ---- 调模型（流式，文本实时下发给前端）----
        with trace_span("llm.chat", trace_id=trace_id) as llm_span:
            llm_span.set_attribute("model", getattr(self.chat_model, "model", "?"))
            llm_span.set_attribute("turn", turn_number)

            response = None
            call_error: Optional[Exception] = None
            emitted_this_turn = False
            llm_started = time.monotonic()

            for attempt in range(self.retry_config.max_attempts):
                holder: Dict[str, Any] = {}
                emitted = False
                attempt_error: Optional[Exception] = None
                detected_text = ""
                # ---- 虚假承诺的**下发前**拦截 ----
                # 2026-11-10 事故：模型没调任何工具，却两次输出"预约成功"，
                # 而事后追加的免责提示是挂在"预约成功"后面的 —— 用户先读到的
                # 仍然是"预约成功"。所以必须在承诺**流式下发之前**截断，
                # 事后补救（追加提示 / 兜底文案）无法收回已显示的字。
                # 只有"承诺不成立"时才拦截：挂号/取消工具**真的成功执行过**，
                # 或本会话早前确实挂过号 —— 这时"已经预约好了"是真话。
                # 注意不能只看 successful_tool_calls：查询号源也算工具调用，
                # 但"查询成功"不等于"预约成功"。
                # （E3：回看窗口/claim 拦截/textual 切割收敛进 StreamFilter）
                stream_filter = StreamFilter(
                    claim_is_legit=state.claim_is_legit(state.prior_booking),
                    query_executed_success=state.query_executed_success,
                    metrics=self.metrics,
                    trace_id=trace_id,
                )
                try:
                    async for delta in self.chat_model.stream_chat(
                        messages, tools=tools, result_holder=holder
                    ):
                        safe = stream_filter.feed(delta)
                        if safe:
                            result.text += safe
                            emitted = True
                            emitted_this_turn = True
                            yield TextResponseEvent(
                                trace_id=trace_id, metadata={"delta": safe}
                            )
                    # 本轮结束：把回看窗口里剩下的字补发出去
                    safe = stream_filter.flush()
                    if safe:
                        result.text += safe
                        emitted = True
                        emitted_this_turn = True
                        yield TextResponseEvent(
                            trace_id=trace_id, metadata={"delta": safe}
                        )
                    candidate = holder.get("turn") or _empty_turn()
                    # 检测用**完整**文本（含被截断的承诺）；
                    # 下发给用户 / 写进历史的是**裁剪后**的文本。
                    # 两者分开，才不会因为"先把谎话删掉"而检测不到它。
                    detected_text = candidate.text
                    stream_filter.apply_truncation(candidate)
                except Exception as exc:
                    attempt_error = exc
                    candidate = _empty_turn()
                    detected_text = ""
                    # 流中断：把已经收到、但还压在回看窗口里的内容补发出去，
                    # 否则用户会凭空少看到一截
                    safe = stream_filter.flush()
                    if safe:
                        result.text += safe
                        emitted = True
                        emitted_this_turn = True
                        yield TextResponseEvent(
                            trace_id=trace_id, metadata={"delta": safe}
                        )

                finding = inspect_turn(
                    _TurnView(detected_text, candidate.tool_calls),
                    known_tool_names,
                    tool_executed=state.tool_executed_for_inspect(state.prior_booking),
                )
                # 结构化事件：谎话被就地截断也是"走了兜底路径"，
                # events 通道要能看到（裸文本模式不消费此事件，前端零改动）
                truncated_claim = stream_filter.truncated_claim
                if truncated_claim is not None:
                    yield DegradedEvent(
                        trace_id=trace_id,
                        metadata={
                            "component": "quality",
                            "reason": "claim_truncated",
                            "claim_kind": truncated_claim,
                        },
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
                        llm_span.set_attribute("textual_tool_call", True)

                    # ---- 质量防线拦截器链（E4）----
                    # 判定全部收敛在 interceptors.QualityGate；
                    # engine 只执行决定（指标/span/事件/兜底替换）。
                    gate_verdict = quality_gate.evaluate(
                        TurnFacts(
                            detected_text=detected_text,
                            response_text=response.text,
                            tool_calls=response.tool_calls,
                            finding=finding,
                            truncated_claim=truncated_claim,
                        ),
                        state,
                    )
                    if gate_verdict.hallucination_suspected:
                        self.metrics.inc("hallucination_suspected")
                        llm_span.set_attribute("hallucination_suspected", True)
                        llm_span.set_attribute(
                            "claim_kind", gate_verdict.claim_for_span
                        )
                    if gate_verdict.corrective_mode != "none":
                        if gate_verdict.corrective_mode == "override":
                            state.override_corrective(gate_verdict.corrective_kind)
                        else:
                            state.schedule_corrective(gate_verdict.corrective_kind)
                        if not gate_verdict.narrated:
                            llm_span.set_attribute(
                                "corrective_retry_scheduled", True
                            )
                            logger.warning(
                                "检测到未调工具的预约/取消承诺，准备纠正重试：trace=%s kind=%s",
                                trace_id,
                                gate_verdict.corrective_kind,
                            )
                    if gate_verdict.narrated:
                        # 模型说"我这就调用预约挂号工具"却没真调 —— 逼它真的调一次。
                        # （2026-11-10 实测：用户看到"我将调用预约挂号工具"，
                        #   然后什么都没发生，工具一次都没执行。）
                        self.metrics.inc("narrated_tool_call_retries")
                        request_span.set_attribute("narrated_tool_call", True)
                        logger.warning(
                            "模型只说要调工具却没调，准备纠正重试：trace=%s",
                            trace_id,
                        )
                    if gate_verdict.fallback_claim is not None:
                        # 纠正重试后仍然编造成功：**丢弃**这段文本，
                        # 换成陈述事实的兜底文案 —— 绝不能让"预约成功"显示给用户
                        claim = gate_verdict.fallback_claim
                        fallback = gate_verdict.fallback_text
                        prefix = (result.text or "").rstrip()
                        if prefix:
                            prefix += "\n\n"
                        result.text = prefix + fallback
                        # 落库的最终回复也要跟着换成兜底文案，
                        # 不能让被截掉的谎话文本进历史
                        state.set_final_reply(result.text)
                        yield TextResponseEvent(
                            trace_id=trace_id,
                            metadata={"delta": fallback},
                        )
                        yield DegradedEvent(
                            trace_id=trace_id,
                            metadata={
                                "component": "quality",
                                "reason": "hallucination_fallback",
                                "claim_kind": claim,
                            },
                        )
                        self.metrics.inc("hallucination_fallbacks")
                        llm_span.set_attribute("hallucination_fallback", True)
                break

            self.metrics.record_llm((time.monotonic() - llm_started) * 1000)
        outcome["response"] = response
        outcome["call_error"] = call_error
        outcome["emitted_this_turn"] = emitted_this_turn

    async def _execute_tools(
        self,
        *,
        response,
        messages: List[Dict[str, Any]],
        state: TurnState,
        user_id: str,
        trace_id: str,
        result: "AgentEngineResultHolder",
    ) -> AsyncIterator[Event]:
        """执行本轮全部工具调用，结果回填 messages（事件实时下发）。"""
        # ---- 执行工具 ----
        assistant_message = Message.assistant(response.text, tool_calls=response.tool_calls)
        state.set_narration(response.text)
        messages.append(self._assistant_to_ollama(assistant_message))

        for call in response.tool_calls:
            if state.tool_call_count >= self.max_tool_calls:
                logger.warning("工具调用次数达到上限，停止本轮工具调用")
                state.tool_results.append(
                    Message.tool_result(
                        "本轮工具调用次数已达上限，请直接给出建议。",
                        call.id,
                        call.name,
                        is_error=True,
                    )
                )
                break

            state.tool_call_count += 1
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
                    state.failure_count(call.name)
                    >= self.tool_failure_threshold
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
                    state.record_tool_success(call.name)
                else:
                    state.record_tool_failure(call.name, tool_result)

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
                    "result_preview": tool_result.content[:160],
                    "duration_ms": round(tool_result.execution_time * 1000, 2),
                },
            )

            result_message = Message.tool_result(
                tool_result.content,
                call.id,
                call.name,
                is_error=tool_result.is_error,
                error_type=tool_result.error_type,
            )
            state.add_tool_result(result_message)
            result.tool_calls.append(call)

        # ---- 工具结果回填，继续下一轮 ----
        for message in state.drain_tool_results():
            messages.append(self._tool_to_ollama(message))

