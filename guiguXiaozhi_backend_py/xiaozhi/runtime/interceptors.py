"""质量防线拦截器链 —— engine 重构 E4 的产物。

engine 不再直接调用具体检测（`_NARRATED_TOOL_CALL` 正则、`claim_kind`、
`inspect_turn` 的各字段判断），而是把"一个模型回合的检测 → 处置决定"
交给有序的拦截器链。以后再出换皮句式，只改 `models/quality.py` 的正则
（或对应拦截器），engine 一行不动。

四个组件（从 engine 内嵌逻辑**逐字平移**，不改变任何行为）：
  1. TextualToolCallInterceptor   纯文本 JSON 工具调用 → schedule("tool_call")
  2. FalseConfirmationInterceptor 虚假确认 + query 门控 → schedule / 兜底替换
  3. NarratedToolCallInterceptor  叙述承诺（仅操作类请求）→ schedule("action")
  4. CorrectiveRetryScheduler     消费前三者的判定，合并成单一处置，
                                  维护 corrective_used <= 1 不变量

行为基线 = E1 特征测试钉住的旧内嵌实现，关键语义：
  * textual 用 schedule 语义（pending/used 任一命中即跳过）；
  * 幻觉分支用 **override** 语义（pending 已置位时仍覆盖 kind），
    且 fallback 与 retry 的分流看 `corrective_used == 0 and not prior_booking`；
  * narrated 检测发生在本轮调度**之后**：本轮 textual/幻觉已置 pending 时
    narrated 不再触发；检测用**裁剪后**文本（response.text），非全文。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from xiaozhi.models.quality import (
    claim_kind,
    detect_narrated_tool_call,
    fallback_for,
    inspect_turn,
)
from xiaozhi.runtime.state import TurnState


@dataclass
class TurnFacts:
    """一个模型回合的检测输入（只读，由 engine 组装）。

    detected_text  检测用**完整**文本（含被截断的承诺）
    response_text  裁剪后的候选文本（narrated 检测用这个，与旧行为一致）
    tool_calls     结构化工具调用（空列表/None = 没有）
    finding        inspect_turn 的判定结果
    truncated_claim StreamFilter 流内截断的承诺类型（未截断为 None）
    """

    detected_text: str
    response_text: str
    tool_calls: Any
    finding: Any
    truncated_claim: Optional[str] = None

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


@dataclass
class InterceptorVerdict:
    """单个拦截器的判定（给 CorrectiveRetryScheduler 消费）。"""

    # "schedule"：排队纠正（pending/used 命中则跳过）
    # "override"：覆盖纠正类型（只看 used 上限）
    # "fallback"：本轮作废，换兜底文案
    action: str = "pass"
    corrective_kind: Optional[str] = None
    fallback_claim: Optional[str] = None
    hallucination_suspected: bool = False
    # 兜底文案（fallback 时使用；None 则由引擎用 fallback_for 生成）
    fallback_text: Optional[str] = None


class TextualToolCallInterceptor:
    """「把工具调用写成纯文本 JSON」检测（检测本身在流内已截断）。"""

    def evaluate(self, facts: TurnFacts, state: TurnState) -> InterceptorVerdict:
        if facts.finding.textual_tool_call:
            return InterceptorVerdict(action="schedule", corrective_kind="tool_call")
        return InterceptorVerdict()


class FalseConfirmationInterceptor:
    """虚假确认（未调工具却声称预约/取消/查询成功）检测 + query 门控。"""

    def evaluate(self, facts: TurnFacts, state: TurnState) -> InterceptorVerdict:
        claim = facts.truncated_claim or claim_kind(facts.detected_text)
        triggered = facts.finding.false_confirmation or (
            claim == "query" and not state.query_executed_success
        )
        if not triggered:
            return InterceptorVerdict()
        if state.corrective_used == 0 and not state.prior_booking:
            # 先不追加免责提示——注入纠正提示重试一轮（pending 已置位时覆盖 kind）
            return InterceptorVerdict(
                action="override",
                corrective_kind=claim,
                hallucination_suspected=True,
            )
        # 纠正重试后仍然编造成功：本轮作废，换陈述事实的兜底文案
        return InterceptorVerdict(
            action="fallback",
            fallback_claim=claim,
            hallucination_suspected=True,
            fallback_text=fallback_for(claim, state.last_tool_failure),
        )


class NarratedToolCallInterceptor:
    """叙述承诺（"我将为您预约…"零工具调用）检测 —— 仅操作类请求启用。"""

    def evaluate(self, facts: TurnFacts, state: TurnState) -> InterceptorVerdict:
        if (
            not facts.has_tool_calls
            and state.operation_request
            and detect_narrated_tool_call(facts.response_text)
        ):
            return InterceptorVerdict(action="schedule", corrective_kind="action")
        return InterceptorVerdict()


@dataclass
class QualityDecision:
    """拦截器链的合并处置（engine 据此执行，不改判断）。"""

    hallucination_suspected: bool = False
    claim_for_span: Optional[str] = None
    narrated: bool = False
    # "none" | "schedule" | "override"
    corrective_mode: str = "none"
    corrective_kind: Optional[str] = None
    fallback_claim: Optional[str] = None
    fallback_text: Optional[str] = None


class CorrectiveRetryScheduler:
    """消费三个拦截器的判定，合并成单一处置（不变量：corrective_used <= 1）。"""

    def __init__(self) -> None:
        self.interceptors = [
            TextualToolCallInterceptor(),
            FalseConfirmationInterceptor(),
            NarratedToolCallInterceptor(),
        ]

    def evaluate(self, facts: TurnFacts, state: TurnState) -> QualityDecision:
        decision = QualityDecision()
        # narrated 的"本轮已置 pending 则跳过"要在 textual/幻觉判定之后生效，
        # 先把 textual 的排队意图记下来
        textual_scheduled = False

        for index, interceptor in enumerate(self.interceptors):
            verdict = interceptor.evaluate(facts, state)
            if verdict.hallucination_suspected:
                decision.hallucination_suspected = True
                decision.claim_for_span = (
                    facts.truncated_claim or claim_kind(facts.detected_text)
                )
            if verdict.action == "fallback":
                decision.fallback_claim = verdict.fallback_claim
                decision.fallback_text = verdict.fallback_text
            elif verdict.action == "override":
                decision.corrective_mode = "override"
                decision.corrective_kind = verdict.corrective_kind
            elif verdict.action == "schedule":
                if index == 0:
                    textual_scheduled = True
                    decision.corrective_mode = "schedule"
                    decision.corrective_kind = verdict.corrective_kind
                else:
                    # narrated（index==2）：本轮 textual/幻觉已置 pending 时不再触发
                    pending_effective = (
                        state.corrective_pending
                        or textual_scheduled
                        or decision.corrective_mode == "override"
                    )
                    if not pending_effective and state.corrective_used == 0:
                        decision.narrated = True
                        decision.corrective_mode = "schedule"
                        decision.corrective_kind = verdict.corrective_kind
        return decision


# 引擎默认使用单例（无状态，可共享）
quality_gate = CorrectiveRetryScheduler()
