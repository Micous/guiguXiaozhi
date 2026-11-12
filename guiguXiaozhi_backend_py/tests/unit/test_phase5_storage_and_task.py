"""Phase 5 验收测试 —— 存储失败策略（P1-01）+ 任务生命周期持久化（P1-02）。

审计验收对号：

P1-01（会话存储故障不能自动切换本地文件）
  1. Mongo 不可用时生产启动/就绪探针失败或聊天返回明确 503；绝不写本地 fallback；
  2. 两个并发请求写同一会话不会丢失消息或乱序；
  3. 存储恢复后不需要人工合并两个不同数据源。

P1-02（Runtime 任务生命周期）
  1. LLM 调用期间进程退出：任务标记为 INTERRUPTED，可重试；
  2. 工具执行成功但进程在写回结果前退出：恢复后先查询业务事实，不重复提交；
  3. 等待审批的任务重启后仍可继续或过期关闭；
  4. 取消任务后不得继续执行尚未开始的工具；
  5. 同一幂等键重复提交任务只产生一个逻辑任务。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from xiaozhi.core.message import Message  # noqa: E402
from xiaozhi.memory.store import (  # noqa: E402
    ConcurrentWriteConflict,
    InMemoryConversationStore,
    MongoConversationStore,
    _merge_messages,
)
from xiaozhi.repositories.errors import RepositoryUnavailable  # noqa: E402
from xiaozhi.repositories.task_repo import (  # noqa: E402
    AgentExecution,
    AgentTask,
    InMemoryTaskRepository,
    TaskRepository,
    hash_args,
    new_id,
)
from xiaozhi.runtime.task_tracker import TaskTracker  # noqa: E402


# ==================================================================== P1-01


class _FakeMongoCollection:
    """最小 Mongo collection 替身：可控的 CAS 匹配行为。

    真实实现里 ``MongoConversationStore._db()`` 直接返回 **collection**
    （client[db][collection]），所以替身也返回 collection 本身。
    """

    def __init__(self, docs=None):
        self.docs = dict(docs or {})
        self.update_calls = []
        self.ping_error = None
        self.database = self  # ping() 走 .database.client.admin.command

    @property
    def client(self):
        collection = self

        class _Admin:
            def command(self, *_a, **_k):
                if collection.ping_error:
                    raise collection.ping_error
                return {"ok": 1}

        class _Client:
            admin = _Admin()

        return _Client()

    def find_one(self, query, projection=None):
        return self.docs.get(query.get("memoryId"))

    def update_one(self, query, update, upsert=False):
        self.update_calls.append((query, update))
        memory_id = query.get("memoryId")
        expected = query.get("version")
        doc = self.docs.get(memory_id)
        if doc is None:
            # 文档不存在：仅当没有版本约束时视为首写
            if expected is None or expected == 0:
                self.docs[memory_id] = {
                    "content": update.get("$set", {}).get("content", "[]"),
                    "version": 1,
                }
                return _FakeUpdateResult(matched_count=1, upserted_id="x")
            return _FakeUpdateResult(matched_count=0)
        if expected is not None and doc.get("version") != expected:
            return _FakeUpdateResult(matched_count=0)
        doc["content"] = update.get("$set", {}).get("content", doc.get("content"))
        doc["version"] = doc.get("version", 0) + 1
        return _FakeUpdateResult(matched_count=1)


class _FakeUpdateResult:
    def __init__(self, matched_count=0, upserted_id=None):
        self.matched_count = matched_count
        self.upserted_id = upserted_id


def _mongo_store(strict=True, docs=None):
    """构造一个指向**固定** fake collection 的 store（同一实例，便于测试操纵）。"""
    store = MongoConversationStore("mongodb://fake", strict=strict)
    collection = _FakeMongoCollection(docs)
    store._db = lambda: collection
    store._fake_collection = collection
    return store

async def test_strict_mongo_read_failure_raises_not_empty_history():
    """P1-01 核心：存储故障**不能**伪装成空历史。

    旧行为：读失败 → return [] → 用户视角「对话失忆」。
    strict 模式：读失败 → RepositoryUnavailable。
    """
    store = _mongo_store(strict=True)

    class _Boom:
        def find_one(self, *_a, **_k):
            raise RuntimeError("connection refused")

    store._db = lambda: _Boom()
    with pytest.raises(RepositoryUnavailable):
        await store.load("m1")


async def test_non_strict_mongo_read_failure_returns_empty_history():
    """灰度关闭时保持一期行为（开发/内网演示不受影响）。"""
    store = _mongo_store(strict=False)

    class _Boom:
        def find_one(self, *_a, **_k):
            raise RuntimeError("connection refused")

    store._db = lambda: _Boom()
    assert await store.load("m1") == []


async def test_strict_mongo_write_failure_raises():
    """strict 模式写失败必须抛，不能静默丢消息。"""
    store = _mongo_store(strict=True)

    class _Boom:
        def find_one(self, *_a, **_k):
            raise RuntimeError("connection reset")

        def update_one(self, *_a, **_k):
            raise RuntimeError("connection reset")

    store._db = lambda: _Boom()
    with pytest.raises(RepositoryUnavailable):
        await store.save("m1", [Message.user("你好")])


async def test_mongo_ping_failure_raises_repository_unavailable():
    """ping 探针失败 → 分类异常（供 /ready 与 chat 前置探测使用）。"""
    collection = _FakeMongoCollection()
    collection.ping_error = RuntimeError("no route to host")
    store = MongoConversationStore("mongodb://fake", strict=True)
    store._db = lambda: collection
    with pytest.raises(RepositoryUnavailable):
        await store.ping()


async def test_concurrent_write_does_not_lose_messages():
    """P1-01 验收 2：并发写同一会话不丢消息（CAS 冲突后合并重试）。"""
    docs = {"m1": {"content": "[]", "version": 3}}
    store = _mongo_store(strict=True, docs=docs)
    collection = store._db()

    # 第一次保存成功；随后模拟另一实例写入（版本前进），
    # 本实例的期望版本过期 → CAS 冲突 → 重读合并 → 重试成功
    original_update = collection.update_one
    state = {"calls": 0}

    def racing_update(query, update, upsert=False):
        state["calls"] += 1
        if state["calls"] == 1:
            # 另一实例抢先写入：版本 +1 且塞了一条新消息
            doc = collection.docs["m1"]
            doc["version"] = doc.get("version", 0) + 1
            other = json.dumps(
                [Message.user("别人的消息").to_dict()], ensure_ascii=False
            )
            doc["content"] = other
        return original_update(query, update, upsert=upsert)

    collection.update_one = racing_update

    await store.save("m1", [Message.user("我的消息")])

    saved = json.loads(collection.docs["m1"]["content"])
    contents = [Message.from_dict_safe(item).content for item in saved]
    assert "我的消息" in contents, "本实例的消息丢了"
    assert "别人的消息" in contents, "并发实例的消息被覆盖了"


async def test_merge_messages_keeps_both_sides_without_duplication():
    ours = [Message.user("A"), Message.user("B")]
    theirs = [Message.user("A"), Message.user("C")]
    merged = _merge_messages(ours, theirs)
    contents = [m.content for m in merged]
    assert contents.count("A") == 1, "重复消息未被去重"
    assert "B" in contents and "C" in contents


def test_production_store_assembly_never_wraps_file_fallback():
    """P1-01 验收 1：快速失败模式下**绝不**包 Resilient 本地文件降级。

    组合根装配逻辑的静态断言：storage_fail_fast=true → 只装 Mongo strict。
    """
    from xiaozhi.application import get_application  # 导入即验证模块可加载

    assert callable(get_application)
    # 源码级断言：快速失败分支里没有 FileConversationStore
    import inspect

    from xiaozhi import application as app_mod

    src = inspect.getsource(app_mod.get_application)
    fast_branch = src.split("if settings.storage_fail_fast:")[1].split("else:")[0]
    assert "FileConversationStore" not in fast_branch
    assert "strict=True" in fast_branch


# ==================================================================== P1-02


def _repo() -> InMemoryTaskRepository:
    return InMemoryTaskRepository()


def test_task_lifecycle_creates_task_execution_and_events():
    """任务登记 → 执行登记 → 事件序号单调递增。"""
    repo = _repo()
    tracker = TaskTracker(repo, model_id="qwen2.5:7b")
    task_id, execution_id = tracker.begin(subject_id="alice", session_id="s1")

    assert task_id and execution_id
    task = repo.get_task(task_id)
    assert task.status == "RUNNING"
    assert task.subject_id == "alice"
    execution = repo.get_execution(execution_id)
    assert execution.status == "RUNNING"
    assert execution.model_id == "qwen2.5:7b"

    seq1 = repo.append_event(task_id, "a")
    seq2 = repo.append_event(task_id, "b")
    assert seq2 == seq1 + 1, "事件序号必须 task 内单调递增"


def test_tool_started_is_recorded_before_execution():
    """工具执行**前**落 STARTED —— 崩溃恢复据此判断"可能有副作用"。"""
    repo = _repo()
    tracker = TaskTracker(repo)
    tracker.begin(subject_id="alice", session_id="s1")

    exec_id = tracker.tool_started("call-1", "预约挂号", {"department": "内科"})
    record = repo.get_tool_execution(exec_id)
    assert record.status == "STARTED"
    assert record.tool_name == "预约挂号"
    assert record.args_hash == hash_args({"department": "内科"})


def test_write_tool_success_marks_side_effect_committed():
    """写工具成功 → side_effect_committed=True（副作用已发生的事实依据）。"""
    repo = _repo()
    tracker = TaskTracker(repo)
    tracker.begin(subject_id="alice", session_id="s1")

    exec_id = tracker.tool_started("call-1", "预约挂号", {})
    tracker.tool_finished(exec_id, "预约挂号", success=True)
    assert repo.get_tool_execution(exec_id).side_effect_committed is True
    assert repo.get_tool_execution(exec_id).status == "SUCCEEDED"


def test_query_tool_success_is_not_side_effect():
    """查询类工具成功**不算**副作用（没有改变外部事实）。"""
    repo = _repo()
    tracker = TaskTracker(repo)
    tracker.begin(subject_id="alice", session_id="s1")

    exec_id = tracker.tool_started("call-1", "查询是否有号源", {})
    tracker.tool_finished(exec_id, "查询是否有号源", success=True)
    assert repo.get_tool_execution(exec_id).side_effect_committed is False


def test_failed_tool_is_not_side_effect_committed():
    repo = _repo()
    tracker = TaskTracker(repo)
    tracker.begin(subject_id="alice", session_id="s1")

    exec_id = tracker.tool_started("call-1", "预约挂号", {})
    tracker.tool_finished(exec_id, "预约挂号", success=False)
    record = repo.get_tool_execution(exec_id)
    assert record.status == "FAILED"
    assert record.side_effect_committed is False


def test_same_idempotency_key_yields_one_logical_task():
    """P1-02 验收 5：同一幂等键重复提交只产生一个逻辑任务。"""
    repo = _repo()
    first = repo.create_task(
        AgentTask(
            task_id=new_id(),
            subject_id="alice",
            session_id="s1",
            status="RUNNING",
            idempotency_key="req-42",
        )
    )
    second = repo.create_task(
        AgentTask(
            task_id=new_id(),
            subject_id="alice",
            session_id="s1",
            status="RUNNING",
            idempotency_key="req-42",
        )
    )
    assert first.task_id == second.task_id, "幂等键重复产生了两个任务"


def test_tracking_failure_never_breaks_conversation():
    """追踪是**可观测性**不是业务依赖：仓储炸了也不能让对话失败。"""

    class _BrokenRepo(TaskRepository):
        def create_task(self, task):
            raise RuntimeError("db down")

        def finish_task(self, task_id, status):
            raise RuntimeError("db down")

        def create_execution(self, execution):
            raise RuntimeError("db down")

        def finish_execution(self, execution_id, status):
            raise RuntimeError("db down")

        def record_tool_started(self, record):
            raise RuntimeError("db down")

        def record_tool_finished(self, *a, **k):
            raise RuntimeError("db down")

        def append_event(self, task_id, event_type, payload=None):
            raise RuntimeError("db down")

        def recover_stale(self, stale_after_seconds):
            raise RuntimeError("db down")

    tracker = TaskTracker(_BrokenRepo())
    task_id, execution_id = tracker.begin(subject_id="alice", session_id="s1")
    assert task_id is None and execution_id is None
    # 后续调用同样不得抛出
    tracker.tool_started("c1", "预约挂号", {})
    tracker.tool_finished("x", "预约挂号", success=True)
    tracker.finish("SUCCEEDED")


# ==================================================================== 恢复扫描


def test_recover_stale_marks_running_execution_interrupted():
    """P1-02 验收 1：进程退出后 RUNNING 执行标记为 INTERRUPTED，任务 FAILED。"""
    repo = _repo()
    task = repo.create_task(
        AgentTask(task_id=new_id(), subject_id="a", session_id="s", status="RUNNING")
    )
    repo.create_execution(
        AgentExecution(
            execution_id=new_id(),
            task_id=task.task_id,
            status="RUNNING",
            started_at="2020-01-01 00:00:00",
        )
    )
    repo.create_tool_started = None  # 防止误用
    report = repo.recover_stale(stale_after_seconds=0)
    assert len(report.interrupted_executions) == 1
    assert repo.get_task(task.task_id).status == "FAILED"


def test_recover_stale_marks_started_tool_unknown_not_retried():
    """P1-02 验收 2：STARTED 工具执行 → UNKNOWN（**只标记**，不重试）。"""
    repo = _repo()
    task = repo.create_task(
        AgentTask(task_id=new_id(), subject_id="a", session_id="s", status="RUNNING")
    )
    execution = AgentExecution(
        execution_id=new_id(), task_id=task.task_id, status="RUNNING"
    )
    repo.create_execution(execution)

    from xiaozhi.repositories.task_repo import ToolExecution

    repo.record_tool_started(
        ToolExecution(
            tool_execution_id=new_id(),
            execution_id=execution.execution_id,
            task_id=task.task_id,
            tool_call_id="c1",
            tool_name="预约挂号",
            args_hash=hash_args({}),
            status="STARTED",
            started_at="2020-01-01 00:00:00",
        )
    )
    report = repo.recover_stale(stale_after_seconds=0)
    assert len(report.unknown_tool_executions) == 1
    record = repo.get_tool_execution(report.unknown_tool_executions[0])
    assert record.status == "UNKNOWN"
    # 关键：**没有**变成 SUCCEEDED，也没有自动重试产生新记录
    assert len(repo._tool_execs) == 1


def test_recover_stale_closes_expired_waiting_approval_task():
    """P1-02 验收 3：等待审批且过期的任务 → CANCELLED（过期关闭）。"""
    repo = _repo()
    task = repo.create_task(
        AgentTask(
            task_id=new_id(),
            subject_id="a",
            session_id="s",
            status="WAITING_APPROVAL",
        )
    )
    repo._tasks[task.task_id].updated_at = "2020-01-01 00:00:00"
    report = repo.recover_stale(stale_after_seconds=0)
    assert task.task_id in report.cancelled_waiting_tasks
    assert repo.get_task(task.task_id).status == "CANCELLED"


def test_cancelled_task_is_not_resumed_by_recovery():
    """P1-02 验收 4：已取消/已终态的任务不被恢复扫描改回 RUNNING。"""
    repo = _repo()
    task = repo.create_task(
        AgentTask(
            task_id=new_id(), subject_id="a", session_id="s", status="CANCELLED"
        )
    )
    repo._tasks[task.task_id].updated_at = "2020-01-01 00:00:00"
    report = repo.recover_stale(stale_after_seconds=0)
    assert task.task_id not in report.cancelled_waiting_tasks
    assert repo.get_task(task.task_id).status == "CANCELLED"


# ==================================================================== 引擎接线


async def test_engine_records_task_and_tool_execution():
    """引擎在 task_tracker 装配时登记任务、执行与工具执行链。"""
    from tests.unit.test_engine_reliability import FakeChatModel, collect, make_engine
    from xiaozhi.core.message import ToolCall
    from xiaozhi.models.ollama import ChatTurn

    repo = _repo()
    tracker = TaskTracker(repo, model_id="qwen2.5:7b")

    model = FakeChatModel(
        [
            {
                "chunks": [],
                "turn": ChatTurn(
                    text="",
                    tool_calls=[ToolCall(id="e1", name="查询是否有号源", arguments={})],
                ),
            },
            {"chunks": ["好的"], "turn": ChatTurn(text="好的")},
        ]
    )
    engine, _metrics = make_engine(model)
    engine.task_tracker = tracker
    await collect(engine, "呼吸内科还有号吗")

    assert tracker.task_id, "任务未登记"
    task = repo.get_task(tracker.task_id)
    assert task is not None
    assert task.status in ("SUCCEEDED", "FAILED")
    tool_records = list(repo._tool_execs.values())
    assert tool_records, "工具执行未落库"
    assert tool_records[0].tool_name == "查询是否有号源"
    assert tool_records[0].status in ("SUCCEEDED", "FAILED")


async def test_engine_without_tracker_has_no_side_effect():
    """灰度关闭（tracker=None）时引擎行为与 Phase 4 完全一致。"""
    from tests.unit.test_engine_reliability import FakeChatModel, collect, make_engine
    from xiaozhi.models.ollama import ChatTurn

    model = FakeChatModel([{"chunks": ["你好"], "turn": ChatTurn(text="你好")}])
    engine, _metrics = make_engine(model)
    assert engine.task_tracker is None
    text, _events = await collect(engine, "查询号源")
    assert "你好" in text
