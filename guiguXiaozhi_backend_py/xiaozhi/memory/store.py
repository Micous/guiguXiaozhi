"""对话记忆存储。

抽象成接口，提供三种实现：
  * MongoConversationStore —— 生产用（与 Java 版同一个库，可同库对比）
  * FileConversationStore  —— Mongo 不可用时的降级（本地文件）
  * InMemoryConversationStore —— 单元测试用

与 LangChain4j 的 MessageWindowChatMemory 等价：
  * 存完整消息（含工具调用与工具结果）
  * 读取时只取最近 N 条（滑动窗口）
"""

from __future__ import annotations

import json
import logging
import threading
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
    """会话消息存储"""

    @abstractmethod
    async def load(self, memory_id: str, limit: int = 20) -> List[Message]:
        """读取会话最近 limit 条消息（按时间正序）"""

    @abstractmethod
    async def save(self, memory_id: str, messages: List[Message]) -> None:
        """覆盖式保存会话消息"""

    @abstractmethod
    async def append(self, memory_id: str, message: Message) -> None:
        """追加一条消息"""

    @abstractmethod
    async def clear(self, memory_id: str) -> None:
        """清空会话"""

    @abstractmethod
    async def close(self) -> None:
        """释放资源"""


class InMemoryConversationStore(ConversationStore):
    """内存实现（测试用）"""

    def __init__(self) -> None:
        self._data: Dict[str, List[Message]] = {}
        self._lock = threading.Lock()

    async def load(self, memory_id: str, limit: int = 20) -> List[Message]:
        with self._lock:
            return list(self._data.get(memory_id, []))[-limit:]

    async def save(self, memory_id: str, messages: List[Message]) -> None:
        with self._lock:
            self._data[memory_id] = list(messages)

    async def append(self, memory_id: str, message: Message) -> None:
        with self._lock:
            self._data.setdefault(memory_id, []).append(message)

    async def clear(self, memory_id: str) -> None:
        with self._lock:
            self._data.pop(memory_id, None)

    async def close(self) -> None:
        return None


class MongoConversationStore(ConversationStore):
    """MongoDB 实现（生产用，与 Java 版同库同集合）"""

    def __init__(self, uri: str, database: str = "chat_memory_db", collection: str = "chat_messages") -> None:
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
        except Exception:
            return []
        if not doc:
            return []
        try:
            raw = json.loads(doc.get("content") or "[]")
        except json.JSONDecodeError:
            return []
        return _decode_messages(raw, str(memory_id))[-limit:]

    async def save(self, memory_id: str, messages: List[Message]) -> None:
        payload = json.dumps([m.to_dict() for m in messages], ensure_ascii=False, default=str)
        try:
            self._db().update_one(
                {"memoryId": str(memory_id)},
                {"$set": {"content": payload}},
                upsert=True,
            )
        except Exception:
            pass

    async def append(self, memory_id: str, message: Message) -> None:
        await self.save(memory_id, await self.load(memory_id, limit=10_000) + [message])

    async def clear(self, memory_id: str) -> None:
        try:
            self._db().delete_one({"memoryId": str(memory_id)})
        except Exception:
            pass

    async def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


class FileConversationStore(ConversationStore):
    """文件实现（Mongo 不可用时的降级）"""

    def __init__(self, directory: str) -> None:
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

    async def save(self, memory_id: str, messages: List[Message]) -> None:
        path = self._path(memory_id)
        with self._lock:
            path.write_text(
                json.dumps([m.to_dict() for m in messages], ensure_ascii=False, default=str),
                encoding="utf-8",
            )

    async def append(self, memory_id: str, message: Message) -> None:
        await self.save(memory_id, await self.load(memory_id, limit=10_000) + [message])

    async def clear(self, memory_id: str) -> None:
        path = self._path(memory_id)
        if path.exists():
            path.unlink()

    async def close(self) -> None:
        return None


class ResilientConversationStore(ConversationStore):
    """带降级的存储：主存储失败时自动切到备用实现。

    保证"MongoDB 挂了会话仍然可用"，只是不持久化到 Mongo。
    """

    def __init__(self, primary: ConversationStore, fallback: ConversationStore) -> None:
        self.primary = primary
        self.fallback = fallback
        self.degraded = False

    async def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        if not self.degraded:
            try:
                result = getattr(self.primary, method)(*args, **kwargs)
                if hasattr(result, "__await__"):
                    result = await result
                return result
            except Exception:
                self.degraded = True
        result = getattr(self.fallback, method)(*args, **kwargs)
        if hasattr(result, "__await__"):
            result = await result
        return result

    async def load(self, memory_id: str, limit: int = 20) -> List[Message]:
        return await self._call("load", memory_id, limit)

    async def save(self, memory_id: str, messages: List[Message]) -> None:
        await self._call("save", memory_id, messages)

    async def append(self, memory_id: str, message: Message) -> None:
        await self._call("append", memory_id, message)

    async def clear(self, memory_id: str) -> None:
        await self._call("clear", memory_id)

    async def close(self) -> None:
        await self.primary.close()
        await self.fallback.close()
