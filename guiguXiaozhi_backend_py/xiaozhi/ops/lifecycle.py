"""上线运维：健康检查 / 限流 / 优雅停机。

对应 PLAN.md「3.4.5 运维要求」。三个组件都可离线单测：
* ``HealthChecker``：探针注册表，``/ready`` 不再把依赖判断硬编码在路由里；
* ``RateLimiter``：滑动窗口，按 memoryId 与来源 IP 限流；
* ``GracefulShutdown``：SIGTERM 后停止收新请求、等在途请求完成再释放连接。
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Deque, Dict, Optional, Union

Probe = Callable[[], Union[Dict[str, Any], Awaitable[Dict[str, Any]]]]


@dataclass
class ProbeResult:
    ok: bool
    detail: Dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        payload = {"ok": self.ok, **self.detail}
        if self.error:
            payload["error"] = self.error
        return payload


class HealthChecker:
    """依赖探针注册表"""

    def __init__(self) -> None:
        self._probes: Dict[str, Probe] = {}

    def register(self, name: str, probe: Probe) -> None:
        self._probes[name] = probe

    def names(self) -> list:
        return sorted(self._probes)

    async def check_all(self, timeout_per_probe: float = 2.0) -> Dict[str, Any]:
        """逐个跑探针；同步探针放线程池，且每个都有超时上限。

        就绪探针必须**有界**：某个依赖（例如 Milvus 的 count）卡住时，
        /ready 不能被拖成一次长阻塞请求。
        """
        checks: Dict[str, Any] = {}
        for name, probe in sorted(self._probes.items()):
            try:
                if inspect.iscoroutinefunction(probe):
                    result = await asyncio.wait_for(probe(), timeout=timeout_per_probe)
                else:
                    result = await asyncio.wait_for(
                        asyncio.to_thread(probe), timeout=timeout_per_probe
                    )
                if inspect.isawaitable(result):
                    result = await asyncio.wait_for(result, timeout=timeout_per_probe)
                detail = dict(result or {})
                ok = bool(detail.get("ok", True))
                checks[name] = ProbeResult(ok=ok, detail=detail).to_dict()
            except (asyncio.TimeoutError, TimeoutError):
                checks[name] = ProbeResult(
                    ok=False, error=f"probe timeout after {timeout_per_probe}s"
                ).to_dict()
            except Exception as exc:
                checks[name] = ProbeResult(
                    ok=False, error=f"{type(exc).__name__}: {exc}"
                ).to_dict()
        return {"ready": all(c.get("ok", False) for c in checks.values()), "checks": checks}


class RateLimiter:
    """滑动窗口限流（线程安全）"""

    def __init__(
        self,
        max_requests: int = 30,
        window_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_requests = max(1, max_requests)
        self.window = max(0.001, window_seconds)
        self._clock = clock
        self._hits: Dict[str, Deque[float]] = defaultdict(deque)
        self._rejected = 0

    def allow(self, key: str) -> bool:
        now = self._clock()
        bucket = self._hits[str(key)]
        cutoff = now - self.window
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= self.max_requests:
            self._rejected += 1
            return False
        bucket.append(now)
        return True

    def remaining(self, key: str) -> int:
        now = self._clock()
        bucket = self._hits[str(key)]
        cutoff = now - self.window
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        return max(0, self.max_requests - len(bucket))

    def reset(self, key: Optional[str] = None) -> None:
        if key is None:
            self._hits.clear()
            self._rejected = 0
        else:
            self._hits.pop(str(key), None)

    def snapshot(self) -> Dict[str, Any]:
        return {
            "max_requests": self.max_requests,
            "window_seconds": self.window,
            "active_keys": len(self._hits),
            "rejected_total": self._rejected,
        }


class GracefulShutdown:
    """在途请求排空"""

    def __init__(self, drain_timeout: float = 10.0, clock=time.monotonic) -> None:
        self.drain_timeout = max(0.0, drain_timeout)
        self._clock = clock
        self._shutting_down = False
        self._inflight = 0
        self._idle = asyncio.Event()
        self._idle.set()

    @property
    def shutting_down(self) -> bool:
        return self._shutting_down

    @property
    def inflight(self) -> int:
        return self._inflight

    def begin(self) -> None:
        self._shutting_down = True

    @asynccontextmanager
    async def inflight_request(self) -> AsyncIterator[None]:
        self._inflight += 1
        self._idle.clear()
        try:
            yield
        finally:
            self._inflight -= 1
            if self._inflight <= 0:
                self._idle.set()

    async def wait_until_drained(self, timeout: Optional[float] = None) -> bool:
        """等待在途请求结束；返回是否在超时前排空"""
        limit = self.drain_timeout if timeout is None else timeout
        if self._inflight <= 0:
            return True
        try:
            await asyncio.wait_for(self._idle.wait(), timeout=limit)
            return True
        except (asyncio.TimeoutError, TimeoutError):
            return False

    def snapshot(self) -> Dict[str, Any]:
        return {
            "shutting_down": self._shutting_down,
            "inflight_requests": self._inflight,
            "drain_timeout": self.drain_timeout,
        }
