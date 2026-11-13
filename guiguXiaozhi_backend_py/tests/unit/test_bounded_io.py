"""B3-1：有界阻塞 IO 执行器的护栏与等价性判据。

覆盖《主体Agent整改-B3-调研与分批实施方案》§3.1 的四条停止条件：
  1. 调用契约与直接同步调用逐字一致（返回值 / 异常对象 / kwargs）；
  2. 同步阻塞不再卡住 event loop，且 contextvars 不丢；
  3. 队列满时不静默丢弃任务，真实并发受 max_workers 限制；
  4. shutdown 排空后拒绝新任务，且不留下无主任务。
另外覆盖 HIS 专用异步入口 ``request_async`` 的等价性与 offload 事实。
"""

from __future__ import annotations

import asyncio
import contextvars
import threading
import time
from typing import Any, Dict, List

import pytest

from xiaozhi.ops.bounded_io import (
    BoundedIOExecutor,
    IOExecutorClosed,
    configure_bounded_io,
    get_bounded_io,
    release_bounded_io,
    reset_bounded_io,
    run_sync_io,
)
from xiaozhi.service.his_client import HisClient


class FakeMetrics:
    """最小指标桩：只记录名字，用于断言执行器的可观测性。"""

    def __init__(self) -> None:
        self.counters: Dict[str, float] = {}
        self.gauges: Dict[str, float] = {}
        self.histograms: Dict[str, List[float]] = {}

    def inc(self, name: str, value: float = 1.0) -> None:
        self.counters[name] = self.counters.get(name, 0.0) + value

    def set_gauge(self, name: str, value: float) -> None:
        self.gauges[name] = value

    def observe(self, name: str, value_ms: float) -> None:
        self.histograms.setdefault(name, []).append(value_ms)


@pytest.fixture
def io_exec() -> BoundedIOExecutor:
    """每个用例一个独立执行器，避免共享线程池互相干扰。"""
    reset_bounded_io()
    executor = BoundedIOExecutor(max_workers=2, max_inflight=8)
    yield executor
    reset_bounded_io()
    executor._pool.shutdown(wait=True)  # noqa: SLF001 - 测试清理，避免遗留线程


# ---------------------------------------------------------------- 1. 调用契约

async def test_direct_call_and_offloaded_call_are_equivalent(io_exec: BoundedIOExecutor):
    """§3.1 判据 1：同一输入下返回值逐字一致。"""

    def compute(a: int, *, b: int = 0, timeout: float = 0.0) -> Dict[str, Any]:
        # 刻意使用 timeout 这个名字：执行器不得吞掉业务同名 kwarg。
        return {"sum": a + b, "timeout": timeout}

    cases = [(1, 0), (5, 7), (-3, 3)]
    for a, b in cases:
        direct = compute(a, b=b, timeout=1.5)
        offloaded = await io_exec.run(compute, a, b=b, timeout=1.5)
        assert offloaded == direct
    assert io_exec.snapshot()["completed"] == len(cases)
    assert io_exec.snapshot()["inflight"] == 0


async def test_exception_is_the_same_object(io_exec: BoundedIOExecutor):
    """§3.1 判据 1：异常类型与异常对象都不得被包装或替换。"""
    sentinel = ValueError("原始异常")

    def boom() -> None:
        raise sentinel

    with pytest.raises(ValueError) as caught:
        await io_exec.run(boom)
    assert caught.value is sentinel
    snap = io_exec.snapshot()
    assert snap["failed"] == 1
    assert snap["inflight"] == 0
    assert snap["completed"] == 0


async def test_contextvars_are_propagated_to_worker(io_exec: BoundedIOExecutor):
    """trace_id / approval 等 ContextVar 必须穿透线程池。"""
    var = contextvars.ContextVar("b3_probe", default="unset")
    var.set("trace-42")
    assert await io_exec.run(var.get) == "trace-42"


# ---------------------------------------------------------------- 2. 不阻塞 event loop

async def test_blocking_call_does_not_stall_event_loop(io_exec: BoundedIOExecutor):
    """§3.1 判据 2：阻塞 IO 期间事件循环仍可推进。"""
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.005)
            ticks += 1

    ticker_task = asyncio.create_task(ticker())
    try:
        started = time.monotonic()
        result = await io_exec.run(time.sleep, 0.12)
        elapsed = time.monotonic() - started
        assert result is None
        assert ticks >= 5, f"事件循环被卡住，仅推进 {ticks} 次"
        assert elapsed >= 0.10
    finally:
        ticker_task.cancel()
        try:
            await ticker_task
        except asyncio.CancelledError:
            pass


async def test_max_workers_caps_real_concurrency():
    """有界性：真实同时执行的线程数不超过 max_workers。"""
    reset_bounded_io()
    executor = BoundedIOExecutor(max_workers=2, max_inflight=16)
    lock = threading.Lock()
    state = {"active": 0, "peak": 0}

    def job() -> None:
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        time.sleep(0.03)
        with lock:
            state["active"] -= 1

    try:
        await asyncio.gather(*(executor.run(job) for _ in range(8)))
        assert state["peak"] <= 2
        assert state["peak"] >= 1
    finally:
        reset_bounded_io()
        executor._pool.shutdown(wait=True)  # noqa: SLF001


# ---------------------------------------------------------------- 3. 不静默丢弃

async def test_queue_saturation_never_drops_tasks():
    """§3.1 判据 3：在途上限只让任务排队，不得丢弃。"""
    reset_bounded_io()
    executor = BoundedIOExecutor(max_workers=1, max_inflight=3)
    try:
        results = await asyncio.gather(
            *(executor.run(lambda i=i: i) for i in range(12))
        )
        assert results == list(range(12))
        snap = executor.snapshot()
        assert snap["submitted"] == 12
        assert snap["completed"] == 12
        assert snap["failed"] == 0
        assert snap["inflight"] == 0
    finally:
        reset_bounded_io()
        executor._pool.shutdown(wait=True)  # noqa: SLF001


async def test_wait_timeout_raises_and_accounts_for_failure(io_exec: BoundedIOExecutor):
    """显式等待超时：原样抛 TimeoutError，并在账本里记一次失败。"""
    with pytest.raises((asyncio.TimeoutError, TimeoutError)):
        await io_exec.run(time.sleep, 0.15, wait_timeout=0.02)
    snap = io_exec.snapshot()
    assert snap["failed"] == 1
    assert snap["inflight"] == 0


# ---------------------------------------------------------------- 4. shutdown / 可观测

async def test_aclose_drains_and_rejects_new_submissions():
    """§3.1 判据 4：排空后拒绝收新任务，且 shutdown 幂等。"""
    reset_bounded_io()
    executor = BoundedIOExecutor(max_workers=2, max_inflight=8)
    done = threading.Event()
    started = threading.Event()

    def slow() -> str:
        started.set()
        time.sleep(0.05)
        done.set()
        return "ok"

    task = asyncio.create_task(executor.run(slow))
    await asyncio.to_thread(started.wait, 1.0)
    await executor.aclose(drain_timeout=2.0)
    assert (await task) == "ok"
    assert done.is_set()
    assert executor.closed is True
    assert executor.snapshot()["inflight"] == 0
    await executor.aclose(drain_timeout=0.1)  # 幂等
    with pytest.raises(IOExecutorClosed):
        await executor.run(lambda: 1)
    reset_bounded_io()


async def test_metrics_and_snapshot_are_observable():
    """执行器自身快照 + 组合根注入的 MetricsCollector 都要有数。"""
    metrics = FakeMetrics()
    reset_bounded_io()
    executor = BoundedIOExecutor(max_workers=2, max_inflight=8, metrics=metrics)
    try:
        assert await executor.run(lambda: 7) == 7
        with pytest.raises(RuntimeError):
            await executor.run(lambda: (_ for _ in ()).throw(RuntimeError("x")))
        assert metrics.counters["io_executor_submitted_total"] == 2
        assert metrics.counters["io_executor_completed_total"] == 1
        assert metrics.counters["io_executor_failed_total"] == 1
        assert "io_executor_queue_wait_ms" in metrics.histograms
        assert "io_executor_duration_ms" in metrics.histograms
        assert metrics.gauges["io_executor_inflight"] == 0
    finally:
        reset_bounded_io()
        executor._pool.shutdown(wait=True)  # noqa: SLF001


async def test_second_event_loop_gets_its_own_semaphore():
    """跨 loop 复用线程池不能抛「Semaphore bound to a different loop」。"""
    reset_bounded_io()
    executor = BoundedIOExecutor(max_workers=2, max_inflight=8)
    try:
        assert await executor.run(lambda: "loop-A") == "loop-A"

        def other_loop() -> str:
            return asyncio.run(executor.run(lambda: "loop-B"))

        assert await asyncio.to_thread(other_loop) == "loop-B"
        assert executor.snapshot()["completed"] == 2
    finally:
        reset_bounded_io()
        executor._pool.shutdown(wait=True)  # noqa: SLF001


async def test_run_sync_io_uses_configured_instance():
    """组合根装配的实例必须是请求链路实际使用的那一个。"""
    reset_bounded_io()
    configured = BoundedIOExecutor(max_workers=2, max_inflight=8)
    fresh = None
    configure_bounded_io(configured)
    try:
        assert await run_sync_io(lambda x: x + 1, 41) == 42
        assert get_bounded_io() is configured
        assert configured.snapshot()["completed"] == 1
        release_bounded_io(configured)
        # 释放后重新 lazy 创建，不会复用已关闭 / 已释放的实例
        fresh = get_bounded_io()
        assert fresh is not configured
    finally:
        reset_bounded_io()
        configured._pool.shutdown(wait=True)  # noqa: SLF001
        if fresh is not None:
            fresh._pool.shutdown(wait=True)  # noqa: SLF001


# ---------------------------------------------------------------- HIS 专用异步入口

async def test_his_request_async_is_equivalent_and_offloaded(monkeypatch):
    """``request_async`` 是整段同步重试循环的 offload 包装。

    把 ``request()`` 改成 async 会破坏同步契约；把整段调用搬到线程池，
    既让 ``time.sleep`` 退避离开 event loop，又保持返回值与异常逐字不变。
    """
    reset_bounded_io()
    client = HisClient("http://his.invalid")
    seen: Dict[str, Any] = {}
    main_thread = threading.get_ident()
    response = object()

    def fake_request(method: str, path: str, **kwargs: Any) -> object:
        seen["method"] = method
        seen["path"] = path
        seen["kwargs"] = kwargs
        seen["thread"] = threading.get_ident()
        return response

    monkeypatch.setattr(client, "request", fake_request)
    try:
        got = await client.request_async(
            "POST", "/booking", body={"a": 1}, idempotency_key="k", timeout=3.0
        )
        assert got is response
        assert seen["method"] == "POST"
        assert seen["path"] == "/booking"
        assert seen["kwargs"] == {
            "params": None,
            "body": {"a": 1},
            "idempotency_key": "k",
            "timeout": 3.0,
        }
        assert seen["thread"] != main_thread

        # 异常同样原样透传，不被包装成执行器错误
        def failing(*_args: Any, **_kwargs: Any) -> None:
            raise KeyError("his-down")

        monkeypatch.setattr(client, "request", failing)
        with pytest.raises(KeyError, match="his-down"):
            await client.request_async("GET", "/slot")
    finally:
        reset_bounded_io()
