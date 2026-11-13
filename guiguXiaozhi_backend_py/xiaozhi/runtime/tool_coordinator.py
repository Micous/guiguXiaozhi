"""工具执行协作者 —— B2 第三批从 ``EnginePhases._execute_tools`` 抽出。

职责边界（对齐《主体Agent生产化架构最终整改方案》§3.1 / §4 P1-1.1）：
    * **只负责**「一轮里全部工具调用的执行、审批门控、结果回填 ``messages``、
      ``TOOL_EXECUTE`` / ``TOOL_RESULT`` / ``APPROVAL_REQUIRED`` 事件」；
    * **不负责**主循环轮次、模型调用、落库、``AgentEndEvent`` 与任务终态化。

跨文件依赖的处理（此前 ``_execute_tools`` 是 mixin 里唯一通过 ``self`` 调用
``engine.py`` 静态方法的地方）：``_assistant_to_ollama`` / ``_tool_to_ollama``
以同名 callable Port 在构造时注入，调用点逐字不变。

与 ModelRunner / LoopController 一致：协作者实例被多个请求共享，因此
``metrics`` / ``max_tool_calls`` / ``tool_failure_threshold`` / ``tool_registry`` /
``task_tracker`` / ``secure_executor`` 全部按每次调用传入，不落到 self 上；
``messages`` / ``state`` / ``result`` 本来就是调用参数。

方法体自 mixin 逐字平移，仅 6 处 ``self.xxx`` → 局部参数替换。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, AsyncIterator, Callable, Dict, List

if TYPE_CHECKING:  # 仅供类型标注，避免与 engine.py 循环导入
    from xiaozhi.runtime.engine import AgentEngineResultHolder

from xiaozhi.core.event import (
    ApprovalRequiredEvent,
    Event,
    TextResponseEvent,
    ToolExecuteEvent,
    ToolResultEvent,
)
from xiaozhi.core.message import Message
from xiaozhi.core.tool import ToolResult
from xiaozhi.ops.bounded_io import run_sync_io
from xiaozhi.reliability.tracing import trace_span
from xiaozhi.runtime.appointment_guard import restore_masked_id_cards
from xiaozhi.runtime.state import TurnState
from xiaozhi.runtime.task_tracker import ToolStartStatus
from xiaozhi.runtime.write_tools import WRITE_TOOL_NAMES
from xiaozhi.security.secure_executor import ToolCall as SecureToolCall

logger = logging.getLogger(__name__)


class ToolCoordinator:
    """一轮工具调用的执行者（无跨请求状态，可安全复用）。"""

    def __init__(
        self,
        *,
        assistant_to_ollama: Callable[[Message], Dict[str, Any]],
        tool_to_ollama: Callable[[Message], Dict[str, Any]],
    ) -> None:
        # 消息格式转换是纯函数，作为显式 Port 注入（定义在 engine.py）。
        self._assistant_to_ollama = assistant_to_ollama
        self._tool_to_ollama = tool_to_ollama

    async def execute_tools(
        self,
        *,
        response,
        messages: List[Dict[str, Any]],
        state: TurnState,
        user_id: str,
        trace_id: str,
        result: "AgentEngineResultHolder",
        metrics,
        max_tool_calls: int,
        tool_failure_threshold: int,
        tool_registry,
        task_tracker,
        secure_executor,
    ) -> AsyncIterator[Event]:
        """执行本轮全部工具调用，结果回填 messages（事件实时下发）。"""
        # ---- 执行工具 ----
        assistant_message = Message.assistant(response.text, tool_calls=response.tool_calls)
        state.set_narration(response.text)
        messages.append(self._assistant_to_ollama(assistant_message))

        for call in response.tool_calls:
            # ---- PII 真值还原（必须在审批门控/参数校验之前）----
            # 落库与确认窗展示用掩码是安全契约（PII P0-06），但 7B 下一轮会把
            # 历史里看到的 `610121********2831` 直接当参数重发。这里还原成真值，
            # 使「审批哈希 = 实际执行参数 = 真值」一致，用户看到的仍是掩码。
            if state.known_id_card and call.arguments:
                restored, restored_changed = restore_masked_id_cards(
                    call.arguments, state.known_id_card
                )
                if restored_changed:
                    # 原地更新：同一 ToolCall 对象已被 append 进本轮 messages，
                    # 后面落库也用这份参数 —— 只替换副本会让三处口径不一致。
                    call.arguments.clear()
                    call.arguments.update(restored)
                    metrics.inc("id_card_restored_total")
                    logger.info(
                        "工具参数中的掩码身份证已还原为真值：tool=%s trace=%s",
                        call.name,
                        trace_id,
                    )

            if state.tool_call_count >= max_tool_calls:
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
                tool = tool_registry.get(call.name)

                # ---- B3-2：STARTED 三态 + 写操作强保证（方案 §4 规则 8）----
                # DISABLED（灰度关闭）→ 写/只读都照常放行，事件序列不变（D1=A）；
                # FAILED + 写工具 → 拒绝进入 Adapter，不产生副作用（核心行为变更）；
                # FAILED + 只读 → 放行但必须留痕（D2）。
                start = None
                if task_tracker is not None:
                    start = await run_sync_io(
                        task_tracker.try_tool_started,
                        call.id,
                        call.name,
                        call.arguments,
                    )
                tool_exec_id = start.tool_execution_id if start else None
                start_failed = bool(start and start.status is ToolStartStatus.FAILED)
                if start_failed:
                    metrics.inc("task_started_failed_total")

                if start_failed and call.name in WRITE_TOOL_NAMES:
                    metrics.inc("task_started_failed_write_total")
                    tool_span.set_error("task_start_failed")
                    logger.warning(
                        "写工具 STARTED 落库失败，已拒绝执行：trace=%s tool=%s call=%s",
                        trace_id,
                        call.name,
                        call.id,
                    )
                    tool_result = ToolResult.fail(
                        f"任务追踪暂不可用，已阻止写操作【{call.name}】执行，请稍后重试。",
                        error_type="TaskStartUnavailable",
                    )
                elif start_failed:
                    logger.warning(
                        "只读工具 STARTED 落库失败，按 D2 决策放行："
                        "trace=%s tool=%s call=%s effect=read",
                        trace_id,
                        call.name,
                        call.id,
                    )

                if start_failed and call.name in WRITE_TOOL_NAMES:
                    pass  # 拒绝结果已在上面构造，直接跳到入账段
                elif tool is None:
                    tool_span.set_error("工具未注册")
                    tool_result = ToolResult.fail(
                        f"工具【{call.name}】不存在。", error_type="ToolNotFound"
                    )
                elif (
                    state.failure_count(call.name)
                    >= tool_failure_threshold
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
                    tool_result = await secure_executor.execute(secure_call, tool)

                if tool_result.success:
                    state.record_tool_success(call.name)
                else:
                    state.record_tool_failure(call.name, tool_result)
                # ---- Phase 4：写工具的结构化结果单独入账 ----
                # 完成态表述以业务服务的 ToolResult 为准（不让模型总结成败）；
                # UNKNOWN（authoritative_status）在请求收尾时换固定文案。
                state.record_write_result(call.name, tool_result)

                # ---- Phase 5：工具执行后落终态（写工具成功 → 副作用已提交）----
                # 被拒绝的写操作没有任何 STARTED 记录可收尾，直接跳过。
                if task_tracker is not None and not (
                    start_failed and call.name in WRITE_TOOL_NAMES
                ):
                    await run_sync_io(
                        task_tracker.tool_finished,
                        tool_exec_id,
                        call.name,
                        success=tool_result.success,
                    )

                metrics.record_tool(call.name, tool_result.success)
                metrics.observe(
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
