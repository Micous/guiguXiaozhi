"""混合检索 —— 稠密（向量）+ 稀疏（BM25）→ RRF 融合。

纯向量检索的短板：
  "抽血需要空腹吗" 在向量检索里排第一的是"实名制就诊"（相似度 0.80），
  真正含"抽血/空腹"的段落排不进去；
BM25 恰好相反 —— 关键词型问题它排第一，语义型问题它排不进去。
RRF（Reciprocal Rank Fusion）按**排名**而非分数融合，两路互补且不需要分数归一化。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence

from xiaozhi.retrieval.bm25 import BM25Index, ScoredDoc
from xiaozhi.retrieval.vector_store import SearchHit, VectorStore

logger = logging.getLogger(__name__)


@dataclass
class HybridHit:
    """融合后的检索结果"""

    id: str
    text: str
    score: float
    metadata: Dict[str, Any]
    dense_rank: Optional[int] = None
    dense_score: Optional[float] = None
    sparse_rank: Optional[int] = None
    sparse_score: Optional[float] = None
    source: str = "hybrid"


class HybridRetriever:
    """稠密 + 稀疏混合检索"""

    RRF_K = 60  # RRF 常数，业界常用 60

    def __init__(
        self,
        vector_store: VectorStore,
        bm25_index: BM25Index,
        embed_fn: Callable[[str], Any],
        dense_top_k: int = 20,
        sparse_top_k: int = 20,
        dense_min_score: float = 0.0,
        min_rrf_score: float = 0.0,
        metadata_filter: Optional[Callable[[Dict[str, Any]], bool]] = None,
    ) -> None:
        self.vector_store = vector_store
        self.bm25_index = bm25_index
        self.embed_fn = embed_fn
        self.dense_top_k = dense_top_k
        self.sparse_top_k = sparse_top_k
        self.dense_min_score = dense_min_score
        self.min_rrf_score = min_rrf_score
        self.metadata_filter = metadata_filter

    def _passes_filter(self, metadata: Dict[str, Any]) -> bool:
        if self.metadata_filter is None:
            return True
        try:
            return bool(self.metadata_filter(metadata))
        except Exception:
            return True

    async def search(self, query: str, top_k: int = 2) -> List[HybridHit]:
        """混合检索并融合（向量化是异步的，这里必须 async）"""
        merged: Dict[str, HybridHit] = {}

        # ---- 稠密检索 ----
        try:
            vector = await self.embed_fn(query)
            dense_hits = self.vector_store.search(vector, top_k=self.dense_top_k)
        except Exception as exc:
            # 稠密检索失败不能静默 —— 否则"混合检索"悄悄退化成纯 BM25，很难发现
            dense_hits = []
            logger.warning("稠密检索失败，已降级为纯稀疏检索: %s: %s", type(exc).__name__, exc)

        for rank, hit in enumerate(dense_hits, start=1):
            if hit.score < self.dense_min_score:
                continue
            if not self._passes_filter(hit.metadata):
                continue
            merged[hit.id] = HybridHit(
                id=hit.id,
                text=hit.text,
                score=0.0,
                metadata=hit.metadata,
                dense_rank=rank,
                dense_score=hit.score,
                source="dense",
            )

        # ---- 稀疏检索 ----
        sparse_hits: Sequence[ScoredDoc] = self.bm25_index.search(query, top_k=self.sparse_top_k)
        for rank, hit in enumerate(sparse_hits, start=1):
            if not self._passes_filter(hit.metadata):
                continue
            existing = merged.get(hit.id)
            if existing is None:
                merged[hit.id] = HybridHit(
                    id=hit.id,
                    text=hit.text,
                    score=0.0,
                    metadata=hit.metadata,
                    sparse_rank=rank,
                    sparse_score=hit.score,
                    source="sparse",
                )
            else:
                existing.sparse_rank = rank
                existing.sparse_score = hit.score

        # ---- RRF 融合 ----
        for hit in merged.values():
            score = 0.0
            if hit.dense_rank:
                score += 1.0 / (self.RRF_K + hit.dense_rank)
            if hit.sparse_rank:
                score += 1.0 / (self.RRF_K + hit.sparse_rank)
            hit.score = score

        results = [h for h in merged.values() if h.score >= self.min_rrf_score]
        results.sort(key=lambda h: h.score, reverse=True)
        return results[:top_k]
