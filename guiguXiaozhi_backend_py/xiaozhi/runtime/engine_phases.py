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
from xiaozhi.runtime.intent import GREETING_REPLY, should_inject_knowledge
from xiaozhi.runtime.interceptors import TurnFacts, quality_gate
from xiaozhi.runtime.response_gate import curate_final_reply, is_write_operation, stream_claim_is_legit
from xiaozhi.runtime.state import TurnState
from xiaozhi.runtime.stream_filter import StreamFilter
from xiaozhi.security.output_guard import (
    MedicalOutputGuard,
    strip_blocked_sentences,
)
from xiaozhi.security.medical_safety import (
    BOUNDARY_PROMPT,
    SafetyVerdict,
    assess_medical_safety,
)
from xiaozhi.security.secure_executor import ToolCall as SecureToolCall
from xiaozhi.security.approval_service import get_approval_context

logger = logging.getLogger(__name__)

# 带审批 token 却没有调用任何工具时，追加给模型的强制执行指令。
#
# 2026-11-11 第十轮事故：用户点了「确认执行」→ 前端换到一次性 token → 带 token
# 重发同一句。模型这一轮输出「请在确认窗口核对上述信息并确认预约。未确认前不会
# 进行实际挂号操作。」—— 既不是成功承诺（claim 不匹配），也不是叙述承诺
# （narrated 不匹配），**三个检测全部漏判** → 零工具调用收尾 → token 白拿、
# 预约**没有发生**，而用户看到的却是"请在确认窗口核对"（确认窗早就关了）。
#
# 关键：此时**用户已经确认过了**，模型再说"请确认"是把已完成的确认又推回去，
# 而操作并不会发生 —— 用户既没约上，也没被如实告知。
APPROVAL_EXECUTION_PROMPT = (
    "（系统要求）用户已经完成确认，本次请求必须**真正调用工具**完成这次操作。"
    "请不要再次要求用户确认，也不要复述预约信息或说明尚未挂号——"
    "调用成功后请把工具返回的结果如实转述给用户。"
)

# 审批闭环开启时追加给模型的说明。
#
# 2026-11-11 真实前端联调实测（用户报"预约挂号，没有审批弹窗"）：
# 模型说「好的，正在为您预约呼吸内科的号，请稍等片刻。…请确认无误后点击
# 确认按钮完成预约。」—— 同一轮**零工具调用**：
#   1. 它把"确认"当成**自己的事**，于是不真的调工具 → 写操作没进审批门控
#      → 后端不下发审批标记 → 前端没有确认弹窗 → 用户的挂号请求石沉大海；
#   2. 它编出一个**并不存在**的"确认按钮"（系统里的确认窗由前端渲染，
#      模型无从得知），用户照着找会落空。
#
# 告诉模型"确认窗由系统弹出、你只管调工具"，既消除幻觉指令，
# 又让它走真正的工具路径 → 审批门控才有机会生效。
APPROVAL_PROMPT_NOTE = (
    "【审批说明】涉及创建或修改业务记录的操作（预约挂号、取消预约挂号等），"
    "系统会在你调用工具后**弹出确认窗**，由用户本人核对参数并点击确认，"
    "确认通过后操作才会真正执行。"
    "因此：1) 你只需要**如实调用工具**并填好参数，不要描述将要执行的动作；"
    "2) 不要让用户去点击任何按钮或自行确认——确认窗会自己弹出；"
    "3) 工具返回“需要确认”时，只需简短告知用户在确认窗口中核对即可。"
    # 2026-11-11 第十轮事故：模型在同一轮里既问用户意见、又称"我这就预约"，
    # 紧接着真的调了工具 → 确认窗弹出。用户看到的观感是
    # "我还没回答'是否同意换成呼吸内科'，弹窗就先弹了"，
    # 而弹窗里的科室是**模型自己替他选的**。所以要明确二选一：
    "4) 信息齐全就直接调用工具；需要用户表态（科室存疑、日期待定等）就先停下询问，"
    "不要在同一轮里既询问又调用写工具——确认窗只在你已经确定参数时才弹。"
)


class EnginePhases:
    # ---------- 内部阶段（上下文/模型回合/工具执行）----------

    async def _short_circuit_medical_safety(
        self,
        verdict: SafetyVerdict,
        *,
        memory_id: str,
        history: List[Message],
        user_input: str,
        result: "AgentEngineResultHolder",
        trace_id: str,
        started: float,
    ) -> AsyncIterator[Event]:
        """Phase 7：急症/越界请求的确定性收尾（不检索、不调模型、不调工具）。

        为什么必须短路而不是"提示模型按固定口径回答"：
          * 急救指引的措辞不能有一字之差（"建议就医"≠"立刻拨 120"）；
          * 越界请求一旦进入生成，7B 就一定会漏出诊断或用药结论
            （提示词约束在历史事故里被反复绕过）。

        落库仍走三段式（只有 user + assistant 两条）：固定话术必须进历史，
        否则下一轮用户说"刚才那个怎么办"时上下文是断的。
        """
        state = self._new_turn_state(history, operation_request=False)
        state.set_final_reply(verdict.reply)
        result.text = verdict.reply

        self.metrics.inc(f"medical_{verdict.kind}_total")
        logger.warning(
            "医疗安全分流（未进模型）：trace=%s kind=%s rule=%s",
            trace_id,
            verdict.kind,
            verdict.rule_id,
        )
        with trace_span("medical.safety", trace_id=trace_id) as span:
            span.set_attribute("kind", verdict.kind)
            span.set_attribute("rule_id", verdict.rule_id)
            span.set_attribute("blocked", True)

        yield TextResponseEvent(
            trace_id=trace_id,
            metadata={"delta": verdict.reply, "medical_safety": verdict.to_metadata()},
        )

        async for _ev in self._persist_and_extract(
            memory_id=memory_id,
            history=history,
            user_input=user_input,
            state=state,
            result=result,
            trace_id=trace_id,
        ):
            yield _ev

        async for _ev in self._finalize(
            result=result, state=state, turns=0, trace_id=trace_id, started=started
        ):
            yield _ev

    async def _short_circuit_greeting(
        self,
        *,
        memory_id: str,
        history: List[Message],
        user_input: str,
        result: "AgentEngineResultHolder",
        trace_id: str,
        started: float,
    ) -> AsyncIterator[Event]:
        """纯问候的确定性快路径：不检索、不调模型、不调工具，首句秒回。

        前端每次「开启新会话」都会 reload 并自动发一句"你好"（ChatWindow.vue
        的 onMounted → hello()）。这句话不含任何待办信息，但走完整 7B 生成要
        69s（本机 trace 9ef10dc2：llm.chat 69447ms，占整轮 99.8%），期间用户
        只看到一个空白气泡。寒暄文案不该交给概率生成。

        与医疗安全短路同样遵守三条：
          * 判定放在读历史之后、组装上下文之前，``model.calls == 0``；
          * 固定话术**必须落库**（三段式只有 user + assistant 两条），否则
            下一轮"刚才那个…"的上下文是断的；
          * 走 ``_finalize`` 收口指标与结束事件，trace 里留 ``greeting.fast_path``。
        """
        state = self._new_turn_state(history, operation_request=False)
        state.set_final_reply(GREETING_REPLY)
        result.text = GREETING_REPLY

        self.metrics.inc("greeting_fast_path_total")
        logger.info("问候快路径（未进模型）：trace=%s", trace_id)
        with trace_span("greeting.fast_path", trace_id=trace_id) as span:
            span.set_attribute("fast_path", True)
            span.set_attribute("model_calls", 0)

        yield TextResponseEvent(trace_id=trace_id, metadata={"delta": GREETING_REPLY})

        async for _ev in self._persist_and_extract(
            memory_id=memory_id,
            history=history,
            user_input=user_input,
            state=state,
            result=result,
            trace_id=trace_id,
        ):
            yield _ev

        async for _ev in self._finalize(
            result=result, state=state, turns=0, trace_id=trace_id, started=started
        ):
            yield _ev

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
            # ---- 审批闭环说明（Phase 2）----
            # 审批开启时，写操作会被系统拦下并弹出**确认窗**（前端渲染）。
            # 不告诉模型这件事，它就会自己编一个不存在的确认流程——
            # 2026-11-11 真实联调实测：模型输出"请确认无误后点击确认按钮完成预约"，
            # 而系统里根本没有那个按钮（用户照着找会落空），
            # 同时它把确认当成自己的事就**不会真的调工具** → 审批门控形同虚设。
            system_prompt = self.system_prompt
            if self._approval_enabled():
                system_prompt = f"{system_prompt}\n\n{APPROVAL_PROMPT_NOTE}"
                context_span.set_attribute("approval_notice_injected", True)
            # ---- Phase 7：医疗内容边界 ----
            # 非急症、非越界的正常医疗咨询（"气管炎要注意什么饮食"）照常交给模型，
            # 但必须约束口径：不许下诊断结论、不许推药、不许判断检查结果。
            # 纯办理类请求（预约/取消/查号源）不含医疗词 → 提示词零变化，
            # 保证既有工具调用行为不受影响。
            #
            # 这里重新判定一次（纯函数、无 IO）：宁可多算一次，也不让
            # _prepare_messages 的调用方必须记得传 verdict —— 漏传就是漏保护。
            safety = assess_medical_safety(user_input)
            if safety.boundary_prompt:
                system_prompt = f"{system_prompt}\n\n{BOUNDARY_PROMPT}"
                context_span.set_attribute("medical_boundary_injected", True)
                self.metrics.inc("medical_boundary_prompt_total")
            context = self.context_assembler.assemble(
                system_prompt, user_input, history, knowledge=knowledge, profile=profile
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
        # ---- Phase 4 写操作判定（Response Gate）----
        # 与 operation_request 的区别：只要"写"意图（预约/取消/退号/含身份证号），
        # 查询类（查号源/查专家）不进写模式 —— 读取类请求继续流式输出。
        outcome["write_operation"] = is_write_operation(user_input, last_assistant_text)

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
                #
                # Phase 4 写操作模式：legit 判据**只认本请求**真实执行的写操作。
                # 历史里的 prior_booking（"会话早前真的挂过号"）在写模式下不再
                # 为完成态承诺背书 —— 真机残留（8.4 第 6 条）：用户不带 token
                # 重发同一句"帮我预约…"，历史里有成功记录 → 旧判据判真 →
                # 模型复述"预约成功"原样下发，而本次请求什么都没做。
                # 查询类请求不受影响（根本不进写模式）。
                if state.write_operation:
                    legit = stream_claim_is_legit(state)
                else:
                    legit = state.claim_is_legit(state.prior_booking)
                stream_filter = StreamFilter(
                    claim_is_legit=legit,
                    query_executed_success=state.query_executed_success,
                    metrics=self.metrics,
                    trace_id=trace_id,
                )
                # ---- Phase 9-A：医疗回复出口护栏 ----
                # 入口（medical_safety）拦的是"越界请求"，这里拦的是"正常请求
                # 但模型自己漏出用药/诊断"。两层串联：StreamFilter 先做承诺截断，
                # 再交给出口护栏按句扫描 —— 顺序不能反，否则被截断的承诺
                # 会把后面的句子粘成一句，扫描边界就错了。
                guard = MedicalOutputGuard(
                    enabled=self._output_guard_enabled(),
                    metrics=self.metrics,
                    trace_id=trace_id,
                )
                try:
                    async for delta in self.chat_model.stream_chat(
                        messages, tools=tools, result_holder=holder
                    ):
                        safe = stream_filter.feed(delta)
                        safe = guard.feed(safe) if safe else ""
                        if safe:
                            result.text += safe
                            emitted = True
                            emitted_this_turn = True
                            yield TextResponseEvent(
                                trace_id=trace_id, metadata={"delta": safe}
                            )
                    # 本轮结束：把回看窗口里剩下的字补发出去
                    safe = guard.feed(stream_filter.flush())
                    tail = guard.flush()
                    if tail:
                        safe = (safe or "") + tail
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
                    # 出口护栏丢掉的句子同样不能进历史：叙述段会以
                    # assistant(tool_calls, content=叙述段) 的形式落进 Mongo，
                    # 落历史等于把"吃点感冒药"教给下一轮的 7B。
                    if guard.blocked_any:
                        candidate.text = strip_blocked_sentences(candidate.text)
                        yield DegradedEvent(
                            trace_id=trace_id,
                            metadata={
                                "component": "medical_safety",
                                "reason": "medical_output_blocked",
                                "rules": guard.blocked_rules[:3],
                            },
                        )
                except Exception as exc:
                    attempt_error = exc
                    candidate = _empty_turn()
                    detected_text = ""
                    # 流中断：把已经收到、但还压在回看窗口里的内容补发出去，
                    # 否则用户会凭空少看到一截
                    safe = guard.feed(stream_filter.flush())
                    tail = guard.flush()
                    if tail:
                        safe = (safe or "") + tail
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
                    tool_executed=legit,
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

                # ---- Phase 5：工具执行**前**落 STARTED ----
                # 崩溃恢复时据此判断"这个工具可能有副作用"，标记为 UNKNOWN
                # 后必须先查业务事实，不可盲目重试（否则重复挂号）。
                tool_exec_id = None
                if self.task_tracker is not None:
                    tool_exec_id = self.task_tracker.tool_started(
                        call.id, call.name, call.arguments
                    )

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
                # ---- Phase 4：写工具的结构化结果单独入账 ----
                # 完成态表述以业务服务的 ToolResult 为准（不让模型总结成败）；
                # UNKNOWN（authoritative_status）在请求收尾时换固定文案。
                state.record_write_result(call.name, tool_result)

                # ---- Phase 5：工具执行后落终态（写工具成功 → 副作用已提交）----
                if self.task_tracker is not None:
                    self.task_tracker.tool_finished(
                        tool_exec_id, call.name, success=tool_result.success
                    )

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

    def _approval_enabled(self) -> bool:
        """审批闭环是否已装配（写操作需一次性确认）。

        以执行器上的 ``approval_service`` 为准 —— 灰度关闭时它是 None，
        此时不注入审批说明（提示词与一期完全一致）。
        """
        executor = getattr(self, "secure_executor", None)
        return getattr(executor, "approval_service", None) is not None

    def _output_guard_enabled(self) -> bool:
        """医疗回复出口扫描是否开启（Phase 9-A）。

        默认开：它的规则只拦"用药建议 / 诊断结论"，正常业务回复（预约成功、
        号源列表、医院流程）不会命中，关掉它没有任何性能收益，只有漏出风险。
        想复现历史行为（比如做对照实验）时用 ``MEDICAL_OUTPUT_GUARD_ENABLED=false``。
        """
        try:
            from xiaozhi.config import get_settings

            return bool(get_settings().medical_output_guard_enabled)
        except Exception:  # pragma: no cover - 配置读不到时按安全侧默认
            logger.warning("读不到 medical_output_guard_enabled，按开启处理")
            return True

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

    def _has_pending_approval_token(self) -> bool:
        """本请求是否带了有效的审批 token（用户已点过「确认执行」）。

        请求级 contextvar 由 routes 层设置 —— 模型输出永远碰不到它。
        """
        try:
            ctx = get_approval_context()
        except Exception:  # pragma: no cover - contextvar 未设置时的兜底
            return False
        return bool(getattr(ctx, "approval_token", ""))

    def _retry_with_approval_hint(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Any],
        state: TurnState,
        response: Any,
        trace_id: str,
    ) -> bool:
        """带审批 token 却没有工具调用 → 追加强制指令再试一轮。

        2026-11-11 第十轮事故：用户点了「确认执行」，前端带一次性 token 重发，
        模型却输出「请在确认窗口核对上述信息并确认预约。未确认前不会进行实际
        挂号操作。」—— 既不是成功承诺也不是叙述承诺，三个检测全部漏判 →
        零工具调用收尾 → **token 白拿、预约没有发生**，而用户看到的却是
        "请在确认窗口核对"（确认窗早就关了，用户会以为系统没认出他已经确认）。

        这里不能靠正则穷举模型的说法，改为在**引擎层**兜底：
        带 token 的请求必须产生写操作，否则补一轮"必须真的调工具"的强指令。

        返回 True 表示已追加指令、调用方应当再跑一轮。
        """
        # 独立计数：**最多补一轮**。
        #
        # 不能用 ``state.corrective_used`` 判断 —— 那是"纠正重试被消费的次数"，
        # 而本方法走的是另一条路径（直接 append 指令），不会经过 consume_corrective，
        # 于是 ``corrective_used`` 一直是 0 → 每次都返回 True → 无限重试。
        # 真机实测：模型被追问后连续 5 轮重复同一句失败原因，
        # 用户看到同一段话被刷了 5 遍。
        # 前置条件：本请求必须真的带了审批 token（routes 层经 contextvar 下发）。
        # 没带 token 的零工具调用轮走纠正重试（_schedule_corrective_retry），
        # 与本兜底互不相干。
        if not self._has_pending_approval_token():
            return False
        if state.approval_forced_retry_done:
            logger.warning(
                "带审批 token 仍未调用工具，放弃强制重试（避免重复刷屏）：trace=%s",
                trace_id,
            )
            return False
        # 2026-11-11 Phase 3a 真机补充：判据必须是「**本次请求**从未调过工具」
        # （tool_call_count==0），而不是「本轮没有 tool_calls」——
        # 执行轮正常时序是"消费 token → 调工具（如返回 ALREADY_EXISTS）→
        # 模型下一轮转述结果"，转述轮本来就不该再调工具。
        # 漏了累计条件会把如实转述误判成"token 白拿"，强制重试一轮 →
        # 同一句在界面上重复输出两遍（8082 真机实测）。
        if state.tool_call_count > 0:
            logger.debug(
                "带审批 token 的请求已发生过工具调用（count=%d），无需强制重试：trace=%s",
                state.tool_call_count,
                trace_id,
            )
            return False
        state.approval_forced_retry_done = True
        messages.append(
            {
                "role": "user",
                "content": APPROVAL_EXECUTION_PROMPT,
            }
        )
        logger.warning(
            "带审批 token 却未调用工具，追加强制执行指令重试：trace=%s", trace_id
        )
        return True

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
                # ---- 带有效审批 token 的请求：模型**必须**真的调工具 ----
                # （2026-11-11 第十轮事故）用户点了「确认执行」→ 前端换到一次性
                # token → 带 token 重发同一句。这一轮模型可能输出
                # 「请在确认窗口核对上述信息并确认预约。未确认前不会进行实际挂号操作。」
                # —— 既不是成功承诺（claim 不匹配），也不是叙述承诺（narrated 不匹配），
                # **三个检测全部漏判** → 模型零工具调用收尾 → token 白拿、
                # 预约**没有发生**，而用户看到的却是"请在确认窗口核对"
                # （确认窗早就关了）。
                #
                # 这类句子靠正则很难穷举（模型有无数种说法），必须在引擎层兜底：
                # **有 token 进来，就一定要有写操作发生**。
                if self._retry_with_approval_hint(
                    messages, tools, state, response, trace_id
                ):
                    turn += 1
                    yield TurnEndEvent(
                        trace_id=trace_id,
                        metadata={"turn_number": turn, "corrective_retry": True},
                    )
                    continue
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

    async def _finalize_response_gate(
        self,
        state: TurnState,
        result: "AgentEngineResultHolder",
        trace_id: str,
    ) -> AsyncIterator[Event]:
        """写操作请求收尾的确定性门控（Phase 4 / P0-05）。

        纠正重试是"逼模型再说一次"（可能再漏），本阶段不看模型说什么，
        只看**业务事实**（state.write_results）：本请求没有真实执行写操作，
        最终回复里就绝不允许站着一句完成态承诺 —— 漏判的换皮句式在这里
        被整段替换，而不是只追加免责声明（审计 P0-05 验收 5）。
        审批短路轮不走这里（clean 文案本就是结构化结果）。
        """
        if not state.write_operation or state.approval_required:
            return
        curated, reason = curate_final_reply(state, result.text)
        if reason is None:
            return
        logger.warning(
            "Response Gate 替换最终回复：trace=%s reason=%s", trace_id, reason
        )
        self.metrics.inc("response_gate_blocks")
        result.text = curated
        # 落库的最终回复同步替换 —— 被拦下的承诺文本绝不能进历史
        # （教训：叙述/谎话进历史就是教模型照抄）
        state.set_final_reply(curated)
        yield TextResponseEvent(trace_id=trace_id, metadata={"delta": curated})
        yield DegradedEvent(
            trace_id=trace_id,
            metadata={"component": "response_gate", "reason": reason},
        )

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

        # ---- Phase 5：任务终态化 ----
        # 有 error → FAILED；否则 SUCCEEDED。审批短路（等待用户确认）不是
        # 失败 —— 那种情况任务停在 WAITING_APPROVAL，由审批服务自己的
        # 状态机管理（approval_requests 表）。
        if self.task_tracker is not None:
            if result.error:
                self.task_tracker.finish("FAILED")
            elif state.approval_required:
                self.task_tracker.finish("WAITING_APPROVAL")
            else:
                self.task_tracker.finish("SUCCEEDED")

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

