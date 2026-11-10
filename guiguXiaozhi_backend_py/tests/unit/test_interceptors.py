"""拦截器链单测 —— E4 拆分后质量防线的独立覆盖。

核心钉子：engine 源码不得 import 具体检测（正则/检测函数）——
以后加换皮句式只改 models/quality.py，engine 一行不动。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from xiaozhi.models.ollama import ChatTurn  # noqa: E402
from xiaozhi.models.quality import inspect_turn  # noqa: E402
from xiaozhi.runtime.interceptors import (  # noqa: E402
    FalseConfirmationInterceptor,
    NarratedToolCallInterceptor,
    TextualToolCallInterceptor,
    TurnFacts,
    quality_gate,
)
from xiaozhi.runtime.state import TurnState  # noqa: E402


def facts_for(text, tool_calls=None, truncated_claim=None, tool_executed=False):
    turn = ChatTurn(text=text, tool_calls=tool_calls or [])
    finding = inspect_turn(
        turn, ["预约挂号", "查询是否有号源"], tool_executed=tool_executed
    )
    return TurnFacts(
        detected_text=text,
        response_text=text,
        tool_calls=tool_calls or [],
        finding=finding,
        truncated_claim=truncated_claim,
    )


# ---------- engine 隔离钉子 ----------


def test_engine_does_not_import_concrete_detectors():
    """engine 源码不得出现具体检测（正则名/检测函数）——换皮句式只改 quality.py。"""
    engine_src = (
        (Path(ROOT) / "xiaozhi" / "runtime" / "engine.py")
        .read_text(encoding="utf-8")
    )
    for forbidden in (
        "_NARRATED_TOOL_CALL",
        "_FALSE_CONFIRMATION",
        "claim_span",
        "detect_narrated_tool_call",
        "fallback_for",
        "claim_kind(",
    ):
        assert forbidden not in engine_src, f"engine 泄漏了具体检测：{forbidden}"


# ---------- 各拦截器 ----------


def test_textual_interceptor_schedules_tool_call_retry():
    state = TurnState()
    verdict = TextualToolCallInterceptor().evaluate(
        facts_for('预约挂号\n{"username": "张三"}'), state
    )
    assert verdict.action == "schedule"
    assert verdict.corrective_kind == "tool_call"


def test_false_confirmation_schedules_when_fresh():
    state = TurnState()
    verdict = FalseConfirmationInterceptor().evaluate(
        facts_for("已为您预约成功"), state
    )
    assert verdict.action == "override"
    assert verdict.corrective_kind == "book"
    assert verdict.hallucination_suspected


def test_false_confirmation_falls_back_after_retry_used():
    state = TurnState(corrective_used=1)
    verdict = FalseConfirmationInterceptor().evaluate(
        facts_for("已为您预约成功"), state
    )
    assert verdict.action == "fallback"
    assert verdict.fallback_claim == "book"
    assert verdict.fallback_text  # 兜底文案已生成


def test_false_confirmation_suppressed_when_prior_booking():
    """历史里真挂过号 → 承诺是真话，不判定幻觉。

    （与 engine 一致：inspect_turn 的 tool_executed 由 state 派生，
    prior_booking 时 finding.false_confirmation 本身就被压掉。）
    """
    state = TurnState(prior_booking=True)
    verdict = FalseConfirmationInterceptor().evaluate(
        facts_for("您的挂号已经预约好了", tool_executed=True), state
    )
    assert verdict.action == "pass"


def test_narrated_interceptor_only_for_operation_requests():
    narrated = facts_for("好的，我将为您查询呼吸内科的号源。")

    state_op = TurnState(operation_request=True)
    verdict_op = NarratedToolCallInterceptor().evaluate(narrated, state_op)
    assert verdict_op.action == "schedule"
    assert verdict_op.corrective_kind == "action"

    # 知识问答请求：引擎根本不启用此检测（旧语义）
    state_kb = TurnState(operation_request=False)
    verdict_kb = NarratedToolCallInterceptor().evaluate(narrated, state_kb)
    assert verdict_kb.action == "pass"


def test_narrated_interceptor_skips_tool_call_turns():
    state = TurnState(operation_request=True)
    facts = facts_for("好的，我将为您查询号源。", tool_calls=[object()])
    verdict = NarratedToolCallInterceptor().evaluate(facts, state)
    assert verdict.action == "pass"


# ---------- 链合并语义 ----------


def test_gate_narrated_skipped_when_false_confirmation_scheduled():
    """本轮幻觉已调度 → narrated 不再触发（旧语义：not corrective_pending）。"""
    state = TurnState(operation_request=True)
    verdict = quality_gate.evaluate(
        facts_for("已为您预约成功。请稍等，我将为您查询呼吸内科的号源。"), state
    )
    assert verdict.corrective_mode == "override"
    assert not verdict.narrated


def test_gate_merges_textual_and_suppresses_narrated():
    state = TurnState(operation_request=True)
    verdict = quality_gate.evaluate(
        facts_for('预约挂号\n{"username": "张三"}'), state
    )
    assert verdict.corrective_mode == "schedule"
    assert verdict.corrective_kind == "tool_call"
    assert not verdict.narrated


def test_gate_narrated_passes_through_when_clean():
    """干净的叙述承诺 → narrated 触发，schedule("action")。"""
    state = TurnState(operation_request=True)
    verdict = quality_gate.evaluate(
        facts_for("好的，我将为您查询呼吸内科的号源。"), state
    )
    assert verdict.narrated
    assert verdict.corrective_mode == "schedule"
    assert verdict.corrective_kind == "action"


def test_gate_ignores_narrated_when_retry_budget_used():
    state = TurnState(operation_request=True, corrective_used=1)
    verdict = quality_gate.evaluate(
        facts_for("好的，我将为您查询呼吸内科的号源。"), state
    )
    assert not verdict.narrated
    assert verdict.corrective_mode == "none"
