"""Redis 共享限流 —— 多实例部署时额度一致。

为什么需要它：``ops.lifecycle.RateLimiter`` 是**进程内**滑动窗口，N 个实例就有
N 份额度。用户把请求打到不同实例（或网关轮询）时额度被放大成 N 倍，
「每人每分钟 30 次」在实际效果上是「每人每分钟 30×N 次」。

实现取舍（**固定窗口**而不是滑动窗口）：
  * 滑动窗口要用 ZSET（ZREMRANGEBYSCORE+ZCARD+ZADD），判断与写入之间必须
    用 Lua 保证原子；固定窗口只用 ``INCR``，**单条命令就原子**，多实例读到的
    计数严格一致，且没有"先查后写"的竞态。
  * 代价是窗口边界效应：跨越窗口边界时，用户可能在极短时间窗口内用掉
    2× 额度。对"防刷"用途可接受；若将来需要严格滑动语义，再换 Lua 脚本，
    ``allow()`` 的契约不变。

fail-closed 与否（``fail_open``）：
  * ``False``（默认）：Redis 运行期故障 → 抛 ``RateLimitBackendError``，
    由路由返回 **503**（不是 429）。理由是限流是保护性组件，不知道真实
    用量时宁可暂时拒服务，也不要假设"没人超限"。
  * ``True``：故障时放行，只记告警与计数。可用性优先，**不建议**用于生产
    （等于给滥用者一个"打挂 Redis 就无限速"的开关）。
  注意：启动期探活失败**一律拒绝启动**，与 ``fail_open`` 无关 —— 配置错误
  不该拖到运行期才暴露。
"""

from __future__ import annotations

import logging
import math
import time
from typing import Any, Callable, Dict, Optional

from xiaozhi.ops.bounded_io import get_bounded_io
from xiaozhi.ops.lifecycle import RateLimiter

logger = logging.getLogger(__name__)


class RateLimitBackendError(RuntimeError):
    """限流后端不可用（区别于"用户超限"：前者是 503，后者是 429）。"""


class RedisRateLimiter:
    """基于 Redis 计数的固定窗口限流器（接口与 ``RateLimiter`` 对齐）"""

    def __init__(
        self,
        url: str = "redis://localhost:6379/0",
        max_requests: int = 30,
        window_seconds: float = 60.0,
        prefix: str = "xiaozhi:rl",
        *,
        client: Any = None,
        clock: Callable[[], float] = time.time,
        fail_open: bool = False,
        socket_timeout: float = 0.5,
    ) -> None:
        self.url = url
        self.max_requests = max(1, max_requests)
        self.window = max(0.001, window_seconds)
        self.prefix = prefix
        self._clock = clock
        self.fail_open = fail_open
        self._client = client
        self._socket_timeout = socket_timeout
        self._rejected = 0
        self._backend_errors = 0

    # ---------- 连接 ----------

    @property
    def client(self):
        if self._client is None:
            try:
                import redis  # 延迟导入：离线单测不装 redis 也能跑（注入 client）
            except ImportError as exc:  # pragma: no cover - 依赖缺失时的显式报错
                raise RateLimitBackendError(
                    "rate_limit_backend=redis 但未安装 redis 客户端：pip install redis"
                ) from exc
            self._client = redis.Redis.from_url(
                self.url,
                socket_timeout=self._socket_timeout,
                socket_connect_timeout=self._socket_timeout,
                decode_responses=True,
            )
        return self._client

    def probe(self) -> None:
        """启动期探活。失败直接抛错（组合根据此拒绝启动）。"""
        try:
            self.client.ping()
        except RateLimitBackendError:
            raise
        except Exception as exc:
            raise RateLimitBackendError(
                f"Redis 限流后端不可达（{self.url}）：{type(exc).__name__}: {exc}"
            ) from exc

    # ---------- 计数 ----------

    def _key(self, key: str) -> str:
        bucket = int(self._clock() // self.window)
        return f"{self.prefix}:{key}:{bucket}"

    def _ttl_seconds(self) -> int:
        # 固定窗口只需活到本窗口结束；多留一个窗口避免边界抖动造成的提前清零
        return max(1, int(math.ceil(self.window * 2)))

    def _incr(self, redis_key: str) -> int:
        count = int(self.client.incr(redis_key))
        if count == 1:
            self.client.expire(redis_key, self._ttl_seconds())
        else:
            # 自愈：进程在 INCR 与 EXPIRE 之间崩溃会留下永不过期的 key，
            # 那条 key 的计数会永久累积 → 该用户被永久限流。每次顺手检查 TTL。
            try:
                if int(self.client.ttl(redis_key)) < 0:
                    self.client.expire(redis_key, self._ttl_seconds())
            except Exception:  # TTL 查询失败不影响本次判定
                logger.debug("限流 TTL 查询失败，跳过自愈", exc_info=True)
        return count

    def allow(self, key: str) -> bool:
        redis_key = self._key(key)
        try:
            count = self._incr(redis_key)
        except Exception as exc:
            self._backend_errors += 1
            logger.error(
                "限流后端故障：%s: %s（fail_open=%s）", type(exc).__name__, exc, self.fail_open
            )
            if self.fail_open:
                return True
            raise RateLimitBackendError(
                f"限流后端不可用：{type(exc).__name__}: {exc}"
            ) from exc
        if count > self.max_requests:
            self._rejected += 1
            return False
        return True

    async def allow_async(self, key: str) -> bool:
        """异步入口：Redis 调用是阻塞 IO，放到线程池里，避免卡住事件循环。"""
        return await get_bounded_io().run(self.allow, key)

    def remaining(self, key: str) -> int:
        try:
            used = int(self.client.get(self._key(key)) or 0)
        except Exception as exc:
            raise RateLimitBackendError(
                f"限流后端不可用：{type(exc).__name__}: {exc}"
            ) from exc
        return max(0, self.max_requests - used)

    def reset(self, key: Optional[str] = None) -> None:
        """重置计数。``key=None`` 时只清空当前窗口内已知 key（按前缀扫描）。"""
        try:
            if key is None:
                for found in self.client.scan_iter(match=f"{self.prefix}:*"):
                    self.client.delete(found)
                self._rejected = 0
                return
            self.client.delete(self._key(key))
        except Exception as exc:
            raise RateLimitBackendError(
                f"限流后端不可用：{type(exc).__name__}: {exc}"
            ) from exc

    def snapshot(self) -> Dict[str, Any]:
        return {
            "backend": "redis",
            "url": self.url,
            "max_requests": self.max_requests,
            "window_seconds": self.window,
            "rejected_total": self._rejected,
            "backend_errors_total": self._backend_errors,
            "fail_open": self.fail_open,
        }


def build_rate_limiter(settings) -> Any:
    """按配置装配限流器（组合根唯一装配点）。

    redis 模式下先探活：连不上直接抛 ``RateLimitBackendError``，
    由组合根转成启动失败 —— 不允许"配了 Redis 却静默用内存"，
    那会让"多实例额度一致"这个前提在无人察觉的情况下失效。
    """
    backend = (settings.rate_limit_backend or "memory").strip().lower()
    if backend == "redis":
        limiter = RedisRateLimiter(
            url=settings.redis_url,
            max_requests=settings.rate_limit_max_requests,
            window_seconds=settings.rate_limit_window_seconds,
            prefix=settings.rate_limit_redis_prefix,
        )
        limiter.probe()
        logger.info(
            "限流后端：Redis 共享计数（%s，%d 次/%.0fs）",
            settings.redis_url,
            limiter.max_requests,
            limiter.window,
        )
        return limiter
    if backend != "memory":
        raise ValueError(
            f"rate_limit_backend 只能是 'memory' 或 'redis'，收到：{settings.rate_limit_backend!r}"
        )
    return RateLimiter(
        settings.rate_limit_max_requests, settings.rate_limit_window_seconds
    )
