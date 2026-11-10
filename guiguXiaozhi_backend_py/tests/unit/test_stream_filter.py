"""StreamFilter 单测 —— E3 拆分后新增的跨 chunk 边界覆盖。

这些用例在旧实现（逻辑内嵌 engine）下只能端到端测；现在直接喂 filter。
行为基线 = E1 特征测试钉住的旧内嵌实现，逐条对应。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from xiaozhi.ops.metrics import MetricsCollector  # noqa: E402
from xiaozhi.runtime.stream_filter import (  # noqa: E402
    LOOKBEHIND,
    StreamFilter,
    safe_prefix,
    trim_to_sentence,
)


def make_filter(**kwargs):
    metrics = kwargs.pop("metrics", None) or MetricsCollector()
    f = StreamFilter(
        claim_is_legit=kwargs.pop("claim_is_legit", False),
        query_executed_success=kwargs.pop("query_executed_success", False),
        metrics=metrics,
        trace_id=kwargs.pop("trace_id", "t"),
    )
    assert not kwargs
    return f, metrics


def feed_all(f, chunks):
    return "".join(out for c in chunks if (out := f.feed(c)))


# ---------- 回看窗口 ----------


def test_char_by_char_promise_never_leaks():
    """承诺词逐字跨 chunk 撕开时一个字都不漏（LOOKBEHIND 的核心保障）。"""
    f, metrics = make_filter()
    out = feed_all(f, list("已为您预约成功"))
    assert "预约成功" not in out
    assert f.truncated_claim == "book"
    assert f.suppressed
    assert metrics.snapshot()["counters"]["false_confirmation_truncated"] == 1
    # 流结束也不补发（suppressed）
    assert f.flush() == ""


def test_lookbehind_holds_tail_then_flush_releases():
    """窗口内的无害文本押后下发，flush() 补发余量。"""
    f, _ = make_filter()
    text = "这句话完全没有任何问题，就是普普通通的一段回答而已，说的都是实际情况。" * 2
    out = feed_all(f, [text])
    # 至少 LOOKBEHIND 个字被押后
    assert len(out) <= len(text) - LOOKBEHIND
    rest = f.flush()
    assert out + rest == text


def test_after_suppression_feed_returns_nothing_but_keeps_buffer():
    """拦截后继续喂内容：不下发，但缓冲继续增长（下游还要 tool_calls）。"""
    f, _ = make_filter()
    feed_all(f, ["已为您预约成功"])
    assert f.feed("预约信息如下") == ""
    assert f.turn_buffer.endswith("预约信息如下")  # 流还在被读完
    assert f.flush() == ""


# ---------- textual JSON 切割 ----------


def test_textual_json_cut_and_never_leaked():
    """纯文本 JSON 工具调用：前缀安全下发，JSON 段一个字不漏。"""
    f, metrics = make_filter()
    textual = '预约挂号\n{"username": "张三", "id_card": "610121199401032831"}'
    out = feed_all(f, ["好的。", textual])
    assert "610121199401032831" not in out
    assert "预约挂号" not in out
    assert "好的。" in out
    assert f.textual_tool_call
    assert metrics.snapshot()["counters"]["textual_tool_call_truncated"] == 1


def test_textual_json_split_across_chunks():
    """JSON 跨 chunk 撕开也能配对到收尾，不会提前放行。"""
    f, _ = make_filter()
    textual = '预约挂号\n{"username": "张三"}'
    out = feed_all(f, [textual[:8], textual[8:16], textual[16:]])
    assert "预约挂号" not in out
    assert '"username"' not in out
    assert f.textual_tool_call


# ---------- legit 放行 ----------


def test_claim_is_legit_disables_all_interception():
    """booking 真执行过 → 承诺是真话，全部放行。"""
    f, metrics = make_filter(claim_is_legit=True)
    text = "已为您预约成功，预约号：A12345"
    out = feed_all(f, [text]) + f.flush()
    assert out == text
    assert f.truncated_claim is None
    assert metrics.snapshot()["counters"].get("false_confirmation_truncated", 0) == 0


def test_query_claim_passes_only_when_query_executed():
    """查询类承诺：查询真执行过 → 放行；没执行过 → 拦截。"""
    text = "查询结果显示呼吸内科上午已约满。"

    f1, _ = make_filter(query_executed_success=True)
    out1 = feed_all(f1, [text]) + f1.flush()
    assert out1 == text  # 真话放行

    f2, metrics2 = make_filter(query_executed_success=False)
    out2 = feed_all(f2, [text])
    assert f2.truncated_claim == "query"
    assert metrics2.snapshot()["counters"]["false_confirmation_truncated"] == 1


# ---------- 条件语境（现行为保留）----------


def test_conditional_context_not_truncated():
    """承诺前 10 字含"不"→ 条件语境 → 不拦截（现行为的副作用，原样保留）。"""
    f, metrics = make_filter()
    text = "请稍等片刻不要走开，已为您预约成功"
    out = feed_all(f, [text]) + f.flush()
    assert "已为您预约成功" in out
    assert metrics.snapshot()["counters"].get("false_confirmation_truncated", 0) == 0


# ---------- safe_prefix ----------


def test_safe_prefix_trims_to_claim_start():
    prefix = "好的，我马上为您办理。"
    full = prefix + "已为您预约成功"
    out = safe_prefix(full, full)
    assert out == prefix


def test_safe_prefix_mismatch_returns_empty():
    """holder 文本与流式缓冲不一致（理论不该发生）→ 宁可丢也别漏。"""
    assert safe_prefix("流式缓冲文本", "完全不同") == ""


def test_trim_to_sentence_keeps_punctuation():
    assert trim_to_sentence("很遗憾，上午查询") == "很遗憾，"
    assert trim_to_sentence("第一句。第二句没说完") == "第一句。"
