"""真实服务集成测试（B3）。

覆盖：存活/就绪探针、工具注册表、指标与审计端点、一次真实对话及其追踪树。
**会真的调用本地 7B 模型**，单条对话 25–60s，默认跳过。

运行：
    XIAOZHI_PY_BASE_URL=http://127.0.0.1:8082 \
      ./.venv/bin/python -m pytest tests/integration -q --run-integration
"""

from __future__ import annotations

import time

import httpx
import pytest

pytestmark = pytest.mark.integration

EXPECTED_TOOLS = {
    "预约挂号",
    "取消预约挂号",
    "查询是否有号源",
    "查询科室专家",
}


def test_health(live_client: httpx.Client):
    resp = live_client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["service"] == "xiaozhi-python"
    assert body["status"] in {"ok", "shutting_down"}


def test_ready_probes(live_client: httpx.Client):
    resp = live_client.get("/ready", timeout=30.0)
    assert resp.status_code == 200, f"依赖未就绪：{resp.text[:300]}"
    body = resp.json()
    assert body["ready"] is True
    assert body["checks"], "探针注册表为空，组合根装配可能有问题"
    assert all(check["ok"] for check in body["checks"].values()), body["checks"]


def test_tools_registry(live_client: httpx.Client):
    resp = live_client.get("/tools")
    assert resp.status_code == 200
    names = {tool["name"] for tool in resp.json()["tools"]}
    assert EXPECTED_TOOLS.issubset(names), names


def test_metrics_and_audit_endpoints(live_client: httpx.Client):
    metrics = live_client.get("/metrics")
    assert metrics.status_code == 200
    summary = metrics.json()["summary"]
    for key in ("requests_total", "error_rate", "tool_success_rate"):
        assert key in summary, summary

    audit = live_client.get("/audit", params={"limit": 5})
    assert audit.status_code == 200
    assert isinstance(audit.json()["entries"], list)

    traces = live_client.get("/traces", params={"limit": 5})
    assert traces.status_code == 200
    assert isinstance(traces.json()["traces"], list)


def test_unknown_trace_returns_404(live_client: httpx.Client):
    resp = live_client.get("/trace/this-trace-does-not-exist")
    assert resp.status_code == 404


def test_chat_roundtrip_emits_trace(live_client: httpx.Client):
    """一次真实对话：文本 + X-Trace-Id + 可回查的追踪树 + 指标计数。"""
    before = live_client.get("/metrics").json()["summary"]["requests_total"]

    memory_id = f"integration-{int(time.time())}"
    resp = live_client.post(
        "/xiaozhi/chat",
        json={"memoryId": memory_id, "message": "你好，请用一句话介绍你自己"},
        timeout=180.0,
    )
    assert resp.status_code == 200, resp.text[:300]
    assert resp.text.strip(), "对话返回了空文本"

    trace_id = resp.headers.get("X-Trace-Id")
    assert trace_id, "响应头缺少 X-Trace-Id"

    tree = live_client.get(f"/trace/{trace_id}", timeout=10.0)
    assert tree.status_code == 200
    body = tree.json()
    assert body["root_spans"], "追踪树没有根 span"
    span_names = {span["name"] for span in _walk(body["root_spans"])}
    assert "http.request" in span_names
    assert "llm.chat" in span_names
    # 修复后的总时长应当是墙钟耗时，而不是父子 span 累加出来的 2 倍
    assert body["summary"]["total_duration_ms"] > 0

    after = live_client.get("/metrics").json()["summary"]["requests_total"]
    assert after > before


def _walk(nodes):
    for node in nodes:
        yield node
        yield from _walk(node.get("children") or [])
