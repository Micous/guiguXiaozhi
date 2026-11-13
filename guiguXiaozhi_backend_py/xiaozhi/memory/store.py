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

Phase 5（2026-11-11 审计 P1-01 加固）：
  * ``strict=True``：读写失败抛 ``RepositoryUnavailable``，不再把存储故障
    伪装成"新会话/空历史"；production 组合根必须用 strict 且不包 Resilient
    （Mongo 挂了 → 聊天 503 快速失败，绝不静默降级到本地文件）；
  * Mongo 文档带 ``version`` 字段做 CAS 条件更新 —— 跨实例并发保存同一
    会话时，过期写入不会直接覆盖：先重读合并（按消息签名去重）再重试，
    彻底失败才报 ``ConcurrentWriteConflict``（非 strict 则留痕继续）；
  * ``ping()``：有界存活探测（chat 前置探活 → 503、/ready 探针复用）。
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

from xiaozhi.ops.bounded_io import get_bounded_io

from xiaozhi.core.message import Message
from xiaozhi.repositories.errors import (
    PersistenceError,
    RepositoryError,
    RepositoryUnavailable,
)

logger = logging.getLogger(__name__)


class ConcurrentWriteConflict(RepositoryError):
    """会话 CAS 并发写保护重试耗尽 —— 过期写入被拒绝且合并失败。

    调用方应重试整个请求（下一轮 load 会拿到最新版本再改写）；
    绝不能用旧快照继续覆盖（那会丢掉别的实例刚写的消息）。
    """

    def __init__(self, message: str = "会话并发写冲突，请重试") -> None:
        super().__init__("concurrent_write_conflict", message)


def _message_signature(message: Message) -> str:
    """消息语义签名 —— 合并两份历史时按内容判等。

    **只用语义字段**，不能用 ``to_dict()`` 全量：后者含 ``message_id`` 与
    ``timestamp``（每条消息都不同），两条内容相同的历史消息签名会不相等
    → 去重失效 → 合并后出现重复消息（实测踩过）。
    """
    return json.dumps(
        {
            "role": message.role.value,
            "content": message.content,
            "tool_call_id": message.tool_call_id,
            "tool_name": message.tool_name,
            "tool_calls": [
                {"id": c.id, "name": c.name, "arguments": c.arguments}
                for c in message.tool_calls
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )


def _merge_messages(ours: List[Message], theirs: List[Message]) -> List[Message]:
    """合并两份会话历史：theirs（库里最新）为基底，ours 里库里没有的消息追加在后。

    顺序语义：共同前缀保持库里顺序；本实例新写的消息（对方实例还没看到的）
    排在末尾 —— 与"后写者追加"的直觉一致，避免消息乱序。
    """
    seen = {_message_signature(m) for m in theirs}
    merged = list(theirs)
    for message in ours:
        if _message_signature(message) not in seen:
            merged.append(message)
            seen.add(_message_signature(message))
    return merged


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
    """MongoDB 实现（生产用，与 Java 版同库同集合）。

    strict 模式（Phase 5 / P1-01）：
        读写失败抛 ``RepositoryUnavailable`` —— 存储故障绝不能伪装成
        "新会话/空历史"。production 组合根必须 strict 且**不包** Resilient
        降级（本地文件不是多副本共享会话的合法故障转移目标）。

    并发写保护（CAS）：
        文档带 ``version`` 字段，save 用条件更新（匹配版本才写）。
        版本不匹配 = 另一个实例刚写过 → 重读它的内容、与本实例合并
        （按消息签名去重），再以新版本重试，最多 3 次；耗尽后
        strict 抛 ``ConcurrentWriteConflict``，非 strict 留痕放弃本次写入
        （放弃而不是覆盖 —— 覆盖会丢对方刚写的消息）。
    """

    _CAS_MAX_ATTEMPTS = 3

    def __init__(
        self,
        uri: str,
        database: str = "chat_memory_db",
        collection: str = "chat_messages",
        max_messages: int = 200,
        *,
        strict: bool = False,
    ) -> None:
        super().__init__(max_messages)
        self._uri = uri
        self._database = database
        self._collection = collection
        self._strict = bool(strict)
        self._client = None
        # 本实例已知的文档版本（CAS 期望值）；None = 未读过，写前先查
        self._versions: Dict[str, int] = {}

    def _db(self):
        if self._client is None:
            from pymongo import MongoClient

            self._client = MongoClient(self._client_uri(), serverSelectionTimeoutMS=3000)
        return self._client[self._database][self._collection]

    def _client_uri(self) -> str:
        return self._uri

    def _fail(self, action: str, memory_id: str, exc: Exception) -> Exception:
        """把底层异常翻译成分类异常（strict 抛出；非 strict 仅留痕）。"""
        wrapped = RepositoryUnavailable(f"会话存储不可用（{action}）: {exc}")
        if self._strict:
            return wrapped
        logger.warning("会话 %s %s失败：%s", memory_id, action, exc)
        return None

    async def ping(self) -> None:
        """有界存活探测（serverSelectionTimeoutMS 已限 3s）。失败抛分类异常。"""
        try:
            await get_bounded_io().run(
                self._db().database.client.admin.command, "ping"
            )
        except Exception as exc:
            raise RepositoryUnavailable(f"会话存储不可用（ping）: {exc}") from exc

    async def load(self, memory_id: str, limit: int = 20) -> List[Message]:
        try:
            doc = await get_bounded_io().run(
                self._db().find_one, {"memoryId": str(memory_id)}
            )
        except Exception as exc:
            failure = self._fail("读取", memory_id, exc)
            if failure is not None:
                raise failure from exc
            return []
        if not doc:
            return []
        try:
            raw = json.loads(doc.get("content") or "[]")
        except json.JSONDecodeError:
            logger.warning("会话 %s 的 content 不是合法 JSON，按空历史处理", memory_id)
            return []
        version = int(doc.get("version") or 0)
        self._versions[str(memory_id)] = version
        return _decode_messages(raw, str(memory_id))[-limit:]

    async def _save_locked(self, memory_id: str, messages: List[Message]) -> None:
        payload = json.dumps([m.to_dict() for m in messages], ensure_ascii=False, default=str)
        key = str(memory_id)
        try:
            await get_bounded_io().run(
                self._save_with_cas, key, payload, messages
            )
        except ConcurrentWriteConflict as exc:
            if self._strict:
                raise
            # 非 strict：留痕即可（旧行为兼容），但绝不能悄悄用旧快照覆盖
            logger.warning("会话 %s 并发写冲突放弃本次写入：%s", key, exc)
        except Exception as exc:
            failure = self._fail("写入", memory_id, exc)
            if failure is not None:
                raise failure from exc
            logger.warning("会话 %s 写入失败：%s", key, exc)

    def _save_with_cas(self, key: str, payload: str, messages: List[Message]) -> None:
        """条件更新 + 冲突合并重试（同步，调用方在线程里跑）。"""
        collection = self._db()
        for _attempt in range(self._CAS_MAX_ATTEMPTS):
            expected = self._versions.get(key)
            if expected is None:
                # 本实例没读过 → 先摸一次当前版本（不存在按 0 处理）
                doc = collection.find_one({"memoryId": key}, {"version": 1})
                expected = int((doc or {}).get("version") or 0)
                self._versions[key] = expected
            result = collection.update_one(
                {"memoryId": key, "version": expected},
                {"$set": {"content": payload}, "$inc": {"version": 1}},
            )
            if result.matched_count == 1:
                self._versions[key] = expected + 1
                return
            # 版本不匹配：另一实例刚写过 → 重读合并后再试
            doc = collection.find_one({"memoryId": key})
            if not doc:
                # 文档刚被删除 → 退化为首写（版本从 1 起）
                insert = collection.update_one(
                    {"memoryId": key},
                    {"$set": {"content": payload}, "$setOnInsert": {"version": 1}},
                    upsert=True,
                )
                if insert.matched_count == 1 or insert.upserted_id is not None:
                    self._versions[key] = 1
                    return
                continue
            try:
                their_raw = json.loads(doc.get("content") or "[]")
            except json.JSONDecodeError:
                their_raw = []
            their_messages = [
                m for m in (Message.from_dict_safe(item) for item in their_raw) if m
            ]
            merged = _merge_messages(messages, their_messages)
            merged = self._trim(merged)
            payload = json.dumps(
                [m.to_dict() for m in merged], ensure_ascii=False, default=str
            )
            self._versions[key] = int(doc.get("version") or 0)
        raise ConcurrentWriteConflict(
            f"会话 {key} 并发写冲突（重试 {self._CAS_MAX_ATTEMPTS} 次耗尽）"
        )

    async def clear(self, memory_id: str) -> None:
        try:
            await get_bounded_io().run(
                self._db().delete_one, {"memoryId": str(memory_id)}
            )
            self._versions.pop(str(memory_id), None)
        except Exception as exc:
            failure = self._fail("清空", memory_id, exc)
            if failure is not None:
                raise failure from exc
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
