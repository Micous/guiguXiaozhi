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
from typing import Any, AsyncIterator, Callable, Dict, Iterable, List, Optional

from xiaozhi.core.event import AgentEndEvent, AgentStartEvent, Event
from xiaozhi.core.message import Role, ToolCall, Message
from xiaozhi.memory.context import ContextAssembler
from xiaozhi.memory.store import ConversationStore
from xiaozhi.ops.metrics import MetricsCollector
from xiaozhi.reliability.resilience import CircuitBreaker, RetryConfig
from xiaozhi.reliability.tracing import (
    get_trace_id,
    peek_trace_id,
    restore_trace_id,
    trace_span,
)
from xiaozhi.runtime.appointment_guard import (
    assess_department_guard,
    extract_latest_id_card,
)
from xiaozhi.runtime.engine_phases import EnginePhases
from xiaozhi.runtime.intent import is_greeting
from xiaozhi.runtime.loop_controller import LoopController, LoopPorts
from xiaozhi.runtime.model_runner import ModelRunner
from xiaozhi.runtime.persister import ConversationPersister
from xiaozhi.runtime.run_context import ExecutionBudget, RunContext
from xiaozhi.runtime.tool_coordinator import ToolCoordinator
from xiaozhi.runtime.stream_filter import trim_to_sentence as _trim_to_sentence
from xiaozhi.security.medical_safety import assess_medical_safety
from xiaozhi.security.secure_executor import SecureToolExecutor
from xiaozhi.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

# 句子裁剪已迁至 runtime/stream_filter.py（E3，`_trim_to_sentence` 为兼容别名）；
# 执行阶段与编排子步骤均已迁至 runtime/engine_phases.py（E6 + run 拆分），
# engine.py 只保留：组件装配（__init__）、纯编排（run）、消息格式转换、结果收集器。


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
        profile_injector=None,
        profile_service=None,
        task_tracker=None,
        department_provider: Optional[Callable[[], Iterable[str]]] = None,
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
        # P2 患者画像（灰度：profile_enabled=false 时两者均为 None，行为与一期完全一致）
        self.profile_injector = profile_injector
        self.profile_service = profile_service
        # Phase 5 任务生命周期持久化（P1-02，灰度：task_tracking_enabled=false
        # 时为 None，行为与 Phase 4 完全一致）。追踪失败绝不打断对话 ——
        # 追踪是可观测性，不是业务依赖。
        self.task_tracker = task_tracker
        # 院方权威科室名单提供者（默认 None = 守卫不启用，既有行为零变更）。
        # 真机装配时注入 ``doctor_service.list_departments``，名单仍只有 HIS 一份。
        # 每次请求都现取，HIS 主档刷新后无需重启。
        self.department_provider = department_provider
        # B2 第一批：模型回合从 EnginePhases mixin 抽到显式协作者。
        # 判定型依赖以 callable Port 注入（每次调用现取 contextvar / 配置）；
        # chat_model / retry_config / metrics 仍由委托壳按调用传入，
        # 不在构造期快照，保住构造后替换依赖的既有装配与单测方式。
        self._model_runner = ModelRunner(
            has_pending_approval_token=self._has_pending_approval_token,
            output_guard_enabled=self._output_guard_enabled,
        )
        # B2 第二批：主循环迁到 LoopController。所有外部能力以显式 Port 注入；
        # max_turns / circuit_breaker 等请求级运行时依赖由委托壳按调用传入，
        # 协作者实例本身不保存任何跨请求状态（并发请求共用同一实例）。
        self._loop_controller = LoopController(
            LoopPorts(
                time_budget_exceeded=self._time_budget_exceeded,
                degrade_on_time_budget=self._degrade_on_time_budget,
                circuit_is_open=self._circuit_is_open,
                degrade_on_circuit_open=self._degrade_on_circuit_open,
                degrade_on_model_error=self._degrade_on_model_error,
                chat_turn=self._chat_turn,
                execute_approved_operation=self._execute_approved_operation,
                retry_with_approval_hint=self._retry_with_approval_hint,
                schedule_corrective_retry=self._schedule_corrective_retry,
                execute_tools=self._execute_tools,
            )
        )

        # B2 第三批：工具执行迁到 ToolCoordinator。两个消息格式转换是纯函数，
        # 以显式 Port 注入；其余运行时依赖由委托壳按调用传入。
        self._tool_coordinator = ToolCoordinator(
            assistant_to_ollama=self._assistant_to_ollama,
            tool_to_ollama=self._tool_to_ollama,
        )

    async def run(
        self,
        memory_id: str,
        user_input: str,
        user_id: str = "anonymous",
        *,
        task_idempotency_key: Optional[str] = None,
    ) -> AsyncIterator[Event]:
        """跑一轮对话，产出事件流。

        纯编排：只回答"按什么顺序做"，每步细节见 EnginePhases 对应方法——
            1. _load_history          读会话历史
            2. _prepare_messages      检索知识 + 组装上下文
            3. _new_turn_state        初始化请求级跨轮状态
            4. _run_loop              主循环（预算/熔断守门 → 调模型 → 降级或收尾或执行工具）
            5. _persist_and_extract   落库 + 画像抽取钩子
            6. _finalize              指标收口 + AgentEnd

        ``task_idempotency_key``（Phase 5）：客户端显式提供的幂等键。
        **绝不用请求文本派生** —— 同一句话重发（例如审批后重放）在业务上
        是两次独立请求，文本哈希会把它们错误并成一个任务。

        B1 生命周期壳：入口只创建一次 ``RunContext`` / ``deadline``，
        ``try / except / finally`` 统一处理取消、异常与 ``GeneratorExit``。
        **finally 里绝不 yield** —— 客户端断开时 async generator 会被
        ``aclose()`` 抛 ``GeneratorExit``，此时 yield 会抛 ``RuntimeError``。
        异常仍原样抛给上层（``routes.generate()`` 的兜底语义不变），本批次
        不吞异常、不改变对外事件序列；"异常路径也保证 AgentEndEvent"随 B2
        抽取 ``RunFinalizer`` 一并收口。
        """
        # 先记录「请求进入前」的上下文，再让 get_trace_id() 可能新建 ID。
        # 顺序反了会把第一次生成的 trace_id 残留给下一个请求。
        entry_trace_id = peek_trace_id()
        trace_id = get_trace_id()
        result = AgentEngineResultHolder()
        run_ctx = RunContext.create(
            trace_id=trace_id,
            session_id=memory_id,
            subject_id=user_id,
            timeout_seconds=self.request_timeout,
        )
        budget = ExecutionBudget(run_ctx)
        # 全 Run 唯一时间基准；所有阶段读同一份 deadline 的剩余预算。
        started = run_ctx.started_monotonic
        self.metrics.inc("requests_total")

        outcome = "unknown"
        try:
            async for event in self._run_body(
                memory_id=memory_id,
                user_input=user_input,
                user_id=user_id,
                task_idempotency_key=task_idempotency_key,
                trace_id=trace_id,
                result=result,
                run_ctx=run_ctx,
                budget=budget,
                started=started,
            ):
                yield event
            outcome = "completed"
        except asyncio.CancelledError:
            # 取消 ≠ 远端写已取消：写工具可能已经提交，业务侧必须靠
            # STARTED / UNKNOWN 对账收敛，这里只记录事实并原样传播。
            run_ctx.cancellation.cancel("client_cancelled")
            self.metrics.inc("run_cancelled_total")
            logger.warning(
                "请求被取消（不代表远端写操作已取消）：run=%s trace=%s",
                run_ctx.run_id,
                trace_id,
            )
            outcome = "cancelled"
            raise
        except Exception as exc:
            result.error = f"{type(exc).__name__}: {exc}"
            result.degraded = True
            self.metrics.inc("run_unhandled_error_total")
            logger.exception(
                "请求未处理异常：run=%s trace=%s", run_ctx.run_id, trace_id
            )
            outcome = "error"
            raise
        finally:
            # GeneratorExit（客户端断开 / aclose）不会进上面两个 except，
            # outcome 仍是 unknown —— 在这里补上取消事实。
            if outcome == "unknown":
                run_ctx.cancellation.cancel("generator_closed")
                self.metrics.inc("run_generator_closed_total")
            restore_trace_id(entry_trace_id)
            self.metrics.inc("run_finished_total")
            logger.debug("run 收尾：outcome=%s budget=%s", outcome, budget.snapshot())

    async def _run_body(
        self,
        *,
        memory_id: str,
        user_input: str,
        user_id: str,
        task_idempotency_key: Optional[str],
        trace_id: str,
        result: "AgentEngineResultHolder",
        run_ctx: RunContext,
        budget: ExecutionBudget,
        started: float,
    ) -> AsyncIterator[Event]:
        """原有编排顺序，逐字保留；B1 只把生命周期壳抽到 ``run()``。"""
        with trace_span("http.request", trace_id=trace_id) as span:
            span.set_attribute("run_id", run_ctx.run_id)
            span.set_attribute("request_id", run_ctx.request_id)
            span.set_attribute("memory_id", memory_id)
            span.set_attribute("message_preview", user_input[:120])

            yield AgentStartEvent(trace_id=trace_id, metadata={"input_length": len(user_input)})

            # 1. 读历史
            history = await self._load_history(memory_id, trace_id)

            # 1.5 Phase 7：医疗安全确定性门控。
            #     急症（胸痛/呼吸困难/大出血/意识障碍…）与越界请求（诊断/处方/剂量）
            #     **不进检索、不进模型**，直接下发固定话术：
            #     7B 对"我爸胸痛晕倒了"会答"建议尽快到医院就诊"——语气对、处置错，
            #     真正的答案是立刻拨 120 且不要搬动/喂水。这种错误不能交给概率。
            safety = assess_medical_safety(user_input)
            if safety.blocked:
                async for _ev in self._short_circuit_medical_safety(
                    safety,
                    memory_id=memory_id,
                    history=history,
                    user_input=user_input,
                    result=result,
                    trace_id=trace_id,
                    started=started,
                ):
                    yield _ev
                return

            # 1.6 纯问候确定性快路径（首句秒回）。
            #     医疗安全判定优先：含自伤/急症/越界的消息即使在寒暄里夹带，
            #     也已经在上一步被收走了，不会落到这里。
            #     限定 is_greeting() 的全匹配短句（≤12 字），"你好，我胸痛"
            #     之类的真实诉求照常进模型与工具链路。
            if is_greeting(user_input):
                async for _ev in self._short_circuit_greeting(
                    memory_id=memory_id,
                    history=history,
                    user_input=user_input,
                    result=result,
                    trace_id=trace_id,
                    started=started,
                ):
                    yield _ev
                return

            # 1.7 预约科室确定性守卫：本院不存在的科室在填单阶段就收尾。
            #     顺序必须在医疗安全、问候之后，检索/模型/工具/审批之前：
            #     真机事故里 7B 拿到 ToolResult「本院没有【内科】」仍继续
            #     要时段、承诺预约、弹确认窗，43 秒后才把拒绝转述出来。
            #     provider 为空或取数失败一律放行，绝不因守卫把请求打死。
            if self.department_provider is not None:
                try:
                    known = list(self.department_provider() or [])
                except Exception as exc:  # 名单不可用 ≠ 本院没有科室
                    logger.warning(
                        "院方科室名单读取失败，本轮跳过科室守卫：trace=%s err=%s: %s",
                        trace_id,
                        type(exc).__name__,
                        exc,
                    )
                    known = []
                dept_verdict = assess_department_guard(user_input, known)
                if dept_verdict is not None:
                    async for _ev in self._short_circuit_department_guard(
                        dept_verdict,
                        memory_id=memory_id,
                        history=history,
                        user_input=user_input,
                        result=result,
                        trace_id=trace_id,
                        started=started,
                    ):
                        yield _ev
                    return

            # 2. 检索知识 + 组装上下文
            prep: Dict[str, Any] = {}
            async for _ev in self._prepare_messages(
                user_input=user_input,
                history=history,
                trace_id=trace_id,
                outcome=prep,
            ):
                yield _ev

            # 3. 请求级跨轮状态（工具跟踪/纠正重试/文本账本的单一事实来源）
            state = self._new_turn_state(history, prep["operation_request"])
            # Phase 4：写操作模式（Response Gate 的完成态确定性门控）
            state.write_operation = bool(prep.get("write_operation"))
            # PII 真值：落库/展示用掩码，但写工具执行必须用真值
            # （7B 会把历史里看到的掩码身份证直接当参数重发，见 appointment_guard）
            state.known_id_card = extract_latest_id_card(history, user_input)

            # 3.5 Phase 5：任务生命周期持久化（灰度关闭时 tracker 为 None，
            #     行为与 Phase 4 完全一致）。追踪失败不打断对话。
            if self.task_tracker is not None:
                task_id, execution_id = self.task_tracker.begin(
                    subject_id=user_id,
                    session_id=memory_id,
                    idempotency_key=task_idempotency_key,
                )
                state.task_id = task_id
                state.execution_id = execution_id

            # 4. 主循环（最终轮数写入 loop_outcome["turns"]）
            loop_outcome: Dict[str, Any] = {}
            async for _ev in self._run_loop(
                messages=prep["messages"],
                tools=prep["tools"],
                known_tool_names=prep["known_tool_names"],
                state=state,
                user_input=user_input,
                user_id=user_id,
                trace_id=trace_id,
                request_span=span,
                started=started,
                budget=budget,
                result=result,
                outcome=loop_outcome,
            ):
                yield _ev

            # 4.5 写操作 Response Gate 收尾（确定性兜底，在落库前执行——
            #     被拦下的承诺文本绝不能进历史）
            async for _ev in self._finalize_response_gate(state, result, trace_id):
                yield _ev

            # 5. 落库 + 画像抽取
            async for _ev in self._persist_and_extract(
                memory_id=memory_id,
                history=history,
                user_input=user_input,
                state=state,
                result=result,
                trace_id=trace_id,
            ):
                yield _ev

            span.set_attribute("turns", loop_outcome.get("turns", 0))
            span.set_attribute("degraded", result.degraded)

        # 6. 指标收口 + 结束事件
        async for _ev in self._finalize(
            result=result,
            state=state,
            turns=loop_outcome.get("turns", 0),
            trace_id=trace_id,
            started=started,
        ):
            yield _ev

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
