"""RAG 检索评估 —— 对比"纯向量"与"混合检索"的召回质量。

用法：
    python scripts/eval_rag.py                     # 用内存索引评估（不依赖 Milvus）
    python scripts/eval_rag.py --use-milvus         # 用真实 Milvus 评估
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from xiaozhi.models.ollama import OllamaEmbeddingModel  # noqa: E402
from xiaozhi.retrieval.bm25 import BM25Index  # noqa: E402
from xiaozhi.retrieval.chunker import Chunker  # noqa: E402
from xiaozhi.retrieval.hybrid import HybridRetriever  # noqa: E402
from xiaozhi.retrieval.parser import parse_directory  # noqa: E402
from xiaozhi.retrieval.vector_store import SearchHit, VectorStore  # noqa: E402

EVAL_FILE = ROOT / "tests" / "eval" / "rag_eval.json"


class InMemoryStore(VectorStore):
    """内存向量库（评估用，不依赖 Milvus）"""

    def __init__(self, chunks, vectors):
        self._chunks = chunks
        self._vectors = vectors
        self._by_id = {c.id: (c, v) for c, v in zip(chunks, vectors)}

    def ensure_collection(self, dimension): return "mem"
    def upsert(self, chunks): return 0
    def delete_by_file(self, file_name): return 0
    def flush(self): return None

    def count(self): return len(self._chunks)

    def search(self, vector, top_k, filter_expr=None, output_fields=None):
        scored = []
        for cid, (chunk, vec) in self._by_id.items():
            score = sum(a * b for a, b in zip(vector, vec))
            scored.append(SearchHit(cid, chunk.text, score, chunk.metadata))
        scored.sort(key=lambda h: h.score, reverse=True)
        return scored[:top_k]

    def all_texts(self):
        return [(c.id, c.text, c.metadata) for c in self._chunks]


async def evaluate(use_milvus: bool = False) -> int:
    cases = json.loads(EVAL_FILE.read_text(encoding="utf-8"))["cases"]
    emb = OllamaEmbeddingModel()

    sections = parse_directory(ROOT / "xiaozhi" / "knowledge")
    chunks = Chunker().chunk(sections)
    texts = [c.text for c in chunks]
    vectors = await emb.embed_batch(texts)

    if use_milvus:
        from pymilvus import MilvusClient

        client = MilvusClient(uri="http://localhost:19530")
        collection = "xiaozhi_knowledge_py_v2"
        client.load_collection(collection)
        store: VectorStore = _MilvusStore(client, collection)
    else:
        store = InMemoryStore(chunks, vectors)

    bm25 = BM25Index()
    for chunk in chunks:
        bm25.add(chunk.id, chunk.text, chunk.metadata)

    async def embed_fn(text: str):
        return await emb.embed(text)

    hybrid = HybridRetriever(
        vector_store=store, bm25_index=bm25, embed_fn=embed_fn, dense_min_score=0.35
    )

    dense_ok = hybrid_ok = 0
    print(f"\n{'查询':<32} {'纯向量':>8} {'混合':>8}   命中内容")
    print("-" * 100)
    for case in cases:
        expect = case["expect"]
        dense_hits = store.search(await emb.embed(case["query"]), top_k=3)
        dense_top = dense_hits[0].text if dense_hits else ""
        dense_hit = any(w in dense_top for w in expect)
        dense_ok += dense_hit

        merged = await hybrid.search(case["query"], top_k=3)
        hybrid_top = merged[0].text if merged else ""
        hybrid_hit = any(w in hybrid_top for w in expect)
        hybrid_ok += hybrid_hit

        flag = "✅" if hybrid_hit else ("➖" if dense_hit else "❌")
        print(
            f"{case['query']:<32} {'✅' if dense_hit else '❌':>8} {'✅' if hybrid_hit else '❌':>8}   "
            f"{flag} {hybrid_top[:36].replace(chr(10), ' ')}"
        )

    total = len(cases)
    print("-" * 100)
    print(f"纯向量 Top-1 命中率: {dense_ok}/{total} = {dense_ok/total:.1%}")
    print(f"混合检索 Top-1 命中率: {hybrid_ok}/{total} = {hybrid_ok/total:.1%}")
    await emb.aclose()
    return 0


class _MilvusStore(VectorStore):
    """真实 Milvus（评估用适配器）"""

    def __init__(self, client, collection):
        self.client = client
        self.collection = collection

    def ensure_collection(self, dimension): return self.collection
    def upsert(self, chunks): return 0
    def delete_by_file(self, file_name): return 0
    def flush(self): return None
    def count(self): return int(self.client.get_collection_stats(self.collection).get("row_count", 0))

    def search(self, vector, top_k, filter_expr=None, output_fields=None):
        res = self.client.search(
            self.collection, data=[vector], limit=top_k,
            filter=filter_expr or "", output_fields=output_fields or ["text", "metadata"],
        )
        return [
            SearchHit(str(r.get("id")), (r.get("entity") or {}).get("text", ""),
                      float(r.get("distance", 0.0)), (r.get("entity") or {}).get("metadata") or {})
            for r in (res[0] if res else [])
        ]

    def all_texts(self):
        rows = self.client.query(self.collection, filter="", limit=16384,
                                 output_fields=["text", "metadata"])
        return [(str(r.get("id")), r.get("text", ""), r.get("metadata") or {}) for r in rows]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--use-milvus", action="store_true")
    args = parser.parse_args()
    asyncio.run(evaluate(args.use_milvus))
