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

from xiaozhi.core.event import AgentEndEvent, AgentStartEvent, Event
from xiaozhi.core.message import Role, ToolCall, Message
from xiaozhi.memory.context import ContextAssembler
from xiaozhi.memory.store import ConversationStore
from xiaozhi.ops.metrics import MetricsCollector
from xiaozhi.reliability.resilience import CircuitBreaker, RetryConfig
from xiaozhi.reliability.tracing import get_trace_id, trace_span
from xiaozhi.runtime.engine_phases import EnginePhases
from xiaozhi.runtime.persister import ConversationPersister
from xiaozhi.runtime.stream_filter import trim_to_sentence as _trim_to_sentence
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

    async def run(
        self,
        memory_id: str,
        user_input: str,
        user_id: str = "anonymous",
    ) -> AsyncIterator[Event]:
        """跑一轮对话，产出事件流。

        纯编排：只回答"按什么顺序做"，每步细节见 EnginePhases 对应方法——
            1. _load_history          读会话历史
            2. _prepare_messages      检索知识 + 组装上下文
            3. _new_turn_state        初始化请求级跨轮状态
            4. _run_loop              主循环（预算/熔断守门 → 调模型 → 降级或收尾或执行工具）
            5. _persist_and_extract   落库 + 画像抽取钩子
            6. _finalize              指标收口 + AgentEnd
        """
        trace_id = get_trace_id()
        result = AgentEngineResultHolder()
        started = time.monotonic()
        self.metrics.inc("requests_total")

        with trace_span("http.request", trace_id=trace_id) as span:
            span.set_attribute("memory_id", memory_id)
            span.set_attribute("message_preview", user_input[:120])

            yield AgentStartEvent(trace_id=trace_id, metadata={"input_length": len(user_input)})

            # 1. 读历史
            history = await self._load_history(memory_id, trace_id)

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
