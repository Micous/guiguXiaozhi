"""主循环编排协作者 —— B2 第二批从 ``EnginePhases._run_loop`` 抽出。

职责边界（对齐《主体Agent生产化架构最终整改方案》§3.1 / §4 P1-1.1）：
    * **只负责**「轮次推进、预算/熔断守门的顺序、``TURN_START`` / ``TURN_END``、
      ``held_deltas`` 补发时机、审批短路与纠正重试、``outcome["turns"]``」；
    * **不负责**模型调用、工具执行、落库、``AgentEndEvent`` 与任务终态化。

所有能力以 ``LoopPorts`` 显式注入（§3.2「不通过继承 mixin 访问其他模块的私有
属性」）。**协作者实例是被多个请求共享的，因此绝不把请求级数据放到 self 上**：
``max_turns`` / ``circuit_breaker`` 等运行时依赖按每次 ``run_loop()`` 调用传入，
``messages`` / ``state`` / ``result`` / ``outcome`` 本来就是调用参数。

方法体自 mixin 逐字平移，仅 12 处标识符替换（``self._xxx`` → ``self._ports.xxx``、
``self.max_turns`` / ``self.circuit_breaker`` → 局部参数），可用等价脚本核对。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, AsyncIterator, Callable, Dict, List

if TYPE_CHECKING:  # 仅供类型标注，避免与 engine.py 循环导入
    from xiaozhi.runtime.engine import AgentEngineResultHolder

from xiaozhi.core.event import Event, TextResponseEvent, TurnEndEvent, TurnStartEvent
from xiaozhi.runtime.engine_helpers import _empty_turn
from xiaozhi.runtime.state import TurnState


@dataclass(frozen=True)
class LoopPorts:
    """主循环需要的全部外部能力（过渡期由 AgentEngine 绑定 mixin 方法）。"""

    time_budget_exceeded: Callable[..., bool]
    degrade_on_time_budget: Callable[..., AsyncIterator[Event]]
    circuit_is_open: Callable[[], bool]
    degrade_on_circuit_open: Callable[..., AsyncIterator[Event]]
    degrade_on_model_error: Callable[..., AsyncIterator[Event]]
    chat_turn: Callable[..., AsyncIterator[Event]]
    execute_approved_operation: Callable[..., AsyncIterator[Event]]
    retry_with_approval_hint: Callable[..., bool]
    schedule_corrective_retry: Callable[..., bool]
    execute_tools: Callable[..., AsyncIterator[Event]]


class LoopController:
    """一次 Run 的主循环执行者（无跨请求状态，可安全复用）。"""

    def __init__(self, ports: LoopPorts) -> None:
        self._ports = ports

    async def run_loop(
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
        budget=None,
        max_turns: int,
        circuit_breaker,
    ) -> AsyncIterator[Event]:
        """主循环：预算/熔断守门 → 调模型 → 出错降级 / 无工具收尾 / 执行工具。

        结束时把最终轮数写入 outcome["turns"]（run 收口与 AgentEnd 要用）。
        """
        turn = 0
        while turn < max_turns:
            # ---- 守门一：请求级时间预算 ----
            if self._ports.time_budget_exceeded(started, budget):
                async for _ev in self._ports.degrade_on_time_budget(user_input, trace_id, result):
                    yield _ev
                break

            # ---- 守门二：模型熔断器打开时直接本地降级 ----
            if self._ports.circuit_is_open():
                async for _ev in self._ports.degrade_on_circuit_open(user_input, trace_id, result):
                    yield _ev
                break

            yield TurnStartEvent(trace_id=trace_id, metadata={"turn_number": turn})

            # ---- 调模型（流式 + 质量拦截器链，见 _chat_turn）----
            turn_outcome: Dict[str, Any] = {}
            async for _ev in self._ports.chat_turn(
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
            held_deltas: List[str] = turn_outcome.get("held_deltas") or []

            def _flush_held() -> List[Event]:
                return [
                    TextResponseEvent(trace_id=trace_id, metadata={"delta": delta})
                    for delta in held_deltas
                ]

            # ---- 模型调用失败 → 降级并结束 ----
            if call_error is not None:
                for _ev in _flush_held():
                    yield _ev
                async for _ev in self._ports.degrade_on_model_error(
                    call_error, emitted_this_turn, user_input, trace_id, result
                ):
                    yield _ev
                break

            if circuit_breaker is not None:
                circuit_breaker.record_success()

            if response is None:
                response = _empty_turn()

            # ---- 没有工具调用 → 结束（或纠正重试一轮） ----
            if not response.tool_calls:
                # ---- 带审批 token 的请求：服务端按审批记录**确定性执行** ----
                # 顺序在"强制重试模型"之前：用户已经点过确认，模型这一轮说什么
                # 都不该决定这件事办不办。2026-11-12 真机，7B 两轮"只说不做"
                # （先"请在确认窗口核对"，再"正在为您提交预约"），token 白拿、
                # 预约没发生 —— 求模型配合的那条路已经证明不可靠。
                # 无 token / token 已消费时此方法静默返回，行为与之前完全一致。
                async for _ev in self._ports.execute_approved_operation(
                    user_id=user_id,
                    trace_id=trace_id,
                    state=state,
                    result=result,
                ):
                    yield _ev
                if state.approval_auto_executed:
                    turn += 1
                    yield TurnEndEvent(
                        trace_id=trace_id,
                        metadata={
                            "turn_number": turn,
                            "approval_auto_executed": True,
                        },
                    )
                    break

                # ---- 兜底未生效（无 token 等）→ 仍按原链路："带有效审批 token 的请求" ----
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
                # 兜底没执行成 → 模型这一轮的话补发给用户（与旧行为一致）
                for _ev in _flush_held():
                    yield _ev
                if self._ports.retry_with_approval_hint(
                    messages, tools, state, response, trace_id
                ):
                    turn += 1
                    yield TurnEndEvent(
                        trace_id=trace_id,
                        metadata={"turn_number": turn, "corrective_retry": True},
                    )
                    continue
                if self._ports.schedule_corrective_retry(response, messages, state):
                    turn += 1
                    yield TurnEndEvent(
                        trace_id=trace_id,
                        metadata={"turn_number": turn, "corrective_retry": True},
                    )
                    continue
                break

            # ---- 执行工具 + 结果回填（见 _execute_tools）----
            # 真调了工具 → 叙述段照常下发（被扣住的文本这时才放行）
            for _ev in _flush_held():
                yield _ev
            async for _ev in self._ports.execute_tools(
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
