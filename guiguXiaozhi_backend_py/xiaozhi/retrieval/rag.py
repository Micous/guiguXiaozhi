"""RAG 服务 —— 把解析/分块/索引/检索串成一条可上线的链路。

职责：
  * bootstrap：解析 → 分块 → 向量化 → **幂等增量**写入 Milvus → 重建 BM25 稀疏索引
  * retrieve：稠密 + 稀疏 → RRF 融合 → 返回知识片段
  * 降级：Milvus 不可用时不阻塞启动，检索时返回空并标记 degraded
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from xiaozhi.reliability.tracing import trace_span

logger = logging.getLogger(__name__)
from xiaozhi.retrieval.bm25 import BM25Index
from xiaozhi.retrieval.chunker import Chunk, Chunker
from xiaozhi.retrieval.hybrid import HybridHit, HybridRetriever
from xiaozhi.retrieval.manifest import Manifest, build_manifest, load_manifest, save_manifest
from xiaozhi.retrieval.parser import parse_directory
from xiaozhi.retrieval.vector_store import VectorStore


@dataclass
class RagStats:
    """RAG 运行统计"""

    chunks: int = 0                 # 权威块数（本次写入的块集大小）
    collection_rows: int = 0        # Milvus 观测行数（最终一致，可能短暂虚高）
    files: int = 0
    updated_files: List[str] = field(default_factory=list)
    removed_files: List[str] = field(default_factory=list)
    duration_ms: float = 0.0
    degraded: bool = False
    error: Optional[str] = None


class RagService:
    """RAG 门面"""

    def __init__(
        self,
        vector_store: VectorStore,
        embedding_model,
        knowledge_dir: Path,
        manifest_path: Path,
        chunker: Optional[Chunker] = None,
        dense_top_k: int = 20,
        sparse_top_k: int = 20,
        dense_min_score: float = 0.35,
        excluded_files: Sequence[str] = ("doctors.md",),
    ) -> None:
        self.vector_store = vector_store
        self.embedding_model = embedding_model
        self.knowledge_dir = knowledge_dir
        self.manifest_path = manifest_path
        self.chunker = chunker or Chunker()
        self.dense_top_k = dense_top_k
        self.sparse_top_k = sparse_top_k
        # 阈值必须跟分块策略一起标定：换了分块，向量分布就变了。
        # 本实现（标题路径前缀 + 不合并）实测：相关问题 0.34~0.67，无关问题 ≤0.41，
        # 故取 0.35 —— 保留 9/10 相关问题，只放过 1/7 无关问题。
        self.dense_min_score = dense_min_score
        self.excluded_files = set(excluded_files)
        self.bm25 = BM25Index()
        self._ready = False
        self._degraded = False

    # ---------- 索引构建 ----------

    async def bootstrap(self) -> RagStats:
        """解析 → 分块 → 向量化 → 幂等写入 → 建稀疏索引"""
        stats = RagStats()
        start = time.perf_counter()
        with trace_span("retrieval.bootstrap") as span:
            try:
                sections = parse_directory(self.knowledge_dir)
                chunks = self.chunker.chunk(sections)
                stats.chunks = len(chunks)
                stats.files = len({c.metadata.get("file_name", "") for c in chunks})
                span.set_attribute("chunk_count", len(chunks))

                # 集合（版本化 + 维度校验）
                dimension = await self.embedding_model.ensure_dimension()
                self.vector_store.ensure_collection(dimension)

                # 增量：按文件内容哈希决定要重建哪些文件
                chunk_counts: Dict[str, int] = {}
                for chunk in chunks:
                    name = chunk.metadata.get("file_name", "")
                    chunk_counts[name] = chunk_counts.get(name, 0) + 1

                new_manifest = build_manifest(self.knowledge_dir, chunk_counts)
                old_manifest = load_manifest(self.manifest_path)
                diff = old_manifest.diff(new_manifest.files)

                # 只信清单是不够的：集合可能被人删了 / 被清空了，而清单还在磁盘上，
                # 这时 diff 会说"没有变化"→ 跳过灌库 → 检索永远召回不到东西。
                # 所以要用**集合实际行数**兜底校验。
                stored_rows = self.vector_store.count()
                if stored_rows == 0 and new_manifest.files:
                    logger.info(
                        "[RAG] 集合为空但清单记录了 %s 个文件，强制全量重建",
                        len(new_manifest.files),
                    )
                    diff = {"update": list(new_manifest.files), "remove": []}
                stats.updated_files = diff["update"]
                stats.removed_files = diff["remove"]
                span.set_attribute("updated_files", diff["update"])
                span.set_attribute("removed_files", diff["remove"])

                # 删除已消失的文件（文件整体没了 → 它的块全部作废）
                for file_name in diff["remove"]:
                    self.vector_store.delete_by_file(file_name)

                # 只对变化的文件做增量重建。
                # 关键：块主键 = 内容哈希，所以
                #   1) 未变的块 upsert 是覆盖（幂等，无副作用）；
                #   2) 只有"旧块里已不存在的新块"才需要删 —— 按主键精确删，
                #      不能按 file_name 删（Milvus 的 delete 是异步的，
                #      按文件删会把刚 upsert 的同 PK 新块一起带走）。
                to_write = [
                    c
                    for c in chunks
                    if c.metadata.get("file_name") in set(diff["update"])
                ]
                stale_count = 0
                if to_write:
                    for file_name in set(diff["update"]):
                        new_ids = {
                            c.id
                            for c in to_write
                            if c.metadata.get("file_name") == file_name
                        }
                        old_ids = set(self.vector_store.list_ids_by_file(file_name))
                        stale = old_ids - new_ids
                        if stale:
                            stale_count += self.vector_store.delete_by_ids(sorted(stale))
                    vectors = await self.embedding_model.embed_batch(
                        [c.text for c in to_write]
                    )
                    self.vector_store.upsert_with_vectors(to_write, vectors)
                span.set_attribute("stale_chunks_deleted", stale_count)

                self.vector_store.flush()

                # 权威块数以"本次写入的块集"为准，而不是 Milvus 的实时行数：
                # Milvus 的 upsert 实质是"删 + 插"，会新建 segment，旧 segment 稍后才回收，
                # 因此紧接着的 query 会**短暂看到两份**（行数虚高）。数据本身是对的。
                stats.chunks = len(chunks)
                observed_rows = self.vector_store.count(settle=True)
                stats.collection_rows = observed_rows
                span.set_attribute("collection_rows_observed", observed_rows)
                span.set_attribute("chunks_authoritative", len(chunks))

                # 稀疏索引重建：直接用**本地块集**建，不用从向量库反查 ——
                # 刚写入的数据在 Milvus 里还不可查（growing segment 未落盘），
                # 反查会得到空索引，稀疏这一路就整个失效了。
                self._rebuild_bm25(chunks)

                new_manifest.version = old_manifest.version + (
                    1 if (diff["update"] or diff["remove"]) else 0
                )
                save_manifest(self.manifest_path, new_manifest)

                self._ready = True
                self._degraded = False
            except Exception as exc:
                stats.degraded = True
                stats.error = f"{type(exc).__name__}: {exc}"
                self._degraded = True
                span.set_error(stats.error)

        stats.duration_ms = (time.perf_counter() - start) * 1000
        return stats

    async def mount_readonly(self) -> RagStats:
        """只读挂载现有索引（**不写向量库、不改清单**）。

        与 ``bootstrap`` 的区别：
          * 不解析 / 不分块 / 不嵌入 / 不写集合 / 不改进度清单 —— 灌库是
            ``scripts/ingest_knowledge.py``（单写者 Job）的职责；
          * 稀疏索引从**现有集合**取回块重建（bootstrap 用本次写入的块集，
            因为刚写的数据在 Milvus 里还不可查）。

        失败语义：Milvus 不可达 / 集合不存在 / 集合为空 → ``degraded=True``，
        检索返回空；是否拒绝启动由 ``rag_require_index`` 决定（组合根判断）。
        这里**不抛异常**：只读挂载失败不该让 Web 进程连健康检查都起不来，
        但也绝不假装就绪 —— degraded 会写日志并让 /ready 的 milvus 探针暴露。
        """
        stats = RagStats()
        start = time.perf_counter()
        with trace_span("retrieval.mount_readonly") as span:
            try:
                rows = self.vector_store.all_texts()
                chunks = [
                    Chunk(id=doc_id, text=text, metadata=metadata or {})
                    for doc_id, text, metadata in rows
                ]
                stats.chunks = len(chunks)
                stats.files = len(
                    {c.metadata.get("file_name", "") for c in chunks if c.metadata.get("file_name")}
                )
                span.set_attribute("chunk_count", len(chunks))
                self._rebuild_bm25(chunks)
                if not chunks:
                    stats.degraded = True
                    stats.error = "向量集合为空或不可达（请先运行灌库 Job）"
                    self._degraded = True
                else:
                    self._ready = True
                    self._degraded = False
            except Exception as exc:
                stats.degraded = True
                stats.error = f"{type(exc).__name__}: {exc}"
                self._degraded = True
                span.set_error(stats.error)

        stats.duration_ms = (time.perf_counter() - start) * 1000
        return stats

    def pending_changes(self) -> List[str]:
        """知识库文件相对上次灌库清单的改动（只读，不写清单）。

        只读实例靠它发现「改了知识库却忘了跑灌库 Job」——不然用户会拿到旧答案，
        而 /health 一直是 ok。没有清单时返回全部文件（等于"从未灌过库"）。
        只比内容哈希，不做分块（启动期开销要小）。
        """
        try:
            current = build_manifest(self.knowledge_dir, {})
            old = load_manifest(self.manifest_path)
            if old.is_empty():
                return sorted(current.files)
            diff = old.diff(current.files)
            return sorted(set(diff["update"]) | set(diff["remove"]))
        except Exception:
            return []

    def _rebuild_bm25(self, chunks: Optional[List[Chunk]] = None) -> None:
        """重建稀疏索引

        优先用本地块集（准确、立即可用）；没有时才回退到从向量库取回。
        """
        self.bm25 = BM25Index()
        if chunks is None:
            chunks = [
                Chunk(id=doc_id, text=text, metadata=metadata)
                for doc_id, text, metadata in self.vector_store.all_texts()
            ]
        for chunk in chunks:
            if chunk.metadata.get("file_name") in self.excluded_files:
                continue
            self.bm25.add(chunk.id, chunk.text, chunk.metadata)

    # ---------- 检索 ----------

    def _retriever(self) -> HybridRetriever:
        async def embed_fn(text: str) -> List[float]:
            return await self.embedding_model.embed(text)

        return HybridRetriever(
            vector_store=self.vector_store,
            bm25_index=self.bm25,
            embed_fn=embed_fn,
            dense_top_k=self.dense_top_k,
            sparse_top_k=self.sparse_top_k,
            dense_min_score=self.dense_min_score,
            metadata_filter=lambda m: m.get("file_name") not in self.excluded_files,
        )

    async def retrieve(self, query: str, top_k: int = 2) -> List[HybridHit]:
        """混合检索"""
        if self._degraded:
            return []
        with trace_span("retrieval.search") as span:
            span.set_attribute("query", query)
            try:
                hits = await self._retriever().search(query, top_k=top_k)
            except Exception as exc:
                span.set_error(f"{type(exc).__name__}: {exc}")
                self._degraded = True
                return []

            span.set_attribute("hit_count", len(hits))
            span.set_attribute(
                "hits",
                [
                    {
                        "score": round(h.score, 5),
                        "dense_rank": h.dense_rank,
                        "dense_score": h.dense_score,
                        "sparse_rank": h.sparse_rank,
                        "sparse_score": h.sparse_score,
                        "heading_path": h.metadata.get("heading_path", ""),
                        "file_name": h.metadata.get("file_name", ""),
                    }
                    for h in hits
                ],
            )
            if not hits:
                span.set_attribute("no_hit", True)
            return hits

    @property
    def ready(self) -> bool:
        return self._ready and not self._degraded

    @property
    def degraded(self) -> bool:
        return self._degraded
