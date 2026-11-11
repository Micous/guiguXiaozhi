"""Phase 6 单测：Prometheus 导出 / 审计 fail-closed / Redis 共享限流 / 灌库 Job。

四个模块的验收点：

* **Prometheus**：导出格式能被标准解析器吃下（HELP/TYPE/桶累计）、单位是秒、
  标签是闭集（工具名 + outcome），不能出现用户/会话这类高基数维度。
* **审计**：fail-closed 时"写不进去就别执行"，且执行前的审计与执行后的审计
  语义不同（前者能拦住操作，后者只能留痕）。
* **限流**：两个实例共享同一个 Redis 计数器时额度**不会被放大**；
  TTL 自愈（崩溃留下的永不过期 key）不能把用户永久限流。
* **灌库 Job**：单写者锁、校验不通过时**别名不切换**、回滚把清单移走
  免得下次增量灌库误判"文件没变"。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from xiaozhi.config import Settings  # noqa: E402
from xiaozhi.core.tool import RiskLevel, Tool, ToolDefinition, ToolParameter, ToolResult  # noqa: E402
from xiaozhi.ops.audit import AuditLogger, AuditWriteError, FileAuditSink  # noqa: E402
from xiaozhi.ops.metrics import MetricsCollector  # noqa: E402
from xiaozhi.ops.prometheus import (  # noqa: E402
    BUCKET_BOUNDS_SECONDS,
    iter_metric_names,
    render_prometheus,
)
from xiaozhi.ops.redis_limiter import (  # noqa: E402
    RateLimitBackendError,
    RedisRateLimiter,
    build_rate_limiter,
)
from xiaozhi.retrieval.chunker import Chunk  # noqa: E402
from xiaozhi.retrieval.rag import RagService, RagStats  # noqa: E402
from xiaozhi.retrieval.vector_store import MilvusVectorStore  # noqa: E402
from xiaozhi.security.permissions import PermissionDecisionEngine, PermissionLevel  # noqa: E402
from xiaozhi.security.secure_executor import SecureToolExecutor, ToolCall  # noqa: E402

TMP = ROOT / "_tmp_phase6"


@pytest.fixture(autouse=True)
def _clean_tmp():
    import shutil

    shutil.rmtree(TMP, ignore_errors=True)
    TMP.mkdir(parents=True, exist_ok=True)
    yield
    shutil.rmtree(TMP, ignore_errors=True)


# ---------------------------------------------------------------- Prometheus


def _sample_snapshot() -> dict:
    return {
        "uptime_seconds": 12.0,
        "counters": {
            "requests_total": 10,
            "tool_calls_total": 3,
            "tool.BookAppointment.success": 2,
            "tool.QuerySlotTool.failed": 1,
            "hallucination_suspected": 0,
        },
        "gauges": {"queue_depth": 4},
        "histograms": {
            "request_duration_ms": {
                "count": 3,
                "avg_ms": 500.0,
                "p50_ms": 200.0,
                "p95_ms": 1200.0,
                "max_ms": 1200.0,
            }
        },
    }


def test_prometheus_counters_use_closed_label_set():
    text = render_prometheus(_sample_snapshot(), {"request_duration_ms": [100, 200, 1200]})
    # 工具名与 outcome 进标签，不编进指标名；标签按字典序输出（便于 diff）
    assert 'xiaozhi_tool_calls_total{outcome="success",tool="BookAppointment"} 2' in text
    assert 'xiaozhi_tool_calls_total{outcome="failed",tool="QuerySlotTool"} 1' in text
    # 汇总计数与带标签的族共用同一指标名（Prometheus 允许同族不同标签组合）
    assert "xiaozhi_tool_calls_total 3" in text
    # 已经以 _total 结尾的不重复加后缀
    assert "xiaozhi_requests_total_total" not in text


def test_prometheus_histogram_buckets_are_cumulative_and_in_seconds():
    text = render_prometheus(_sample_snapshot(), {"request_duration_ms": [100, 200, 1200]})
    # 100ms 落在 0.1s 桶内，200ms 落在 0.25s，1200ms 落在 2.5s
    assert 'xiaozhi_request_duration_seconds_bucket{le="0.1"} 1' in text
    assert 'xiaozhi_request_duration_seconds_bucket{le="0.25"} 2' in text
    assert 'xiaozhi_request_duration_seconds_bucket{le="2.5"} 3' in text
    assert 'xiaozhi_request_duration_seconds_bucket{le="+Inf"} 3' in text
    # 单位必须是秒（1.5s）
    assert "xiaozhi_request_duration_seconds_sum 1.5" in text
    assert "xiaozhi_request_duration_seconds_count 3" in text
    # 桶边界有序且单调
    assert list(BUCKET_BOUNDS_SECONDS) == sorted(BUCKET_BOUNDS_SECONDS)


def test_prometheus_without_samples_still_exports_count_and_sum():
    text = render_prometheus(_sample_snapshot())
    assert "xiaozhi_request_duration_seconds_count 3" in text
    # 没有样本时用 avg*count 兜底（0.5s * 3 = 1.5s）
    assert "xiaozhi_request_duration_seconds_sum 1.5" in text
    assert "_bucket{le=\"+Inf\"} 3" in text


def test_prometheus_escapes_label_values_and_help():
    snapshot = {
        "counters": {"tool.工具\"带引号\\.success": 1},
        "gauges": {},
        "histograms": {},
    }
    text = render_prometheus(snapshot)
    assert '\\"' in text and "\\\\" in text
    assert "# HELP" in text and "# TYPE" in text
    assert text.endswith("\n")


def test_prometheus_never_leaks_high_cardinality_labels():
    """闭合性检查：导出文本里不允许出现 session/user/trace 之类维度。"""
    text = render_prometheus(
        _sample_snapshot(),
        {"request_duration_ms": [10]},
        namespace="xiaozhi",
    )
    for forbidden in ("session_id=", "user_id=", "trace_id=", "patient=", "dept="):
        assert forbidden not in text


def test_prometheus_metric_names_are_unique():
    names = list(iter_metric_names(_sample_snapshot()))
    assert len(names) == len(set(names))
    assert "xiaozhi_tool_calls_total" in names
    assert "xiaozhi_request_duration_seconds" in names


def test_metrics_collector_raw_samples_returns_copy():
    metrics = MetricsCollector()
    metrics.observe("request_duration_ms", 12.0)
    samples = metrics.raw_samples("request_duration")
    samples["request_duration_ms"].append(999.0)
    assert metrics.raw_samples("request_duration")["request_duration_ms"] == [12.0]


# ---------------------------------------------------------------- 审计 fail-closed


class BrokenSink:
    def __init__(self) -> None:
        self.calls = 0

    def write(self, entry) -> None:
        self.calls += 1
        raise OSError("磁盘已满")


class RecordingSink:
    def __init__(self) -> None:
        self.entries = []

    def write(self, entry) -> None:
        self.entries.append(entry)


def test_audit_fail_open_keeps_flow_running():
    """默认 fail-open：写失败不抛，但必须计数并留痕（否则没人会发现）。"""
    errors = []
    audit = AuditLogger(
        None, sinks=[BrokenSink()], on_write_error=errors.append
    )
    audit.record("tool_execution", tool_name="x")
    assert audit.write_errors == 1
    assert errors and "磁盘已满" in errors[0]
    assert audit.read_recent()  # 内存里仍然有记录


def test_audit_fail_closed_raises():
    audit = AuditLogger(None, sinks=[BrokenSink()], fail_closed=True)
    with pytest.raises(AuditWriteError):
        audit.record("permission_decision", tool_name="x")
    assert audit.write_errors == 1


def test_audit_fail_closed_conflicts_with_disabled():
    with pytest.raises(ValueError):
        AuditLogger(None, enabled=False, fail_closed=True)


def test_audit_writes_all_sinks_and_file():
    sink = RecordingSink()
    path = TMP / "audit.jsonl"
    audit = AuditLogger(str(path), sinks=[sink])
    audit.record("tool_execution", tool_name="查号源", id_card="110101198805123456")
    assert len(sink.entries) == 1
    content = path.read_text(encoding="utf-8")
    assert "查号源" in content
    # 落盘前脱敏：完整身份证号不得出现
    assert "110101198805123456" not in content
    assert isinstance(audit._sinks[0], FileAuditSink)


class CountingTool(Tool):
    def __init__(self, name: str = "测试工具", risk_level: RiskLevel = RiskLevel.READ_ONLY):
        self._name = name
        self._risk = risk_level
        self.call_count = 0

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self._name,
            description="测试",
            parameters=[ToolParameter("username", "string", "患者姓名")],
            risk_level=self._risk,
        )

    async def call(self, params):
        self.call_count += 1
        return ToolResult.ok("done")


async def test_executor_refuses_to_run_when_audit_fails_closed():
    """审计不可用 → 工具**不得执行**，且给出可判别的错误类型。"""
    metrics = MetricsCollector()
    executor = SecureToolExecutor(
        PermissionDecisionEngine(policies={"测试工具": PermissionLevel.AUTO}),
        no_ui_policy="deny",
        audit_logger=AuditLogger(None, sinks=[BrokenSink()], fail_closed=True),
        metrics=metrics,
    )
    tool = CountingTool()
    result = await executor.execute(
        ToolCall(tool_name="测试工具", args={"username": "王新勃"}), tool
    )
    assert result.success is False
    assert result.error_type == "AuditUnavailable"
    assert tool.call_count == 0
    assert metrics.snapshot()["counters"]["audit_write_failed_total"] == 1


async def test_executor_still_runs_when_audit_fails_open():
    executor = SecureToolExecutor(
        PermissionDecisionEngine(policies={"测试工具": PermissionLevel.AUTO}),
        no_ui_policy="deny",
        audit_logger=AuditLogger(None, sinks=[BrokenSink()]),
    )
    tool = CountingTool()
    result = await executor.execute(
        ToolCall(tool_name="测试工具", args={"username": "王新勃"}), tool
    )
    assert result.success is True
    assert tool.call_count == 1


# ---------------------------------------------------------------- Redis 限流


class FakeRedis:
    """只实现限流器用到的命令（INCR/TTL/EXPIRE/GET/DELETE/PING）。"""

    def __init__(self) -> None:
        self.counters: dict = {}
        self.ttls: dict = {}
        self.fail = False
        self.expire_calls = 0

    def _check(self):
        if self.fail:
            raise ConnectionError("redis down")

    def ping(self):
        self._check()
        return True

    def incr(self, key):
        self._check()
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]

    def expire(self, key, seconds):
        self._check()
        self.expire_calls += 1
        self.ttls[key] = seconds
        return True

    def ttl(self, key):
        self._check()
        return self.ttls.get(key, -1)

    def get(self, key):
        self._check()
        return str(self.counters.get(key, 0))

    def delete(self, key):
        self._check()
        self.counters.pop(key, None)
        self.ttls.pop(key, None)
        return 1

    def scan_iter(self, match=None):
        self._check()
        return iter(list(self.counters))


def test_redis_limiter_quota_is_shared_across_instances():
    """核心诉求：两个应用实例用同一个 Redis，额度不会被放大成 2 倍。"""
    client = FakeRedis()
    a = RedisRateLimiter("redis://x", max_requests=3, window_seconds=60, client=client, clock=lambda: 100.0)
    b = RedisRateLimiter("redis://x", max_requests=3, window_seconds=60, client=client, clock=lambda: 100.0)
    verdicts = [a.allow("u1"), b.allow("u1"), a.allow("u1"), b.allow("u1")]
    assert verdicts == [True, True, True, False]
    # 拒绝计数落在**做判断的那个实例**上（共享的是额度，不是进程内计数）
    assert b.snapshot()["rejected_total"] == 1


def test_redis_limiter_window_rollover_frees_quota():
    client = FakeRedis()
    now = [100.0]
    limiter = RedisRateLimiter(
        "redis://x", max_requests=1, window_seconds=60, client=client, clock=lambda: now[0]
    )
    assert limiter.allow("u1") is True
    assert limiter.allow("u1") is False
    now[0] += 60  # 进入下一个窗口 → 新 key
    assert limiter.allow("u1") is True


def test_redis_limiter_heals_missing_ttl():
    """崩溃留下的永不过期 key 必须被自愈，否则该用户被永久限流。"""
    client = FakeRedis()
    limiter = RedisRateLimiter(
        "redis://x", max_requests=5, window_seconds=60, client=client, clock=lambda: 100.0
    )
    limiter.allow("u1")
    key = list(client.counters)[0]
    del client.ttls[key]  # 模拟 INCR 后进程崩溃
    before = client.expire_calls
    limiter.allow("u1")
    assert client.expire_calls > before
    assert client.ttl(key) > 0


def test_redis_limiter_backend_failure_is_503_not_429():
    """后端故障抛专门异常（路由据此返回 503），不能和"用户超限"混为一谈。"""
    client = FakeRedis()
    limiter = RedisRateLimiter("redis://x", max_requests=1, client=client)
    client.fail = True
    with pytest.raises(RateLimitBackendError):
        limiter.allow("u1")


def test_redis_limiter_can_fail_open_when_explicitly_configured():
    client = FakeRedis()
    client.fail = True
    limiter = RedisRateLimiter("redis://x", max_requests=1, client=client, fail_open=True)
    assert limiter.allow("u1") is True
    assert limiter.snapshot()["backend_errors_total"] == 1


def test_build_rate_limiter_memory_is_default():
    limiter = build_rate_limiter(Settings(_env_file=None))
    assert limiter.snapshot()["backend"] == "memory"


def test_build_rate_limiter_redis_probe_failure_raises():
    settings = Settings(_env_file=None, rate_limit_backend="redis", redis_url="redis://127.0.0.1:6399/0")
    # 端口上没有服务 → 探活必须失败（组合根据此拒绝启动，不静默退回内存）
    with pytest.raises(RateLimitBackendError):
        build_rate_limiter(settings)


def test_build_rate_limiter_rejects_unknown_backend():
    with pytest.raises(ValueError):
        build_rate_limiter(Settings(_env_file=None, rate_limit_backend="memcached"))


# ---------------------------------------------------------------- RAG 只读挂载


class FakeVectorStore:
    """只读挂载用：不实现任何写方法（谁调用写方法谁就报错）。"""

    def __init__(self, rows):
        self.rows = rows
        self.write_calls = []

    def all_texts(self):
        return list(self.rows)

    def count(self, settle=False):
        return len(self.rows)

    def flush(self):
        self.write_calls.append("flush")

    def upsert_with_vectors(self, chunks, vectors):  # pragma: no cover - 不应被调用
        self.write_calls.append("upsert")
        raise AssertionError("只读挂载不允许写向量库")

    def delete_by_file(self, name):  # pragma: no cover
        raise AssertionError("只读挂载不允许删除")


class _NoEmbed:
    async def ensure_dimension(self):
        raise AssertionError("只读挂载不应嵌入")

    async def embed(self, text):  # pragma: no cover
        raise AssertionError("只读挂载不应嵌入")


def _rag_with(store, *, knowledge_dir=None, manifest_path=None) -> RagService:
    return RagService(
        vector_store=store,
        embedding_model=_NoEmbed(),
        knowledge_dir=knowledge_dir or (TMP / "knowledge"),
        manifest_path=manifest_path or (TMP / "manifest.json"),
    )


async def test_mount_readonly_builds_sparse_index_without_writing():
    store = FakeVectorStore(
        [
            ("id1", "协和医院地址是帅府园一号", {"file_name": "医院信息.md"}),
            ("id2", "神经内科专家出诊时间", {"file_name": "神经内科.md"}),
            ("id3", "张三 神经内科", {"file_name": "doctors.md"}),
        ]
    )
    rag = _rag_with(store)
    stats = await rag.mount_readonly()
    assert stats.degraded is False
    assert stats.chunks == 3
    assert stats.files == 3
    assert store.write_calls == []  # 关键：只读
    # 稀疏索引已建好，且排除 doctors.md（与 chat RAG 的口径一致）
    assert rag.bm25.search("协和医院", top_k=3)
    assert all(
        hit.metadata.get("file_name") != "doctors.md"
        for hit in rag.bm25.search("神经内科", top_k=5)
    )


async def test_mount_readonly_marks_degraded_when_index_empty():
    rag = _rag_with(FakeVectorStore([]))
    stats = await rag.mount_readonly()
    assert stats.degraded is True
    assert "空" in (stats.error or "") or "不可达" in (stats.error or "")
    assert rag._degraded is True


def test_pending_changes_lists_untracked_or_modified_files():
    knowledge = TMP / "knowledge"
    knowledge.mkdir(parents=True, exist_ok=True)
    (knowledge / "a.md").write_text("A 内容", encoding="utf-8")
    (knowledge / "b.md").write_text("B 内容 v1", encoding="utf-8")
    manifest = TMP / "manifest.json"

    rag = _rag_with(FakeVectorStore([]), knowledge_dir=knowledge, manifest_path=manifest)
    # 没有清单（从未灌库）→ 全部文件都算未灌
    assert rag.pending_changes() == ["a.md", "b.md"]

    from xiaozhi.retrieval.manifest import build_manifest, save_manifest

    save_manifest(manifest, build_manifest(knowledge, {"a.md": 1, "b.md": 1}))
    assert rag.pending_changes() == []

    (knowledge / "b.md").write_text("B 内容 v2", encoding="utf-8")
    (knowledge / "c.md").write_text("新增", encoding="utf-8")
    assert rag.pending_changes() == ["b.md", "c.md"]


# ---------------------------------------------------------------- 版本化集合 / 直方图


class FakeMilvus:
    """Milvus 集合与别名的内存替身（只实现 version/alias 相关接口）。"""

    def __init__(self) -> None:
        self.collections: dict = {}
        self.aliases: dict = {}
        self.ops: list = []

    def list_collections(self):
        return sorted(self.collections)

    def has_collection(self, name):
        return name in self.collections

    def create_collection(self, collection_name, dimension, **kwargs):
        self.collections[collection_name] = {"dim": dimension, "rows": {}}
        self.ops.append(("create", collection_name))

    def load_collection(self, name):
        self.ops.append(("load", name))

    def describe_collection(self, name):
        return {"fields": [{"params": {"dim": self.collections[name]["dim"]}}]}

    def list_indexes(self, name):
        return ["vector"]

    def drop_alias(self, alias):
        self.aliases.pop(alias, None)
        self.ops.append(("drop_alias", alias))

    def create_alias(self, collection_name, alias):
        if alias in self.aliases:
            raise RuntimeError("alias already exist")
        self.aliases[alias] = collection_name
        self.ops.append(("create_alias", alias, collection_name))

    def describe_alias(self, alias):
        if alias not in self.aliases:
            raise KeyError(f"alias {alias} not found")
        return {"alias": alias, "collection_name": self.aliases[alias], "db_name": "default"}

    def list_aliases(self, collection_name=None):
        return {
            "aliases": [a for a, c in self.aliases.items() if c == collection_name]
        }

    def drop_collection(self, name):
        self.collections.pop(name, None)
        self.ops.append(("drop", name))

    def upsert(self, collection_name, data):
        rows = self.collections[collection_name]["rows"]
        for row in data:
            rows[row["id"]] = row
        return len(data)

    def query(self, collection, filter="", limit=16384, output_fields=None, **kwargs):
        return list(self.collections[collection]["rows"].values())[:limit]

    def flush(self, name):
        return None

    def get_collection_stats(self, name):
        return {"row_count": len(self.collections[name]["rows"])}

    def get_load_state(self, name):
        return SimpleNamespace(state="Loaded")

    def delete(self, collection_name, filter):
        return 0

    def search(self, **kwargs):  # pragma: no cover
        return [[]]


def _store_with_fake(fake: FakeMilvus) -> MilvusVectorStore:
    store = MilvusVectorStore(uri="http://fake", alias="kb")
    store._client = fake
    return store


def test_pin_collection_overrides_alias_resolution():
    fake = FakeMilvus()
    store = _store_with_fake(fake)
    v1 = store.create_new_version(8)
    v2 = store.create_new_version(8)
    store.switch_to_version(1)
    assert store.current_version() == 1

    # 灌库期间 pin 到 v2：写入必须落 v2，而别名仍指向 v1（读者无感）
    store.pin_collection(v2)
    store.upsert_with_vectors(
        [Chunk(id="c1", text="t", metadata={"file_name": "a.md"})], [[0.0] * 8]
    )
    assert "c1" in fake.collections[v2]["rows"]
    assert "c1" not in fake.collections[v1]["rows"]
    store.switch_alias(v2)
    store.pin_collection(None)
    assert store.current_version() == 2


def test_drop_old_versions_never_drops_current():
    fake = FakeMilvus()
    store = _store_with_fake(fake)
    store.create_new_version(8)
    store.create_new_version(8)
    store.create_new_version(8)
    store.switch_to_version(3)
    dropped = store.drop_old_versions(keep=1)
    assert "kb_v3" in fake.collections  # 当前版本必须留着
    assert dropped and "kb_v3" not in dropped


def test_drop_old_versions_keeps_newest_even_if_listing_is_unordered():
    """``list_collections()`` 顺序不保证递增（实测 v1,v3,v2），切片前必须按版本号排序。

    真机踩到：keep=2 时把 v2 删了、留下 v1 —— "保留最近两个"变成"保留最旧两个"，
    等于把可回滚的版本删掉。
    """
    fake = FakeMilvus()
    store = _store_with_fake(fake)
    store.create_new_version(8)
    store.create_new_version(8)
    store.create_new_version(8)
    fake.list_collections = lambda: ["kb_v1", "kb_v3", "kb_v2"]  # 乱序返回
    store.switch_to_version(3)
    dropped = store.drop_old_versions(keep=2)
    assert dropped == ["kb_v1"]
    assert set(fake.collections) == {"kb_v2", "kb_v3"}


# ---------------------------------------------------------------- 灌库 Job


def _load_ingest_module():
    path = ROOT / "scripts" / "ingest_knowledge.py"
    spec = importlib.util.spec_from_file_location("ingest_knowledge", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeIngestStore:
    def __init__(self, *, visible_rows=None):
        self.alias = "kb"
        self.versions = [1]
        self.current = 1
        self.pinned = None
        self.switched = []
        self.dropped = []
        self.visible_rows = visible_rows

    def current_version(self):
        return self.current

    def create_new_version(self, dimension):
        self.versions.append(2)
        return "kb_v2"

    def pin_collection(self, name):
        self.pinned = name

    def count(self, settle=False):
        return self.visible_rows if self.visible_rows is not None else 10

    def switch_alias(self, name):
        self.switched.append(name)
        self.current = 2

    def drop_old_versions(self, keep=1):
        self.dropped.append(keep)
        return ["kb_v1"]

    def list_versions(self):
        return list(self.versions)

    def switch_to_version(self, version):
        self.switched.append(f"kb_v{version}")
        self.current = version
        return f"kb_v{version}"


class _Embedding:
    async def ensure_dimension(self):
        return 8


class FakeRag:
    def __init__(self, *, degraded=False, chunks=10, error=None, manifest_path=None):
        self.degraded = degraded
        self.chunks = chunks
        self.error = error
        self.manifest_path = manifest_path or (TMP / "manifest.json")

    async def bootstrap(self):
        return RagStats(
            chunks=self.chunks,
            files=3,
            updated_files=["a.md"],
            degraded=self.degraded,
            error=self.error,
        )


def test_force_ingest_switches_alias_only_after_verification(monkeypatch):
    module = _load_ingest_module()
    store = FakeIngestStore(visible_rows=10)
    rag = FakeRag(chunks=10)
    monkeypatch.setattr(module, "build_stack", lambda settings: (rag, store, _Embedding()))
    code = asyncio.run(module.ingest(Settings(_env_file=None), force=True, keep=1))
    assert code == module.EXIT_OK
    assert store.switched == ["kb_v2"]
    assert store.pinned is None  # 无论成功失败都要解除 pin
    assert store.dropped == [1]


def test_force_ingest_refuses_to_switch_when_rows_missing(monkeypatch):
    """校验不通过时别名**不能切**：旧索引必须继续服务。"""
    module = _load_ingest_module()
    store = FakeIngestStore(visible_rows=3)  # 应有 10 块，只见 3 行
    rag = FakeRag(chunks=10)
    monkeypatch.setattr(module, "build_stack", lambda settings: (rag, store, _Embedding()))
    code = asyncio.run(module.ingest(Settings(_env_file=None), force=True, keep=1))
    assert code == module.EXIT_VERIFY_FAILED
    assert store.switched == []
    assert store.pinned is None


def test_force_ingest_does_not_switch_when_bootstrap_degraded(monkeypatch):
    module = _load_ingest_module()
    store = FakeIngestStore(visible_rows=0)
    rag = FakeRag(degraded=True, error="Milvus 不可达")
    monkeypatch.setattr(module, "build_stack", lambda settings: (rag, store, _Embedding()))
    code = asyncio.run(module.ingest(Settings(_env_file=None), force=True, keep=1))
    assert code == module.EXIT_FAILED
    assert store.switched == []


def test_rollback_moves_manifest_aside(tmp_path=None):
    module = _load_ingest_module()
    manifest = TMP / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    store = FakeIngestStore()
    store.versions = [1, 2]
    store.current = 2
    rag = FakeRag(manifest_path=manifest)
    module.build_stack = lambda settings: (rag, store, _Embedding())
    code = module.rollback(Settings(_env_file=None), target_version=None)
    assert code == module.EXIT_OK
    assert store.switched == ["kb_v1"]
    # 清单必须被移走：否则下次增量灌库会误判"文件没变"而跳过重建
    assert not manifest.exists()
    assert (TMP / "manifest.json.rollback").exists()


def test_rollback_rejects_when_no_older_version():
    module = _load_ingest_module()
    store = FakeIngestStore()
    store.versions = [1]
    store.current = 1
    module.build_stack = lambda settings: (FakeRag(), store, _Embedding())
    assert module.rollback(Settings(_env_file=None), target_version=None) == module.EXIT_FAILED


def test_single_writer_lock_is_exclusive():
    module = _load_ingest_module()
    lock_path = TMP / "ingest.lock"
    first = module.SingleWriterLock(lock_path)
    second = module.SingleWriterLock(lock_path)
    assert first.acquire() is True
    try:
        assert second.acquire() is False  # 同一进程内两次独占也会被 flock 拦住
    finally:
        first.release()
        second.release()
    assert second.acquire() is True
    second.release()
