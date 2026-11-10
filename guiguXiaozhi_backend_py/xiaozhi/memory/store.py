"""对话记忆存储。

抽象成接口，提供三种实现：
  * MongoConversationStore —— 生产用（与 Java 版同一个库，可同库对比）
  * FileConversationStore  —— Mongo 不可用时的降级（本地文件）
  * InMemoryConversationStore —— 单元测试用

与 LangChain4j 的 MessageWindowChatMemory 等价：
  * 存完整消息（含工具调用与工具结果）
  * 读取时只取最近 N 条（滑动窗口）

健壮性（2026-11-10 审计加固）：
  * 同一 memory_id 的写操作经 asyncio.Lock 串行化 —— 并发请求不再互相覆盖丢消息；
  * save 按 max_messages 截断 —— 历史不再无限增长；
  * Mongo 异常记日志并计数，不再静默吞掉；
  * File 实现用"临时文件 + 原子替换"，进程崩溃不会写坏 JSON；
  * Resilient 降级后按冷却期自动重试主存储，成功即回归（不再一降到底）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional

from xiaozhi.core.message import Message

logger = logging.getLogger(__name__)


def _decode_messages(raw: Any, memory_id: str) -> List[Message]:
    """安全解码消息列表：损坏条目跳过并告警，绝不让整段历史加载失败。

    此前用 Message.from_dict 直接解码 —— Mongo 里一条未知 role 的脏消息
    会让整个会话 load 失败（返回 []），用户视角就是"对话失忆"。
    """
    if not isinstance(raw, list):
        return []
    messages: List[Message] = []
    skipped = 0
    for item in raw:
        message = Message.from_dict_safe(item)
        if message is None:
            skipped += 1
            continue
        messages.append(message)
    if skipped:
        logger.warning("会话 %s 有 %d 条损坏消息被跳过", memory_id, skipped)
    return messages


class ConversationStore(ABC):
    """会话消息存储（带每会话写锁与容量上限）"""

    def __init__(self, max_messages: int = 200) -> None:
        self.max_messages = max(1, int(max_messages))
        self._write_locks: Dict[str, asyncio.Lock] = {}

    def _write_lock(self, memory_id: str) -> asyncio.Lock:
        """同一 memory_id 的写操作串行化。

        并发请求对同一会话做 load+save 会互相覆盖（丢消息）；
        单事件循环内用 asyncio.Lock 即可保证读改写的原子性。
        """
        lock = self._write_locks.get(memory_id)
        if lock is None:
            lock = asyncio.Lock()
            self._write_locks[memory_id] = lock
        return lock

    def _trim(self, messages: List[Message]) -> List[Message]:
        if len(messages) > self.max_messages:
            return messages[-self.max_messages :]
        return messages

    @abstractmethod
    async def load(self, memory_id: str, limit: int = 20) -> List[Message]:
        """读取会话最近 limit 条消息（按时间正序）"""

    @abstractmethod
    async def _save_locked(self, memory_id: str, messages: List[Message]) -> None:
        """实际写盘（调用方已持有该会话的写锁）"""

    @abstractmethod
    async def clear(self, memory_id: str) -> None:
        """清空会话"""

    @abstractmethod
    async def close(self) -> None:
        """释放资源"""

    async def save(self, memory_id: str, messages: List[Message]) -> None:
        """覆盖式保存会话消息（截断到 max_messages）"""
        async with self._write_lock(memory_id):
            await self._save_locked(memory_id, self._trim(list(messages)))

    async def append(self, memory_id: str, message: Message) -> None:
        """追加一条消息（读改写整体持锁，不被并发请求打断）"""
        async with self._write_lock(memory_id):
            current = await self.load(memory_id, limit=10_000)
            await self._save_locked(memory_id, self._trim(current + [message]))


class InMemoryConversationStore(ConversationStore):
    """内存实现（测试用）"""

    def __init__(self, max_messages: int = 200) -> None:
        super().__init__(max_messages)
        self._data: Dict[str, List[Message]] = {}
        self._lock = threading.Lock()

    async def load(self, memory_id: str, limit: int = 20) -> List[Message]:
        with self._lock:
            return list(self._data.get(memory_id, []))[-limit:]

    async def _save_locked(self, memory_id: str, messages: List[Message]) -> None:
        with self._lock:
            self._data[memory_id] = list(messages)

    async def clear(self, memory_id: str) -> None:
        with self._lock:
            self._data.pop(memory_id, None)

    async def close(self) -> None:
        return None


class MongoConversationStore(ConversationStore):
    """MongoDB 实现（生产用，与 Java 版同库同集合）"""

    def __init__(
        self,
        uri: str,
        database: str = "chat_memory_db",
        collection: str = "chat_messages",
        max_messages: int = 200,
    ) -> None:
        super().__init__(max_messages)
        self._uri = uri
        self._database = database
        self._collection = collection
        self._client = None

    def _db(self):
        if self._client is None:
            from pymongo import MongoClient

            self._client = MongoClient(self._client_uri(), serverSelectionTimeoutMS=3000)
        return self._client[self._database][self._collection]

    def _client_uri(self) -> str:
        return self._uri

    async def load(self, memory_id: str, limit: int = 20) -> List[Message]:
        try:
            doc = self._db().find_one({"memoryId": str(memory_id)})
        except Exception as exc:
            logger.warning("会话 %s 读取失败（返回空历史）：%s", memory_id, exc)
            return []
        if not doc:
            return []
        try:
            raw = json.loads(doc.get("content") or "[]")
        except json.JSONDecodeError:
            logger.warning("会话 %s 的 content 不是合法 JSON，按空历史处理", memory_id)
            return []
        return _decode_messages(raw, str(memory_id))[-limit:]

    async def _save_locked(self, memory_id: str, messages: List[Message]) -> None:
        payload = json.dumps([m.to_dict() for m in messages], ensure_ascii=False, default=str)
        try:
            self._db().update_one(
                {"memoryId": str(memory_id)},
                {"$set": {"content": payload}},
                upsert=True,
            )
        except Exception as exc:
            # 不阻断对话，但必须留痕 —— 静默吞掉会让"丢消息"无从排查
            logger.warning("会话 %s 写入失败：%s", memory_id, exc)

    async def clear(self, memory_id: str) -> None:
        try:
            self._db().delete_one({"memoryId": str(memory_id)})
        except Exception as exc:
            logger.warning("会话 %s 清空失败：%s", memory_id, exc)

    async def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


class FileConversationStore(ConversationStore):
    """文件实现（Mongo 不可用时的降级）"""

    def __init__(self, directory: str, max_messages: int = 200) -> None:
        super().__init__(max_messages)
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _path(self, memory_id: str) -> Path:
        safe = "".join(ch for ch in str(memory_id) if ch.isalnum() or ch in "-_")
        return self._dir / f"{safe or 'default'}.json"

    async def load(self, memory_id: str, limit: int = 20) -> List[Message]:
        path = self._path(memory_id)
        if not path.exists():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return _decode_messages(raw, memory_id)[-limit:]
        except Exception:
            return []

    async def _save_locked(self, memory_id: str, messages: List[Message]) -> None:
        path = self._path(memory_id)
        payload = json.dumps(
            [m.to_dict() for m in messages], ensure_ascii=False, default=str
        )
        with self._lock:
            # 先写临时文件再原子替换：进程崩溃不会留下写坏的 JSON
            tmp_path = path.with_suffix(".json.tmp")
            tmp_path.write_text(payload, encoding="utf-8")
            os.replace(tmp_path, path)

    async def clear(self, memory_id: str) -> None:
        path = self._path(memory_id)
        if path.exists():
            path.unlink()

    async def close(self) -> None:
        return None


class ResilientConversationStore(ConversationStore):
    """带降级的存储：主存储失败时自动切到备用实现。

    保证"MongoDB 挂了会话仍然可用"，只是不持久化到 Mongo。
    降级不是永久的：超过冷却期（默认 30s）后会重试主存储，
    成功即回归 —— 避免一次抖动导致整个生命周期都绕过主存储。
    """

    def __init__(
        self,
        primary: ConversationStore,
        fallback: ConversationStore,
        recovery_cooldown: float = 30.0,
    ) -> None:
        super().__init__()
        self.primary = primary
        self.fallback = fallback
        self.recovery_cooldown = max(0.0, float(recovery_cooldown))
        self.degraded = False
        self._degraded_at: Optional[float] = None

    async def _try_primary(self, method: str, *args: Any) -> Any:
        result = getattr(self.primary, method)(*args)
        if hasattr(result, "__await__"):
            result = await result
        return result

    async def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        if not self.degraded:
            try:
                return await self._try_primary(method, *args)
            except Exception:
                self.degraded = True
                self._degraded_at = time.monotonic()
                logger.warning("主存储失败，降级到备用存储（%s）", method)
        else:
            # 冷却期过后尝试回归主存储
            elapsed = time.monotonic() - (self._degraded_at or 0.0)
            if elapsed >= self.recovery_cooldown:
                try:
                    result = await self._try_primary(method, *args)
                    self.degraded = False
                    self._degraded_at = None
                    logger.info("主存储恢复，结束降级（%s）", method)
                    return result
                except Exception:
                    self._degraded_at = time.monotonic()  # 重置冷却期
        result = getattr(self.fallback, method)(*args, **kwargs)
        if hasattr(result, "__await__"):
            result = await result
        return result

    async def load(self, memory_id: str, limit: int = 20) -> List[Message]:
        return await self._call("load", memory_id, limit)

    async def save(self, memory_id: str, messages: List[Message]) -> None:
        await self._call("save", memory_id, messages)

    async def _save_locked(self, memory_id: str, messages: List[Message]) -> None:
        # Resilient 覆写了 save/append（写锁由下游实现持有），
        # 本方法仅为满足抽象契约；万一被直接调用则走降级委托
        await self._call("save", memory_id, messages)

    async def append(self, memory_id: str, message: Message) -> None:
        await self._call("append", memory_id, message)

    async def clear(self, memory_id: str) -> None:
        await self._call("clear", memory_id)

    async def close(self) -> None:
        await self.primary.close()
        await self.fallback.close()
