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
from typing import Any, AsyncIterator, Dict, List, Optional, Sequence

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
    booking_executed_in_history,
    claim_kind,
    claim_span,
    corrective_prompt,
    detect_narrated_tool_call,
    detect_textual_tool_call,
    fallback_for,
    textual_tool_call_span,
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

# 流式下发的**回看窗口**（字）。
#
# 承诺正则要等匹配文本完整出现才命中，而命中点比承诺**起点**晚十几个字
# （"已为您预约成功"要等到"预约"出现才命中，起点却在更早的"已"）。
# 不留窗口就会先漏出"已"再被截断 —— 用户看到半截承诺。
# 窗口必须 >= 最长承诺正则的长度（当前最长约 20 字），取 40 留足余量。
LOOKBEHIND = 40


class _TurnView:
    """只读的模型输出视图 —— 用来在"检测"和"下发"之间传递不同的文本。"""

    __slots__ = ("text", "tool_calls")

    def __init__(self, text: str, tool_calls: Any) -> None:
        self.text = text
        self.tool_calls = tool_calls


# 句子边界（截断时保留到这里为止，避免留下半截句子）
_SENTENCE_END = "。！？!?\n"
# 次级边界：没有完整句子时，裁到最近的逗号，至少保留已经说清的信息
_CLAUSE_END = "，,、;；"


def _trim_to_sentence(text: str) -> str:
    """把文本裁到最后一个完整句子的边界。

    截断承诺时，承诺之前的前缀往往停在半句上
    （"很遗憾，您在2026年11月11日上午" + 兜底文案 → 读起来像病句）。

    裁剪策略：
      1. 有完整句子 → 裁到最后一个句末标点；
      2. 只有逗号分句 → 裁到最后一个逗号（保留已经说清的信息）；
      3. 连逗号都没有 → 整段丢弃（半句话比不完整更糟）。

    **保留标点**：裁出来的"很遗憾，"要带着逗号，
    后面接下一句时读起来才通顺（"很遗憾，抱歉，本次预约…"）。
    """
    if not text:
        return ""
    cut = -1
    for index, char in enumerate(text):
        if char in _SENTENCE_END:
            cut = index + 1
    if cut >= 0:
        return text[:cut].rstrip()
    for index, char in enumerate(text):
        if char in _CLAUSE_END:
            cut = index + 1
    if cut < 0:
        return ""
    return text[:cut].rstrip()


def _safe_prefix(turn_buffer: str, full_text: str) -> str:
    """从完整输出里切出"实际下发给用户"的那一段（去掉被截断的承诺及其后续）。

    ``turn_buffer`` 是流式拼出来的完整文本，``full_text`` 是 holder 里的
    同一段文本；两者一致时直接按承诺起点裁剪，不一致时保守返回空串。

    裁剪后再裁到句子边界 —— 承诺前的半截句子对用户没有意义。
    """
    if not full_text:
        return ""
    if turn_buffer.startswith(full_text):
        span = claim_span(full_text)
        if span is None:
            return full_text
        return _trim_to_sentence(full_text[: span[1]])
    # 兜底：holder 文本与流式缓冲不一致（理论上不该发生）→ 宁可丢也别漏
    return ""


def _last_assistant_text(history: Sequence[Any]) -> str:
    """取历史里最后一条 assistant 回复的正文（用于上下文相关的意图判定）"""
    for message in reversed(history or []):
        if getattr(message, "role", None) == Role.ASSISTANT:
            return str(getattr(message, "content", "") or "")
    return ""


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
            # 请求级工具真实执行结果：打在持久化消息上，供下一请求的
            # booking_executed_in_history 判定"历史里是否真的成功预约过"。
            # 只看 tool_calls 会把失败调用也当成已预约（2026-11-10 事故根因）。
            booking_executed_success = False
            cancel_executed_success = False
            # 本请求里查询工具（号源/科室专家）真实执行成功过 →
            # 模型转述"查询结果显示…"是真话，不能当编造拦截
            # （booking/cancel 的 legit 标记不覆盖查询，需要单独跟踪）。
            query_executed_success = False
            tool_results: List[Message] = []
            tool_failures: Dict[str, int] = {}
            # 最近一次工具失败（工具名 / 错误类型 / 原始反馈），
            # 用于生成**与事实相符**的兜底文案
            last_tool_failure: Optional[Dict[str, Any]] = None
            # 历史里是否真的执行过预约（本次会话早前请求成功挂过号且未取消）。
            # 是真话依据：模型说"已经预约好了"时不算幻觉，也不做纠正重试。
            prior_booking = booking_executed_in_history(history)
            corrective_used = 0     # 已做过的纠正重试次数（每请求至多 1 次）
            corrective_pending = False  # 本轮检测到幻觉且待纠正
            corrective_kind: Optional[str] = None  # 幻觉承诺的操作类型（book/cancel）

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
                        # 放在 try 外面：流异常时也能把已收到的内容补发出去
                        turn_buffer = ""
                        pending_start = 0
                        suppressed = False
                        truncated_claim: Optional[str] = None
                        textual_tool_call = False
                        detected_text = ""
                        try:
                            # ---- 虚假承诺的**下发前**拦截 ----
                            # 2026-11-10 事故：模型没调任何工具，却两次输出"预约成功"，
                            # 而事后追加的免责提示是挂在"预约成功"后面的 —— 用户先读到的
                            # 仍然是"预约成功"。所以必须在承诺**流式下发之前**截断，
                            # 事后补救（追加提示 / 兜底文案）无法收回已显示的字。
                            #
                            # **回看窗口**：承诺的正则要等"匹配文本完整出现"才能命中，
                            # 而它的**起点**可能比命中点早十几个字（"已为您预约成功"
                            # 要等到"预约"两个字出现才命中，起点却在更早的"已"）。
                            # 所以这里留出 LOOKBEHIND 个字不立即下发，
                            # 承诺就能在"露头"之前被拦下来。
                            # 只有"承诺不成立"时才拦截：挂号/取消工具**真的成功执行过**，
                            # 或本会话早前确实挂过号 —— 这时"已经预约好了"是真话。
                            # 注意不能只看 successful_tool_calls：查询号源也算工具调用，
                            # 但"查询成功"不等于"预约成功"。
                            claim_is_legit = (
                                booking_executed_success
                                or cancel_executed_success
                                or prior_booking
                            )
                            async for delta in self.chat_model.stream_chat(
                                messages, tools=tools, result_holder=holder
                            ):
                                if not delta:
                                    continue
                                turn_buffer += delta
                                if suppressed:
                                    # 承诺之后的内容一律不下发（但**继续读完流**，
                                    # 否则拿不到 tool_calls，工具就永远不会被执行）
                                    continue
                                # 「把工具调用写成纯文本 JSON」也要拦在下发之前：
                                # 原始参数漏给用户是一方面，更关键的是
                                # 工具一次都没执行 —— 用户以为在办事，其实什么都没发生。
                                textual_span = (
                                    None
                                    if claim_is_legit
                                    else textual_tool_call_span(turn_buffer)
                                )
                                if textual_span is not None:
                                    textual_tool_call = True
                                    suppressed = True
                                    # 精确切掉"工具名 + JSON"这一段，
                                    # 切割点不对就会把参数漏给用户
                                    safe = _trim_to_sentence(
                                        turn_buffer[pending_start : textual_span[0]]
                                    )
                                    if safe:
                                        result.text += safe
                                        emitted = True
                                        emitted_this_turn = True
                                        yield TextResponseEvent(
                                            trace_id=trace_id, metadata={"delta": safe}
                                        )
                                    pending_start = len(turn_buffer)
                                    self.metrics.inc("textual_tool_call_truncated")
                                    logger.warning(
                                        "模型把工具调用写成了纯文本，就地截断：trace=%s",
                                        trace_id,
                                    )
                                    continue
                                match = None if claim_is_legit else claim_span(turn_buffer)
                                if (
                                    match is not None
                                    and match[0] == "query"
                                    and query_executed_success
                                ):
                                    # 查询工具真的执行过 → "查询结果显示…"是真话
                                    match = None
                                if match is not None:
                                    kind, start, _end = match
                                    truncated_claim = kind
                                    suppressed = True
                                    # 承诺之前的前缀无害，补发出去（承诺本身被丢掉）
                                    safe = _trim_to_sentence(turn_buffer[pending_start:start])
                                    if safe:
                                        result.text += safe
                                        emitted = True
                                        emitted_this_turn = True
                                        yield TextResponseEvent(
                                            trace_id=trace_id, metadata={"delta": safe}
                                        )
                                    pending_start = len(turn_buffer)
                                    self.metrics.inc("false_confirmation_truncated")
                                    logger.warning(
                                        "模型输出中出现虚假成功承诺，就地截断：trace=%s kind=%s",
                                        trace_id,
                                        kind,
                                    )
                                    continue
                                # 回看窗口：末尾 LOOKBEHIND 个字先留着不下发，
                                # 等下一段文本来了再判断它是不是承诺的开头
                                flush_upto = len(turn_buffer) - LOOKBEHIND
                                if flush_upto > pending_start:
                                    chunk = turn_buffer[pending_start:flush_upto]
                                    pending_start = flush_upto
                                    result.text += chunk
                                    emitted = True
                                    emitted_this_turn = True
                                    yield TextResponseEvent(
                                        trace_id=trace_id, metadata={"delta": chunk}
                                    )
                            # 本轮结束：把回看窗口里剩下的字补发出去
                            if not suppressed and pending_start < len(turn_buffer):
                                chunk = turn_buffer[pending_start:]
                                result.text += chunk
                                emitted = True
                                emitted_this_turn = True
                                yield TextResponseEvent(
                                    trace_id=trace_id, metadata={"delta": chunk}
                                )
                            candidate = holder.get("turn") or _empty_turn()
                            # 检测用**完整**文本（含被截断的承诺）；
                            # 下发给用户 / 写进历史的是**裁剪后**的文本。
                            # 两者分开，才不会因为"先把谎话删掉"而检测不到它。
                            detected_text = candidate.text
                            if truncated_claim is not None:
                                candidate.text = _safe_prefix(turn_buffer, detected_text)
                        except Exception as exc:
                            attempt_error = exc
                            candidate = _empty_turn()
                            detected_text = ""
                            # 流中断：把已经收到、但还压在回看窗口里的内容补发出去，
                            # 否则用户会凭空少看到一截
                            if not suppressed and pending_start < len(turn_buffer):
                                chunk = turn_buffer[pending_start:]
                                pending_start = len(turn_buffer)
                                result.text += chunk
                                emitted = True
                                emitted_this_turn = True
                                yield TextResponseEvent(
                                    trace_id=trace_id, metadata={"delta": chunk}
                                )

                        finding = inspect_turn(
                            _TurnView(detected_text, candidate.tool_calls),
                            known_tool_names,
                            tool_executed=(
                                booking_executed_success
                                or cancel_executed_success
                                or prior_booking
                            ),
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
                                # 已经在流式阶段截断过了，这里只需安排纠正重试，
                                # 让模型用**结构化 tool_call** 重新发起
                                if corrective_used == 0 and not corrective_pending:
                                    corrective_pending = True
                                    corrective_kind = "tool_call"
                                    llm_span.set_attribute("corrective_retry_scheduled", True)
                            claim = truncated_claim or claim_kind(detected_text)
                            if finding.false_confirmation or (
                                claim == "query" and not query_executed_success
                            ):
                                self.metrics.inc("hallucination_suspected")
                                llm_span.set_attribute("hallucination_suspected", True)
                                llm_span.set_attribute("claim_kind", claim)
                                if corrective_used == 0 and not prior_booking:
                                    # 先不追加免责提示——注入纠正提示重试一轮，
                                    # 逼迫模型真正调用工具（比事后提示更能纠错）
                                    corrective_pending = True
                                    corrective_kind = claim
                                    llm_span.set_attribute(
                                        "corrective_retry_scheduled", True
                                    )
                                    logger.warning(
                                        "检测到未调工具的预约/取消承诺，准备纠正重试：trace=%s kind=%s",
                                        trace_id,
                                        claim,
                                    )
                                else:
                                    # 纠正重试后仍然编造成功：**丢弃**这段文本，
                                    # 换成陈述事实的兜底文案 —— 绝不能让"预约成功"显示给用户
                                    fallback = fallback_for(claim, last_tool_failure)
                                    prefix = (result.text or "").rstrip()
                                    if prefix:
                                        prefix += "\n\n"
                                    result.text = prefix + fallback
                                    yield TextResponseEvent(
                                        trace_id=trace_id,
                                        metadata={"delta": fallback},
                                    )
                                    self.metrics.inc("hallucination_fallbacks")
                                    llm_span.set_attribute("hallucination_fallback", True)
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
                    # 模型说"我这就调用预约挂号工具"却没真调 —— 逼它真的调一次。
                    # （2026-11-10 实测：用户看到"我将调用预约挂号工具"，
                    #   然后什么都没发生，工具一次都没执行。）
                    if (
                        not corrective_pending
                        and corrective_used == 0
                        and operation_request
                        and detect_narrated_tool_call(response.text)
                    ):
                        corrective_pending = True
                        corrective_kind = "action"
                        self.metrics.inc("narrated_tool_call_retries")
                        span.set_attribute("narrated_tool_call", True)
                        logger.warning(
                            "模型只说要调工具却没调，准备纠正重试：trace=%s",
                            trace_id,
                        )

                    if corrective_pending:
                        corrective_pending = False
                        corrective_used += 1
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
                            if call.name == "预约挂号":
                                booking_executed_success = True
                            elif call.name == "取消预约挂号":
                                cancel_executed_success = True
                            elif call.name in ("查询是否有号源", "查询科室专家"):
                                query_executed_success = True
                        else:
                            tool_failures[call.name] = tool_failures.get(call.name, 0) + 1
                            # 记住**失败原因**：模型编造"预约成功"时，兜底文案要
                            # 反映工具的真实反馈（"已有预约，请勿重复"），
                            # 而不是笼统地说"没有生成记录" —— 后者在用户明明
                            # 已经约过号时是误导。
                            last_tool_failure = {
                                "tool": call.name,
                                "error_type": tool_result.error_type,
                                "content": tool_result.content,
                            }

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
                    outcome_meta: Dict[str, Any] = {}
                    if booking_executed_success:
                        outcome_meta["booking_executed"] = True
                    if cancel_executed_success:
                        outcome_meta["cancel_executed"] = True
                    new_messages.append(
                        Message(
                            role=Role.ASSISTANT,
                            content=result.text,
                            tool_calls=result.tool_calls,
                            metadata=outcome_meta,
                        )
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
