"""存储层健壮化单测（核心模块第 2 批）。

覆盖：每会话写锁（并发 append 不丢消息）、max_messages 截断、
File 原子写与坏文件容错、Resilient 降级后按冷却期自动回归主存储。
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from xiaozhi.core.message import Message  # noqa: E402
from xiaozhi.memory.store import (  # noqa: E402
    ConversationStore,
    FileConversationStore,
    InMemoryConversationStore,
    ResilientConversationStore,
)


async def test_concurrent_appends_do_not_lose_messages():
    """同一会话并发 append：读改写整体持锁，20 条一条不丢"""
    store = InMemoryConversationStore()

    async def append_one(index: int) -> None:
        await store.append("m1", Message.user(f"消息{index}"))

    await asyncio.gather(*(append_one(i) for i in range(20)))
    saved = await store.load("m1", limit=100)
    assert len(saved) == 20


async def test_save_is_trimmed_to_max_messages():
    store = InMemoryConversationStore(max_messages=5)
    messages = [Message.user(f"第{i}条") for i in range(50)]
    await store.save("m1", messages)
    saved = await store.load("m1", limit=100)
    assert len(saved) == 5
    assert saved[-1].content == "第49条"  # 保留最新


async def test_file_store_atomic_write_leaves_no_tmp():
    store = FileConversationStore(str(ROOT / "tests" / "_tmp_file_store"))
    await store.save("会话/带@特殊字符", [Message.user("你好")])
    path = store._path("会话/带@特殊字符")
    try:
        assert path.exists()
        assert list(path.parent.glob("*.tmp")) == []  # 原子替换后无残留
        loaded = await store.load("会话/带@特殊字符")
        assert loaded[0].content == "你好"
    finally:
        await store.clear("会话/带@特殊字符")


async def test_file_store_corrupt_file_loads_empty():
    directory = ROOT / "tests" / "_tmp_file_store"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "corrupt-session.json"
    path.write_text("{这不是 JSON", encoding="utf-8")
    store = FileConversationStore(str(directory))
    loaded = await store.load("corrupt-session")
    assert loaded == []
    path.unlink()


class FlakyPrimaryStore(ConversationStore):
    """可控故障的主存储"""

    def __init__(self) -> None:
        super().__init__()
        self.fail = True
        self.calls = 0

    async def load(self, memory_id: str, limit: int = 20):
        self.calls += 1
        if self.fail:
            raise RuntimeError("primary down")
        return [Message.user("来自主存储")]

    async def _save_locked(self, memory_id: str, messages) -> None:
        if self.fail:
            raise RuntimeError("primary down")

    async def clear(self, memory_id: str) -> None:
        return None

    async def close(self) -> None:
        return None


async def test_resilient_recovers_primary_after_cooldown():
    """主存储故障 → 降级到备用；冷却期后重试主存储成功 → 结束降级"""
    primary = FlakyPrimaryStore()
    fallback = InMemoryConversationStore()
    resilient = ResilientConversationStore(
        primary, fallback, recovery_cooldown=0.01
    )

    first = await resilient.load("m1", limit=10)
    assert primary.calls == 1
    assert resilient.degraded is True

    await asyncio.sleep(0.02)
    primary.fail = False  # 主存储恢复
    second = await resilient.load("m1", limit=10)
    assert resilient.degraded is False
    assert [m.content for m in second] == ["来自主存储"]


async def test_resilient_stays_degraded_within_cooldown():
    """冷却期内不反复探测主存储（避免每次请求都撞一次故障）"""
    primary = FlakyPrimaryStore()
    resilient = ResilientConversationStore(primary, InMemoryConversationStore(), recovery_cooldown=60)

    await resilient.load("m1")
    await resilient.load("m1")
    assert primary.calls == 1  # 冷却期内只撞过一次
    assert resilient.degraded is True
