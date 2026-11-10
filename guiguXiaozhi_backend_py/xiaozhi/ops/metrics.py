"""运行指标采集（对应 PLAN.md「3.4.5 指标」）。

不引入 prometheus_client 之类的依赖：进程内计数器 + 有界直方图即可覆盖
QPS / P50 / P95 / 错误率 / 工具成功率 / 空响应重试率 / 降级次数，
通过 ``GET /metrics`` 暴露，也便于单测断言。
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, Iterator, Optional

DEFAULT_COUNTERS = (
    "requests_total",
    "errors_total",
    "tool_calls_total",
    "tool_calls_success",
    "tool_calls_failed",
    "llm_calls_total",
    "empty_response_retries",
    "degraded_total",
    "rate_limited_total",
    "hallucination_suspected",
)


@dataclass
class HistogramSummary:
    count: int = 0
    avg_ms: float = 0.0
    p50_ms: float = 0.0
    p95_ms: float = 0.0
    max_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "count": self.count,
            "avg_ms": round(self.avg_ms, 2),
            "p50_ms": round(self.p50_ms, 2),
            "p95_ms": round(self.p95_ms, 2),
            "max_ms": round(self.max_ms, 2),
        }


class MetricsCollector:
    """线程安全的进程内指标收集器"""

    def __init__(self, max_samples: int = 1000, clock=time.monotonic) -> None:
        self._max_samples = max(1, max_samples)
        self._clock = clock
        self._started_at = clock()
        self._lock = threading.Lock()
        self._counters: Dict[str, float] = {name: 0.0 for name in DEFAULT_COUNTERS}
        self._gauges: Dict[str, float] = {}
        self._histograms: Dict[str, Deque[float]] = {}

    # ---------- 写入 ----------

    def inc(self, name: str, value: float = 1.0) -> None:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0.0) + value

    def set_gauge(self, name: str, value: float) -> None:
        with self._lock:
            self._gauges[name] = float(value)

    def observe(self, name: str, value_ms: float) -> None:
        with self._lock:
            bucket = self._histograms.setdefault(
                name, deque(maxlen=self._max_samples)
            )
            bucket.append(float(value_ms))

    @contextmanager
    def timer(self, name: str = "request_duration_ms") -> Iterator[None]:
        start = self._clock()
        try:
            yield
        finally:
            self.observe(name, (self._clock() - start) * 1000)

    # ---------- 业务封装 ----------

    def record_request(self, *, ok: bool = True, degraded: bool = False) -> None:
        self.inc("requests_total")
        if not ok:
            self.inc("errors_total")
        if degraded:
            self.inc("degraded_total")

    def record_tool(self, name: str, success: bool) -> None:
        self.inc("tool_calls_total")
        self.inc("tool_calls_success" if success else "tool_calls_failed")
        self.inc(f"tool.{name}.{'success' if success else 'failed'}")

    def record_llm(self, duration_ms: float) -> None:
        self.inc("llm_calls_total")
        self.observe("llm_duration_ms", duration_ms)

    # ---------- 读取 ----------

    @staticmethod
    def _summarize(values: Deque[float]) -> HistogramSummary:
        if not values:
            return HistogramSummary()
        ordered = sorted(values)
        count = len(ordered)

        def percentile(q: float) -> float:
            index = max(0, math.ceil(q * count) - 1)
            return ordered[min(index, count - 1)]

        return HistogramSummary(
            count=count,
            avg_ms=sum(ordered) / count,
            p50_ms=percentile(0.50),
            p95_ms=percentile(0.95),
            max_ms=ordered[-1],
        )

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            counters = dict(self._counters)
            gauges = dict(self._gauges)
            histograms = {k: deque(v, maxlen=self._max_samples) for k, v in self._histograms.items()}
            uptime = max(self._clock() - self._started_at, 1e-6)
        return {
            "uptime_seconds": round(uptime, 2),
            "counters": {k: round(v, 3) for k, v in sorted(counters.items())},
            "gauges": gauges,
            "histograms": {k: self._summarize(v).to_dict() for k, v in sorted(histograms.items())},
        }

    def summary(self) -> Dict[str, Any]:
        """给运维看的精简视图"""
        snap = self.snapshot()
        counters = snap["counters"]
        requests = counters.get("requests_total", 0.0)
        tools = counters.get("tool_calls_total", 0.0)
        latency = snap["histograms"].get("request_duration_ms", {})

        def ratio(num: float, den: float) -> float:
            return round(num / den, 4) if den else 0.0

        # uptime 在 snapshot 里被四舍五入到 2 位，刚启动时可能是 0.0
        uptime = max(float(snap["uptime_seconds"]), 1e-6)
        return {
            "requests_total": requests,
            "qps_avg": round(requests / uptime, 3),
            "error_rate": ratio(counters.get("errors_total", 0.0), requests),
            "tool_success_rate": ratio(counters.get("tool_calls_success", 0.0), tools),
            "empty_response_retries": counters.get("empty_response_retries", 0.0),
            "degraded_total": counters.get("degraded_total", 0.0),
            "rate_limited_total": counters.get("rate_limited_total", 0.0),
            "hallucination_suspected": counters.get("hallucination_suspected", 0.0),
            "latency": latency,
        }

    def reset(self) -> None:
        with self._lock:
            self._counters = {name: 0.0 for name in DEFAULT_COUNTERS}
            self._gauges.clear()
            self._histograms.clear()
            self._started_at = self._clock()
