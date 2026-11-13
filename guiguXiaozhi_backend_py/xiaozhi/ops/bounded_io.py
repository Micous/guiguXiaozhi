"""B3-1：有界、可观测的专用阻塞 IO 执行器。

整改方案 §1.4 要求把 async 链路里的同步阻塞调用统一搬到「有界、可观测、
可排空」的专用执行器上，替换散落的 ``asyncio.to_thread``（无界默认线程池）。

设计约束（B3-1 是「行为不变」批次）：

* ``run()`` 对调用方的契约与 ``asyncio.to_thread`` 完全一致 —— 返回值、
  异常类型、异常对象原样传递；``contextvars`` 通过 ``copy_context()`` 传播
  （``loop.run_in_executor`` 默认不传播，trace / approval 上下文会丢）。
* **不静默丢弃任务**：超过在途上限时在 async 层排队等待，不抛错、不丢弃。
  显式拒绝策略属于后续灰度，不在 B3-1 引入（那会是行为变更）。
* 有界性由两层保证：``ThreadPoolExecutor(max_workers=...)`` 限制真实线程数，
  ``asyncio.Semaphore(max_inflight)`` 限制「执行中 + 排队中」的任务数。
* 每个 event loop 持有自己的 Semaphore（3.11 的 ``asyncio.Semaphore`` 绑定
  loop，跨 loop 复用会抛 ``RuntimeError``）；线程池与计数器跨 loop 共享。
* ``aclose()`` 先停止收新任务、再等在途任务排空、最后关闭线程池；
  不创建无主 ``create_task``（方案 §1.4 策略 4）。

组合根通过 ``configure_bounded_io()`` 装配带指标的实例；离线单测可直接使用
独立实例，互不干扰。
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, Optional, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class IOExecutorClosed(RuntimeError):
    """执行器已关闭后仍有提交 —— 显式失败，绝不静默丢弃。"""


class BoundedIOExecutor:
    """专用有界线程池 + 在途上限 + 指标 + shutdown 排空。"""

    def __init__(
        self,
        *,
        max_workers: int = 8,
        max_inflight: int = 64,
        metrics: Optional[Any] = None,
        clock: Callable[[], float] = time.monotonic,
        thread_name_prefix: str = "xiaozhi-io",
    ) -> None:
        self._max_workers = max(1, int(max_workers))
        self._max_inflight = max(self._max_workers, int(max_inflight))
        self._metrics = metrics
        self._clock = clock
        self._pool = ThreadPoolExecutor(
            max_workers=self._max_workers, thread_name_prefix=thread_name_prefix
        )
        # asyncio.Semaphore 绑定首次使用的 loop；pytest 每个用例一个新 loop，
        # 因此按 loop 分别持有，线程池本身跨 loop 共享且线程安全。
        self._sems: Dict[asyncio.AbstractEventLoop, asyncio.Semaphore] = {}
        self._sems_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._inflight = 0
        self._submitted = 0
        self._completed = 0
        self._failed = 0
        self._closed = False
        self._shutdown_done = False
        self._drained = threading.Event()
        self._drained.set()

    # ---------- 内部 ----------

    def _semaphore(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        with self._sems_lock:
            sem = self._sems.get(loop)
            if sem is None:
                sem = asyncio.Semaphore(self._max_inflight)
                self._sems[loop] = sem
            return sem

    def _inc(self, name: str, value: float = 1.0) -> None:
        if self._metrics is not None:
            self._metrics.inc(name, value)

    def _observe(self, name: str, value_ms: float) -> None:
        if self._metrics is not None:
            self._metrics.observe(name, value_ms)

    def _gauge(self, name: str, value: float) -> None:
        if self._metrics is not None:
            self._metrics.set_gauge(name, value)

    def _on_submit(self) -> None:
        with self._state_lock:
            self._submitted += 1
            self._inflight += 1
            if self._inflight == 1:
                self._drained.clear()
            depth = self._inflight
        self._inc("io_executor_submitted_total")
        self._gauge("io_executor_inflight", depth)

    def _on_finish(self, *, ok: bool, duration_ms: float) -> None:
        with self._state_lock:
            self._inflight -= 1
            if ok:
                self._completed += 1
            else:
                self._failed += 1
            depth = self._inflight
            if depth == 0:
                self._drained.set()
        self._inc("io_executor_completed_total" if ok else "io_executor_failed_total")
        self._observe("io_executor_duration_ms", duration_ms)
        self._gauge("io_executor_inflight", depth)

    # ---------- 对外 ----------

    @property
    def closed(self) -> bool:
        return self._closed

    def snapshot(self) -> Dict[str, Any]:
        """执行器自身的可观测快照（不依赖 MetricsCollector）。"""
        with self._state_lock:
            return {
                "max_workers": self._max_workers,
                "max_inflight": self._max_inflight,
                "inflight": self._inflight,
                "submitted": self._submitted,
                "completed": self._completed,
                "failed": self._failed,
                "closed": self._closed,
            }

    async def run(
        self,
        fn: Callable[..., T],
        /,
        *args: Any,
        wait_timeout: Optional[float] = None,
        **kwargs: Any,
    ) -> T:
        """在专用线程池里跑一个同步调用，返回值/异常原样传递。

        ``wait_timeout`` 默认 None（与 ``asyncio.to_thread`` 一致）；显式传入时
        用 ``asyncio.wait_for`` 限界，超时只停止本地等待，不杀工作线程 ——
        与方案 §1.3「超时不代表对端停止处理」保持一致。
        刻意不占用 ``timeout`` 这个名字：业务函数（例如 ``HisClient.request``）
        常自带同名参数，必须能原样透传到 kwargs。
        """
        if self._closed:
            raise IOExecutorClosed("有界 IO 执行器已关闭，拒绝提交新任务")
        loop = asyncio.get_running_loop()
        sem = self._semaphore()
        ctx = contextvars.copy_context()
        call = functools.partial(fn, *args, **kwargs)

        wait_started = self._clock()
        await sem.acquire()
        # 关闭与排队可能竞态：拿到槽位后二次确认，绝不在 shutdown 之后偷偷提交。
        if self._closed:
            sem.release()
            raise IOExecutorClosed("有界 IO 执行器已关闭，拒绝提交新任务")
        self._observe(
            "io_executor_queue_wait_ms", (self._clock() - wait_started) * 1000
        )
        self._on_submit()
        run_started = self._clock()
        ok = False
        try:
            future = loop.run_in_executor(self._pool, ctx.run, call)
            if wait_timeout is not None:
                try:
                    result = await asyncio.wait_for(future, timeout=wait_timeout)
                except (asyncio.TimeoutError, TimeoutError):
                    self._inc("io_executor_timeout_total")
                    raise
            else:
                result = await future
            ok = True
            return result
        finally:
            # 异常原样抛出（不在这里捕获），只做记账与槽位释放。
            self._on_finish(
                ok=ok, duration_ms=(self._clock() - run_started) * 1000
            )
            sem.release()

    async def aclose(self, drain_timeout: float = 10.0) -> None:
        """停止收新任务 → 等在途排空 → 关闭线程池。可重复调用。"""
        with self._state_lock:
            if self._shutdown_done:
                return
            self._shutdown_done = True
        self._closed = True
        self._inc("io_executor_closed_total")
        loop = asyncio.get_running_loop()
        drained = await asyncio.to_thread(
            self._drained.wait, max(0.0, float(drain_timeout))
        )
        if not drained:
            logger.warning(
                "有界 IO 执行器排空超时（%.1fs），仍有 %d 个在途任务",
                drain_timeout,
                self.snapshot()["inflight"],
            )
        # shutdown(wait=True) 会阻塞调用线程；停机路径放到默认线程池里 await。
        await loop.run_in_executor(None, functools.partial(self._pool.shutdown, wait=True))


# ---------------------------------------------------------------- 进程级默认实例

_default: Optional[BoundedIOExecutor] = None
_default_lock = threading.Lock()


def get_bounded_io() -> BoundedIOExecutor:
    """取进程级执行器。

    组合根应先调用 ``configure_bounded_io()`` 注入带指标的实例；没有装配时
    lazy 创建一个默认实例，保证离线单测与旧调用方式零改动。
    """
    global _default
    with _default_lock:
        if _default is None:
            _default = BoundedIOExecutor()
        return _default


def configure_bounded_io(executor: BoundedIOExecutor) -> BoundedIOExecutor:
    """组合根装配：替换默认实例并返回它，便于放进 ``Application``。"""
    global _default
    with _default_lock:
        _default = executor
        return _default


def release_bounded_io(executor: BoundedIOExecutor) -> None:
    """停机钩子：仅当全局实例仍是本实例时清空。

    应用可能在单进程内被多次装配 / 关闭（测试、热重载）。若直接置 None，
    会误伤「后一次装配」注入的新实例；若不清空，则下一次请求会拿到一个
    已 ``aclose()`` 的执行器并抛 ``IOExecutorClosed``。
    """
    global _default
    with _default_lock:
        if _default is executor:
            _default = None


async def run_sync_io(fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """便捷入口：把一次同步阻塞 IO offload 到进程级有界执行器。

    async 调用方只需把 ``svc.query(a, b=k)`` 改成
    ``await run_sync_io(svc.query, a, b=k)``，返回值与异常原样传递，
    ``contextvars`` 由执行器负责传播。
    """
    return await get_bounded_io().run(fn, *args, **kwargs)


def reset_bounded_io() -> None:
    """仅供测试：清掉默认实例（不关闭线程池，避免影响其它用例）。"""
    global _default
    with _default_lock:
        _default = None
