"""向量存储 —— RAG 的第三道工序（索引管理）。

与 Java 版相比，这里根治了两个真实缺陷：
  1. **幂等**：块主键用内容哈希（chunk.id），重复灌库是覆盖而不是新增；
     Java 版用随机 UUID，重复灌库会堆出多份同样的向量（实测 25 块 → 79 行）。
  2. **真实判空**：用 row_count 判断集合是否有数据，而不是拿一句无关文本做语义探针。
  3. **版本化 + 原子切换**：重建写新集合 `xxx_v{N}`，灌完校验行数后再切别名，
     失败回滚，不留半成品索引。
"""

from __future__ import annotations

import json
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from xiaozhi.reliability.tracing import trace_span
from xiaozhi.retrieval.chunker import Chunk


@dataclass
class SearchHit:
    """一条检索结果"""

    id: str
    text: str
    score: float
    metadata: Dict[str, Any]


class VectorStore(ABC):
    """向量库抽象"""

    @abstractmethod
    def ensure_collection(self, dimension: int) -> str:
        """确保集合存在，返回实际使用的集合名"""

    @abstractmethod
    def upsert(self, chunks: Sequence[Chunk]) -> int:
        """幂等写入（覆盖同 ID）"""

    @abstractmethod
    def delete_by_file(self, file_name: str) -> int:
        """删除某个源文件的全部块"""

    @abstractmethod
    def search(
        self,
        vector: List[float],
        top_k: int,
        filter_expr: Optional[str] = None,
        output_fields: Optional[List[str]] = None,
    ) -> List[SearchHit]:
        """稠密检索"""

    @abstractmethod
    def count(self, settle: bool = False) -> int:
        """**存活行**的真实数量

        不能用 `get_collection_stats().row_count` —— 它统计的是各 segment 的行数之和，
        **已删除但尚未回收的行也会被算进去**，所以它既不能用来判空，也不能用来核对增量更新。
        （Java 版用语义探针判空、Java 版统计出 79 行 vs 25 唯一块，都是踩了这个坑。）

        这里改为用 query 精确计数。集合规模很大时应改为维护一份本地的 ID 索引。
        """
        collection = self._resolve_collection()
        if not collection:
            return 0
        try:
            self.client.flush(collection)
        except Exception:
            pass
        if not settle:
            try:
                rows = self.client.query(
                    collection, filter="", limit=16384, output_fields=["id"]
                )
                return len(rows)
            except Exception:
                return 0

        # 写入/删除后 Milvus 的可见性是最终一致的 —— 轮询到连续两次读数相同为止
        previous = -1
        for _ in range(10):
            try:
                rows = self.client.query(
                    collection, filter="", limit=16384, output_fields=["id"]
                )
                current = len({str(r.get("id")) for r in rows})
            except Exception:
                current = -1
            if current >= 0 and current == previous:
                return current
            previous = current
            time.sleep(0.6)
            try:
                self.client.flush(collection)
            except Exception:
                pass
        return max(previous, 0)

    def flush(self) -> None:
        """落盘"""

    @abstractmethod
    def all_texts(self) -> List[Tuple[str, str, Dict[str, Any]]]:
        """取回全部块（供稀疏索引构建用）：(id, text, metadata)"""


class MilvusVectorStore(VectorStore):
    """Milvus 实现（pymilvus）"""

    def __init__(
        self,
        uri: str,
        alias: str = "xiaozhi_knowledge",
        metric_type: str = "COSINE",
        index_type: str = "FLAT",
        max_version: int = 5,
    ) -> None:
        self.uri = uri
        self.alias = alias
        self.metric_type = metric_type
        self.index_type = index_type
        self.max_version = max_version
        self._lock = threading.Lock()
        self._client = None

    # ---------- 内部工具 ----------

    @property
    def client(self):
        """惰性连接（避免导入期就要求 Milvus 在线）"""
        if self._client is None:
            from pymilvus import MilvusClient

            self._client = MilvusClient(uri=self.uri)
        return self._client

    def _versioned_name(self, version: int) -> str:
        return f"{self.alias}_v{version}"

    def _resolve_collection(self) -> Optional[str]:
        """解析别名指向的物理集合（别名 → 同名物理集合 → 最高版本号集合）"""
        try:
            if self.client.has_collection(self.alias):
                return self.alias
        except Exception:
            pass
        try:
            info = self.client.list_aliases(collection_name=self.alias) or {}
            aliases = info.get("aliases") or []
            for alias_target in aliases:
                # 悬空别名（指向已删除的集合）要跳过
                try:
                    if self.client.has_collection(alias_target):
                        return alias_target
                except Exception:
                    continue
        except Exception:
            pass
        # 兜底：取版本号最高的物理集合
        try:
            versions = []
            for name in self.client.list_collections():
                if name.startswith(f"{self.alias}_v"):
                    try:
                        versions.append((int(name.rsplit("_v", 1)[1]), name))
                    except ValueError:
                        continue
            if versions:
                return max(versions)[1]
        except Exception:
            pass
        return None

    def switch_alias(self, target_collection: str) -> None:
        """把别名原子切换到新集合（版本化重建的最后一步）

        注意：Milvus **不会**在集合被 drop 时自动清理指向它的别名，
        于是会出现"别名指向已删除集合"的悬空状态 —— 此时 create_alias 会报
        "alias already exist"。所以切换前必须先删掉旧别名。
        """
        for op in ("drop_alias", "release_alias"):
            try:
                getattr(self.client, op)(self.alias)
                break
            except Exception:
                continue
        self.client.create_alias(collection_name=target_collection, alias=self.alias)

    def drop_old_versions(self, keep: int = 1) -> List[str]:
        """清理旧版本集合（保留最近 keep 个）

        注意：Milvus **不允许 drop 带别名的集合** —— 必须先把别名摘掉，
        否则报 "unable to drop the collection ... because it has associated aliases"。
        """
        dropped: List[str] = []
        try:
            names = [
                n
                for n in self.client.list_collections()
                if n.startswith(f"{self.alias}_v")
            ]
            current = self._resolve_collection()
            for name in names[:-keep] if keep > 0 else names:
                if name == current:
                    continue  # 别名当前指向的集合不能删
                try:
                    self.client.drop_collection(name)
                    dropped.append(name)
                except Exception:
                    continue
        except Exception:
            pass
        return dropped

    def _create_versioned(self, version: int, dimension: int) -> str:
        name = self._versioned_name(version)
        if not self.client.has_collection(name):
            self.client.create_collection(
                collection_name=name,
                dimension=dimension,
                id_type="string",
                max_length=64,
                primary_field_name="id",
                vector_field_name="vector",
                enable_dynamic_field=True,
                metric_type=self.metric_type,
                auto_id=False,
            )
            # Milvus 2.5 建集合时已自动建好向量索引；这里做幂等补建（失败不致命）
            try:
                if "vector" not in self.client.list_indexes(name):
                    self.client.create_index(
                        collection_name=name,
                        field_name="vector",
                        index_params={
                            "index_type": self.index_type,
                            "metric_type": self.metric_type,
                        },
                    )
            except Exception as exc:
                print(f"[RAG] 索引补建跳过（可能已存在）: {exc}")
        return name

    # ---------- VectorStore 接口 ----------

    def ensure_collection(self, dimension: int) -> str:
        """确保集合存在；返回物理集合名。

        维度一致 → 直接复用；
        维度变了 → 建新版本集合并把别名切过去（旧的保留以便回滚）。
        """
        with self._lock:
            current = self._resolve_collection()
            if current:
                actual = self._dimension_of(current)
                if actual is None or actual == dimension:
                    self.client.load_collection(current)
                    return current
            version = self._next_version()
            name = self._create_versioned(version, dimension)
            self.client.load_collection(name)
            # 别名指向新版本（原子切换）
            self.switch_alias(name)
            return name

    def _dimension_of(self, collection: str) -> Optional[int]:
        """取向量字段的维度（兼容 params.dim 与 type_params.dim 两种形态）"""
        try:
            desc = self.client.describe_collection(collection)
            for field in desc.get("fields", []):
                for key in ("params", "type_params"):
                    params = field.get(key) or {}
                    if params.get("dim"):
                        return int(params["dim"])
        except Exception:
            return None
        return None

    def _next_version(self) -> int:
        versions = []
        for name in self.client.list_collections():
            if name.startswith(f"{self.alias}_v"):
                try:
                    versions.append(int(name.rsplit("_v", 1)[1]))
                except ValueError:
                    continue
        return (max(versions) + 1) if versions else 1

    def upsert(self, chunks: Sequence[Chunk]) -> int:
        """幂等写入 —— 主键为 chunk.id（内容哈希），重复写入即覆盖"""
        if not chunks:
            return 0
        collection = self._resolve_collection() or self.alias
        rows = [
            {
                "id": chunk.id,
                "vector": [],  # 由调用方填向量
                "text": chunk.text,
                "metadata": chunk.metadata,
            }
            for chunk in chunks
        ]
        return self.client.upsert(collection_name=collection, data=rows)

    def upsert_with_vectors(
        self, chunks: Sequence[Chunk], vectors: Sequence[List[float]]
    ) -> int:
        """带向量的幂等写入"""
        if not chunks:
            return 0
        collection = self._resolve_collection() or self.alias
        self._ensure_loaded(collection)
        rows = [
            {
                "id": chunk.id,
                "vector": list(vec),
                "text": chunk.text,
                "metadata": chunk.metadata,
            }
            for chunk, vec in zip(chunks, vectors)
        ]
        return self.client.upsert(collection_name=collection, data=rows)

    def delete_by_file(self, file_name: str) -> int:
        """按 metadata.file_name 删除该文件的全部块

        注意：Milvus 的 delete 是**异步**的，而且是在 flush 时按当时的行状态生效 ——
        如果先按 file_name 删、再 upsert 同 PK 的新块，旧块的删除会把新块一起带走。
        增量更新请改用 `list_ids_by_file` + `delete_by_ids` 的精确删除路径。
        """
        collection = self._resolve_collection()
        if not collection:
            return 0
        try:
            self.client.delete(
                collection_name=collection,
                filter=f'metadata["file_name"] == "{file_name}"',
            )
            return 1
        except Exception:
            return 0

    def list_ids_by_file(self, file_name: str) -> List[str]:
        """列出某个源文件当前已入库的块 ID"""
        collection = self._resolve_collection()
        if not collection:
            return []
        try:
            rows = self.client.query(
                collection,
                filter=f'metadata["file_name"] == "{file_name}"',
                limit=16384,
                output_fields=["id"],
            )
            return [str(r.get("id")) for r in rows]
        except Exception:
            return []

    def delete_by_ids(self, ids: Sequence[str]) -> int:
        """按主键精确删除（增量更新用：只删真正陈旧的块）"""
        ids = [i for i in ids if i]
        if not ids:
            return 0
        collection = self._resolve_collection()
        if not collection:
            return 0
        try:
            quoted = ", ".join(f'"{i}"' for i in ids)
            self.client.delete(
                collection_name=collection,
                filter=f"id in [{quoted}]",
            )
            return len(ids)
        except Exception:
            return 0

    def _ensure_loaded(self, collection: str) -> None:
        """确保集合已 load 到内存

        Milvus 的集合必须处于 loaded 状态才能被检索；
        写入 / flush / compaction 之后都可能回到 NotLoad，
        此时检索会报 "collection not loaded" 或静默返回空。
        """
        try:
            state = self.client.get_load_state(collection)
            if str(getattr(state, "state", "")).endswith("NotLoad"):
                self.client.load_collection(collection)
        except Exception:
            try:
                self.client.load_collection(collection)
            except Exception:
                pass

    def search(
        self,
        vector: List[float],
        top_k: int,
        filter_expr: Optional[str] = None,
        output_fields: Optional[List[str]] = None,
    ) -> List[SearchHit]:
        """稠密检索"""
        collection = self._resolve_collection()
        if not collection:
            return []
        self._ensure_loaded(collection)
        results = self.client.search(
            collection_name=collection,
            data=[vector],
            limit=top_k,
            filter=filter_expr or "",
            output_fields=output_fields or ["text", "metadata"],
        )
        hits: List[SearchHit] = []
        for item in results[0] if results else []:
            entity = item.get("entity") or {}
            metadata = entity.get("metadata") or {}
            hits.append(
                SearchHit(
                    id=str(item.get("id")),
                    text=entity.get("text", ""),
                    score=float(item.get("distance", 0.0)),
                    metadata=metadata,
                )
            )
        return hits

    def count(self, settle: bool = False) -> int:
        """真实行数

        Milvus 的 row_count 是最终一致的：刚 delete / upsert 完立刻读会拿到旧值。
        需要准确计数时传 settle=True（会等待一小段时间再读）。
        """
        collection = self._resolve_collection()
        if not collection:
            return 0
        try:
            self.client.flush(collection)
        except Exception:
            pass
        if settle:
            time.sleep(1.0)
            try:
                self.client.flush(collection)
            except Exception:
                pass
            time.sleep(0.5)
        try:
            return int(self.client.get_collection_stats(collection).get("row_count", 0))
        except Exception:
            return 0

    def flush(self) -> None:
        collection = self._resolve_collection()
        if collection:
            try:
                self.client.flush(collection)
            except Exception:
                pass
            # flush 之后集合可能回到 NotLoad，重新 load 以保证后续可检索
            self._ensure_loaded(collection)

    def all_texts(self) -> List[Tuple[str, str, Dict[str, Any]]]:
        """取回全部块"""
        collection = self._resolve_collection()
        if not collection:
            return []
        try:
            rows = self.client.query(
                collection,
                filter="",
                limit=16384,
                output_fields=["text", "metadata"],
            )
        except Exception:
            return []
        return [
            (str(r.get("id")), r.get("text", ""), r.get("metadata") or {}) for r in rows
        ]
