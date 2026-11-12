"""单轮模型调用协作者 —— B2 第一批从 ``EnginePhases._chat_turn`` 抽出。

职责边界（对齐《主体Agent生产化架构最终整改方案》§3.1 / §4 P1-1.1）：
    * **只负责**「调一次模型 + 流式文本 / StreamFilter / 医疗出口护栏 /
      质量拦截器链 + 把本轮结果写进 ``outcome``」；
    * **不负责**主循环控制、工具执行、落库、Response Gate、
      ``AgentEndEvent`` 与任务终态化 —— 那些仍属于 Loop / Finalizer。

依赖全部显式传入，不通过继承 mixin 访问 ``self`` 上的私有属性（§3.2）：
    * 两个判定型 Port 在构造时注入（请求级 contextvar / 配置，随时现取）；
    * ``chat_model`` / ``retry_config`` / ``metrics`` 在每次 ``chat_turn()``
      调用时传入，**不在构造期快照** —— 既有装配与单测用
      ``engine.chat_model = FakeChatModel(...)`` 在构造后替换模型，
      快照会让它们静默失效。

本批是「先抽协作者、再迁移方法、最后删 mixin」的第一步：方法体逐字平移，
``EnginePhases._chat_turn`` 保留为一行委托壳，行为与事件顺序零变更。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, AsyncIterator, Callable, Dict, List, Optional

if TYPE_CHECKING:  # 仅供类型标注，避免与 engine.py 循环导入
    from xiaozhi.runtime.engine import AgentEngineResultHolder

from xiaozhi.core.event import DegradedEvent, Event, TextResponseEvent
from xiaozhi.models.quality import inspect_turn
from xiaozhi.reliability.tracing import trace_span
from xiaozhi.runtime.engine_helpers import _TurnView, _empty_turn
from xiaozhi.runtime.interceptors import TurnFacts, quality_gate
from xiaozhi.runtime.response_gate import stream_claim_is_legit
from xiaozhi.runtime.state import TurnState
from xiaozhi.runtime.stream_filter import StreamFilter
from xiaozhi.security.output_guard import (
    MedicalOutputGuard,
    strip_blocked_sentences,
)

logger = logging.getLogger(__name__)


class ModelRunner:
    """一次模型回合的执行者（无跨请求状态，可安全复用）。"""

    def __init__(
        self,
        *,
        has_pending_approval_token: Callable[[], bool],
        output_guard_enabled: Callable[[], bool],
    ) -> None:
        # 判定型依赖以 callable Port 注入：过渡期由 AgentEngine 绑定 mixin 上的
        # 同名方法，后续批次会把这两个判定也迁到显式 Port 实现。
        self.has_pending_approval_token = has_pending_approval_token
        self.output_guard_enabled = output_guard_enabled

    async def chat_turn(
        self,
        *,
        chat_model,
        retry_config,
        metrics,
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
        # ---- 审批执行轮：文本先扣住，等"到底有没有真调工具"再决定要不要发 ----
        # 2026-11-12 真机：用户点了确认，模型输出「请在确认窗口核对…未确认前不会
        # 进行实际挂号操作」—— 这句话**与随后发生的事实相反**（服务端会按审批
        # 记录真的执行），可它是流式下发的，字已经到用户眼前收不回来。
        # 带 token 的这一轮里，模型的话在"看到它到底调没调工具"之前都不可信，
        # 所以先攒进 held_deltas，由调用方在拿到 response 之后决定丢弃还是补发：
        #   * 真调了工具 → 补发（正常叙述轮，行为不变）；
        #   * 零工具调用且服务端已完成确定性执行 → 丢弃（用户只看到真实结果）；
        #   * 其余情况（无 token / token 无效）→ 补发（行为与之前完全一致）。
        held_deltas: List[str] = []
        hold_stream = self.has_pending_approval_token()

        def _text_event(text: str) -> Optional[TextResponseEvent]:
            """要下发的文本 → 事件；被扣住时返回 None（由调用方统一补发）。"""
            if hold_stream:
                held_deltas.append(text)
                return None
            return TextResponseEvent(trace_id=trace_id, metadata={"delta": text})

        # ---- 调模型（流式，文本实时下发给前端）----
        with trace_span("llm.chat", trace_id=trace_id) as llm_span:
            llm_span.set_attribute("model", getattr(chat_model, "model", "?"))
            llm_span.set_attribute("turn", turn_number)

            response = None
            call_error: Optional[Exception] = None
            emitted_this_turn = False
            llm_started = time.monotonic()

            for attempt in range(retry_config.max_attempts):
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
                    metrics=metrics,
                    trace_id=trace_id,
                )
                # ---- Phase 9-A：医疗回复出口护栏 ----
                # 入口（medical_safety）拦的是"越界请求"，这里拦的是"正常请求
                # 但模型自己漏出用药/诊断"。两层串联：StreamFilter 先做承诺截断，
                # 再交给出口护栏按句扫描 —— 顺序不能反，否则被截断的承诺
                # 会把后面的句子粘成一句，扫描边界就错了。
                guard = MedicalOutputGuard(
                    enabled=self.output_guard_enabled(),
                    metrics=metrics,
                    trace_id=trace_id,
                )
                try:
                    async for delta in chat_model.stream_chat(
                        messages, tools=tools, result_holder=holder
                    ):
                        safe = stream_filter.feed(delta)
                        safe = guard.feed(safe) if safe else ""
                        if safe:
                            result.text += safe
                            emitted = True
                            emitted_this_turn = True
                            event = _text_event(safe)
                            if event is not None:
                                yield event
                    # 本轮结束：把回看窗口里剩下的字补发出去
                    safe = guard.feed(stream_filter.flush())
                    tail = guard.flush()
                    if tail:
                        safe = (safe or "") + tail
                    if safe:
                        result.text += safe
                        emitted = True
                        emitted_this_turn = True
                        event = _text_event(safe)
                        if event is not None:
                            yield event
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
                        event = _text_event(safe)
                        if event is not None:
                            yield event

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
                if retryable and attempt < retry_config.max_attempts - 1:
                    metrics.inc("empty_response_retries")
                    llm_span.set_attribute("empty_response_retries", attempt + 1)
                    logger.warning(
                        "模型响应为空/异常，重试 %s/%s：trace=%s reason=%s",
                        attempt + 1,
                        retry_config.max_attempts - 1,
                        trace_id,
                        ";".join(finding.reasons) or type(attempt_error).__name__,
                    )
                    await asyncio.sleep(retry_config.delay_for(attempt + 1))
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
                        metrics.inc("hallucination_suspected")
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
                        metrics.inc("narrated_tool_call_retries")
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
                        event = _text_event(fallback)
                        if event is not None:
                            yield event
                        yield DegradedEvent(
                            trace_id=trace_id,
                            metadata={
                                "component": "quality",
                                "reason": "hallucination_fallback",
                                "claim_kind": claim,
                            },
                        )
                        metrics.inc("hallucination_fallbacks")
                        llm_span.set_attribute("hallucination_fallback", True)
                break

            metrics.record_llm((time.monotonic() - llm_started) * 1000)
        outcome["response"] = response
        outcome["call_error"] = call_error
        outcome["emitted_this_turn"] = emitted_this_turn
        # 被扣住的文本（审批执行轮）交给调用方决策：真调了工具就补发，
        # 服务端确定性执行了就直接丢弃（用户只看到真实结果）。
        outcome["held_deltas"] = held_deltas
