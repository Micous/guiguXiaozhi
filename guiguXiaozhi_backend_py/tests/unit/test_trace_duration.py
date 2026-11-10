"""trace 总耗时的回归测试。

历史缺陷：``total_duration_ms`` 把所有 span 的 duration_ms 相加，
父 span（http.request）与子 span（llm.chat / tool.execute）区间互相包含，
结果约为真实 wall time 的 2 倍。现在改为时间轴跨度。
"""

from __future__ import annotations

import json
from pathlib import Path

from xiaozhi.reliability.trace_store import TraceStore, _wall_time_ms
from xiaozhi.reliability.tracing import Span

ROOT = Path(__file__).resolve().parents[2]


def _span(name: str, trace_id: str, start: float, end: float, parent=None) -> Span:
    span = Span(
        name=name,
        trace_id=trace_id,
        parent_span_id=parent,
        span_id=f"{trace_id}-{name}",
    )
    span.start_time = start
    span.end_time = end
    return span


def test_wall_time_is_not_double_counted():
    root = _span("http.request", "t1", 1000.0, 1000.5)
    child = _span("llm.chat", "t1", 1000.1, 1000.4, root.span_id)

    store = TraceStore()
    store._on_span(root)
    store._on_span(child)

    summary = store.get_trace_tree("t1")["summary"]
    assert summary["total_duration_ms"] == 500.0  # 墙钟耗时，不是 500 + 300
    assert summary["span_duration_sum_ms"] == 800.0  # 各 span 自身耗时仍可见
    assert summary["span_count"] == 2


def test_parallel_root_spans_use_time_axis():
    first = _span("a", "t2", 1000.0, 1000.2)
    second = _span("b", "t2", 1000.1, 1000.3)

    store = TraceStore()
    store._on_span(first)
    store._on_span(second)

    assert store.list_traces()[0]["total_duration_ms"] == 300.0


def test_stats_average_uses_wall_time():
    root = _span("http.request", "t3", 1000.0, 1000.4)
    child = _span("tool.execute", "t3", 1000.1, 1000.3, root.span_id)
    store = TraceStore()
    store._on_span(root)
    store._on_span(child)

    assert store.stats()["avg_duration_ms"] == 400.0


def test_fallback_without_timestamps():
    spans = [{"duration_ms": 10.0}, {"duration_ms": 25.5}]
    assert _wall_time_ms(spans) == 35.5


def test_load_from_disk_rebuilds_summary_and_dedupes_spans():
    """旧版本每个 span 结束都追加一条完整快照，回放时不能重复叠加。"""
    root = _span("http.request", "t-old", 1000.0, 1000.5)
    child = _span("llm.chat", "t-old", 1000.1, 1000.4, root.span_id)

    persist_dir = ROOT / "logs" / "test_trace_duration"
    persist_dir.mkdir(parents=True, exist_ok=True)
    path = persist_dir / "traces.jsonl"
    path.unlink(missing_ok=True)
    snapshots = [
        {
            "trace_id": "t-old",
            "spans": [root.to_dict()],
            "summary": {"trace_id": "t-old", "total_duration_ms": 500.0},
        },
        {
            "trace_id": "t-old",
            "spans": [root.to_dict(), child.to_dict()],
            # 旧实现写下的 2 倍值，回放时应被重算
            "summary": {"trace_id": "t-old", "total_duration_ms": 800.0},
        },
    ]
    path.write_text(
        "\n".join(json.dumps(s, ensure_ascii=False) for s in snapshots) + "\n",
        encoding="utf-8",
    )

    store = TraceStore(persist_dir=str(persist_dir))
    assert store.load_from_disk() == 1

    raw = store.get_trace("t-old")
    assert raw is not None
    assert len(raw["spans"]) == 2  # 不是 3（1 + 2 重复叠加）
    assert {s["name"] for s in raw["spans"]} == {"http.request", "llm.chat"}
    assert raw["summary"]["total_duration_ms"] == 500.0
    assert raw["summary"]["span_duration_sum_ms"] == 800.0
    assert len(store.list_traces()) == 1

    path.unlink(missing_ok=True)
