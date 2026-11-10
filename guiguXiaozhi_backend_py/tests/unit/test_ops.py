"""运维组件单测：指标 / 审计 / 健康检查 / 限流 / 优雅停机。"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from xiaozhi.ops.audit import AuditLogger  # noqa: E402
from xiaozhi.ops.lifecycle import GracefulShutdown, HealthChecker, RateLimiter  # noqa: E402
from xiaozhi.ops.metrics import MetricsCollector  # noqa: E402


# ---------------- 指标 ----------------

def test_metrics_counters_and_summary():
    metrics = MetricsCollector()
    metrics.record_request(ok=True)
    metrics.record_request(ok=False, degraded=True)
    metrics.record_tool("预约挂号", True)
    metrics.record_tool("预约挂号", False)
    metrics.inc("empty_response_retries")

    summary = metrics.summary()
    assert summary["requests_total"] == 2
    assert summary["error_rate"] == 0.5
    assert summary["tool_success_rate"] == 0.5
    assert summary["degraded_total"] == 1
    assert summary["empty_response_retries"] == 1


def test_metrics_histogram_percentiles_and_timer():
    metrics = MetricsCollector()
    for value in (10, 20, 30, 40, 100):
        metrics.observe("request_duration_ms", value)
    with metrics.timer():
        pass

    histogram = metrics.snapshot()["histograms"]["request_duration_ms"]
    assert histogram["count"] == 6
    assert histogram["p50_ms"] == 20
    assert histogram["p95_ms"] == 100
    assert histogram["max_ms"] == 100


def test_metrics_reset():
    metrics = MetricsCollector()
    metrics.inc("requests_total", 5)
    metrics.reset()
    assert metrics.snapshot()["counters"]["requests_total"] == 0


# ---------------- 审计 ----------------

def test_audit_logger_persists_redacted_jsonl():
    # 用项目内的临时目录，避免依赖 pytest 的系统临时目录（沙箱可能不允许创建）
    log_root = ROOT / "logs"
    log_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="audit-test-", dir=str(log_root)) as tmp:
        path = Path(tmp) / "audit.jsonl"
        audit = AuditLogger(str(path))
        audit.record(
            "tool_execution",
            tool_name="预约挂号",
            id_card="110101198805123456",
            api_key="sk-abcdef123456",
            trace_id="t-1",
        )
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["event_type"] == "tool_execution"
        assert entry["id_card"] == "[REDACTED]"
        assert entry["api_key"] == "[REDACTED]"
        assert "110101198805123456" not in lines[0]
        assert audit.read_recent()[0]["tool_name"] == "预约挂号"


def test_audit_logger_memory_only():
    audit = AuditLogger(None)
    audit.record("permission_decision", decision="allow")
    assert audit.read_recent(limit=1)[0]["decision"] == "allow"
    audit.clear()
    assert audit.read_recent() == []


# ---------------- 健康检查 / 限流 / 停机 ----------------

async def test_health_checker_handles_sync_async_and_failing_probes():
    health = HealthChecker()

    async def async_ok():
        return {"ok": True, "name": "qwen2.5:7b"}

    health.register("model", async_ok)
    health.register("milvus", lambda: {"ok": True, "rows": 12})

    def broken():
        raise RuntimeError("连不上")

    health.register("mysql", broken)

    report = await health.check_all()
    assert report["ready"] is False
    assert report["checks"]["model"]["ok"] is True
    assert report["checks"]["milvus"]["rows"] == 12
    assert "RuntimeError" in report["checks"]["mysql"]["error"]


def test_rate_limiter_sliding_window():
    clock = {"now": 0.0}

    def now():
        return clock["now"]

    limiter = RateLimiter(max_requests=2, window_seconds=10.0, clock=now)
    assert limiter.allow("user-1")
    assert limiter.allow("user-1")
    assert not limiter.allow("user-1")
    assert limiter.remaining("user-1") == 0
    assert limiter.allow("user-2")  # 不同 key 互不影响

    clock["now"] = 11.0
    assert limiter.allow("user-1")
    assert limiter.snapshot()["rejected_total"] == 1


async def test_graceful_shutdown_waits_for_inflight():
    shutdown = GracefulShutdown(drain_timeout=0.2)
    assert not shutdown.shutting_down

    async with shutdown.inflight_request():
        assert shutdown.inflight == 1
        shutdown.begin()
        assert shutdown.shutting_down
        assert not await shutdown.wait_until_drained(timeout=0.01)

    assert shutdown.inflight == 0
    assert await shutdown.wait_until_drained()
