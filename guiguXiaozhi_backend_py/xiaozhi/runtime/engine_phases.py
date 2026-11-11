"""engine 的三个执行阶段（上下文组装 / 模型回合 / 工具执行）—— E6 拆分。

以 mixin 形式供 AgentEngine 继承：方法通过 self 访问共享设施
（chat_model / metrics / retry_config / 拦截器链 / StreamFilter…），
事件实时 yield 给 run() 透传。逻辑自 engine.py 原样平移，行为零变更。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, AsyncIterator, Dict, List, Optional

if TYPE_CHECKING:  # 仅供类型标注，避免与 engine.py 循环导入
    from xiaozhi.runtime.engine import AgentEngineResultHolder

from xiaozhi.core.event import (
    AgentEndEvent,
    ApprovalRequiredEvent,
    DegradedEvent,
    Event,
    RetrievalEvent,
    TextResponseEvent,
    ToolExecuteEvent,
    ToolResultEvent,
    TurnEndEvent,
    TurnStartEvent,
)
from xiaozhi.core.message import Message
from xiaozhi.core.tool import ToolResult
from xiaozhi.models.quality import booking_executed_in_history, corrective_prompt, inspect_turn
from xiaozhi.reliability.resilience import degraded_reply
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
            # ---- 患者画像（P2-M4）：只在开关打开时非空；失败返回空串不阻断 ----
            profile = ""
            if getattr(self, "profile_injector", None) is not None:
                profile = await self.profile_injector.build_segment(user_input, history)
                if profile:
                    context_span.set_attribute("profile_injected", True)
            context = self.context_assembler.assemble(
                self.system_prompt, user_input, history, knowledge=knowledge, profile=profile
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

            # ---- Phase 2：审批门控拦下写工具，立刻短路本轮 ----
            # 不能把「需要确认」的工具结果继续喂给模型：7b 模型会编造
            # 「已为您确认并预约成功」。这里直接给用户可读提示 + 结构化审批事件。
            approval_payload = (tool_result.metadata or {}).get("approval") or {}
            if approval_payload:
                clean = approval_payload.get("message") or "该操作需要您确认后才会执行。"
                state.mark_approval_required(approval_payload)
                state.set_final_reply(clean)
                result.text = clean
                state.add_tool_result(
                    Message.tool_result(clean, call.id, call.name, is_error=False)
                )
                result.tool_calls.append(call)
                yield TextResponseEvent(trace_id=trace_id, metadata={"delta": clean})
                yield ApprovalRequiredEvent(
                    trace_id=trace_id,
                    metadata=dict(approval_payload),
                )
                break

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

    # ---------- 编排子步骤（run 的单一职责拆分，逻辑自 run() 原样平移） ----------

    async def _load_history(self, memory_id: str, trace_id: str) -> List[Message]:
        """读会话历史（最近 20 条），留 memory.load 跨度。"""
        with trace_span("memory.load", trace_id=trace_id) as memory_span:
            history = await self.conversation_store.load(memory_id, limit=20)
            memory_span.set_attribute("message_count", len(history))
        return history

    def _new_turn_state(self, history: List[Message], operation_request: bool) -> TurnState:
        """请求级跨轮状态（E2 收敛）。

        prior_booking：历史里是否真的执行过预约（本次会话早前请求成功挂过号且未取消）。
        是真话依据：模型说"已经预约好了"时不算幻觉，也不做纠正重试。
        """
        return TurnState(
            prior_booking=booking_executed_in_history(history),
            operation_request=operation_request,
        )

    def _time_budget_exceeded(self, started: float) -> bool:
        """请求级时间预算是否已用尽。"""
        return (
            self.request_timeout is not None
            and time.monotonic() - started > self.request_timeout
        )

    def _circuit_is_open(self) -> bool:
        """模型熔断器是否处于打开状态。"""
        return self.circuit_breaker is not None and not self.circuit_breaker.allow()

    async def _degrade_on_time_budget(
        self, user_input: str, trace_id: str, result: "AgentEngineResultHolder"
    ) -> AsyncIterator[Event]:
        """时间预算用尽：返回已生成内容 + 提示语（不再调模型）。"""
        result.degraded = True
        yield DegradedEvent(
            trace_id=trace_id,
            metadata={"component": "request", "reason": "time_budget_exceeded"},
        )
        hint = "\n\n（响应已达时间上限，先返回已生成的内容。）"
        result.text += hint
        yield TextResponseEvent(trace_id=trace_id, metadata={"delta": hint})

    async def _degrade_on_circuit_open(
        self, user_input: str, trace_id: str, result: "AgentEngineResultHolder"
    ) -> AsyncIterator[Event]:
        """模型熔断打开：直接返回本地规则分导诊建议。"""
        result.degraded = True
        fallback = degraded_reply(user_input)
        result.text += fallback
        yield TextResponseEvent(trace_id=trace_id, metadata={"delta": fallback})
        yield DegradedEvent(
            trace_id=trace_id,
            metadata={"component": "model", "reason": "circuit_open"},
        )
        logger.warning("模型熔断器打开，返回规则应答：trace=%s", trace_id)

    async def _degrade_on_model_error(
        self,
        call_error: Exception,
        emitted_this_turn: bool,
        user_input: str,
        trace_id: str,
        result: "AgentEngineResultHolder",
    ) -> AsyncIterator[Event]:
        """模型调用失败：记录熔断并给可读降级文案（不暴露内部错误）。

        已向用户吐出过文本时不再补降级文案，否则前端会出现重复文字。
        """
        if self.circuit_breaker is not None:
            self.circuit_breaker.record_failure()
        result.error = str(call_error)
        result.degraded = True
        logger.warning("模型调用失败：trace=%s error=%s", trace_id, call_error)
        if not emitted_this_turn:
            fallback = degraded_reply(user_input)
            result.text += fallback
            yield TextResponseEvent(trace_id=trace_id, metadata={"delta": fallback})
        yield DegradedEvent(
            trace_id=trace_id,
            metadata={"component": "model", "error": str(call_error)},
        )

    def _schedule_corrective_retry(
        self,
        response,
        messages: List[Dict[str, Any]],
        state: TurnState,
    ) -> bool:
        """无工具调用收尾：记录最终回复，命中幻觉/误报则追加纠正提示。

        返回 True 表示需要纠正重试一轮（调用方负责 turn+=1 并 continue）。
        narrated 叙述承诺检测已收敛进拦截器链（_chat_turn）。
        """
        state.set_final_reply(response.text)
        corrective_fired, corrective_kind = state.consume_corrective()
        if not corrective_fired:
            return False
        if corrective_kind != "action":
            self.metrics.inc("false_confirmation_retries")
        messages.append(
            {"role": "user", "content": corrective_prompt(corrective_kind)}
        )
        return True

    async def _run_loop(
        self,
        *,
        messages: List[Dict[str, Any]],
        tools: List[Any],
        known_tool_names: List[str],
        state: TurnState,
        user_input: str,
        user_id: str,
        trace_id: str,
        request_span,
        started: float,
        result: "AgentEngineResultHolder",
        outcome: Dict[str, Any],
    ) -> AsyncIterator[Event]:
        """主循环：预算/熔断守门 → 调模型 → 出错降级 / 无工具收尾 / 执行工具。

        结束时把最终轮数写入 outcome["turns"]（run 收口与 AgentEnd 要用）。
        """
        turn = 0
        while turn < self.max_turns:
            # ---- 守门一：请求级时间预算 ----
            if self._time_budget_exceeded(started):
                async for _ev in self._degrade_on_time_budget(user_input, trace_id, result):
                    yield _ev
                break

            # ---- 守门二：模型熔断器打开时直接本地降级 ----
            if self._circuit_is_open():
                async for _ev in self._degrade_on_circuit_open(user_input, trace_id, result):
                    yield _ev
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
                request_span=request_span,
                result=result,
                outcome=turn_outcome,
            ):
                yield _ev
            response = turn_outcome.get("response")
            call_error = turn_outcome.get("call_error")
            emitted_this_turn = turn_outcome.get("emitted_this_turn", False)

            # ---- 模型调用失败 → 降级并结束 ----
            if call_error is not None:
                async for _ev in self._degrade_on_model_error(
                    call_error, emitted_this_turn, user_input, trace_id, result
                ):
                    yield _ev
                break

            if self.circuit_breaker is not None:
                self.circuit_breaker.record_success()

            if response is None:
                response = _empty_turn()

            # ---- 没有工具调用 → 结束（或纠正重试一轮） ----
            if not response.tool_calls:
                if self._schedule_corrective_retry(response, messages, state):
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

            # 审批短路：本请求到此为止，不再进入下一轮模型调用
            if state.approval_required:
                turn += 1
                yield TurnEndEvent(
                    trace_id=trace_id,
                    metadata={"turn_number": turn, "approval_required": True},
                )
                break

            turn += 1
            yield TurnEndEvent(
                trace_id=trace_id,
                metadata={"turn_number": turn, "has_tool_calls": True},
            )

        outcome["turns"] = turn

    async def _persist_and_extract(
        self,
        *,
        memory_id: str,
        history: List[Message],
        user_input: str,
        state: TurnState,
        result: "AgentEngineResultHolder",
        trace_id: str,
    ) -> AsyncIterator[Event]:
        """落库（失败不阻断，组装逻辑见 runtime/persister.py）+ 画像抽取钩子。"""
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
                # ---- 画像抽取（P2-M6，fire-and-forget）----
                # 落库成功后才触发；失败只记日志，绝不影响本轮响应。
                if self.profile_service is not None:
                    asyncio.get_running_loop().create_task(
                        self.profile_service.maybe_extract(
                            user_input, new_messages, trace_id=trace_id
                        )
                    )
            except Exception as exc:
                write_span.set_error(f"{type(exc).__name__}: {exc}")
                result.degraded = True
                yield DegradedEvent(
                    trace_id=trace_id,
                    metadata={"component": "memory", "error": str(exc)},
                )

    async def _finalize(
        self,
        *,
        result: "AgentEngineResultHolder",
        state: TurnState,
        turns: int,
        trace_id: str,
        started: float,
    ) -> AsyncIterator[Event]:
        """指标收口 + AgentEnd 结束事件。"""
        self.metrics.observe("request_duration_ms", (time.monotonic() - started) * 1000)
        if result.error:
            self.metrics.inc("errors_total")
        if result.degraded:
            self.metrics.inc("degraded_total")

        yield AgentEndEvent(
            trace_id=trace_id,
            metadata={
                "turns": turns,
                "tool_calls": [c.name for c in result.tool_calls],
                "text_length": len(result.text),
                "degraded": result.degraded,
                "corrective_retries": state.corrective_used,
            },
        )

