"""RAG 单元测试：不依赖 Ollama / Milvus，全部用内存实现。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from xiaozhi.retrieval.bm25 import BM25Index, tokenize
from xiaozhi.retrieval.chunker import Chunker, chunk_id, estimate_tokens
from xiaozhi.retrieval.hybrid import HybridRetriever
from xiaozhi.retrieval.manifest import Manifest, FileEntry, build_manifest
from xiaozhi.retrieval.parser import parse_markdown, parse_directory
from xiaozhi.retrieval.vector_store import SearchHit, VectorStore


# ---------------- 解析 ----------------

MARKDOWN = """# 医院信息

## 医院概况
北京协和医院成立于1921年。

## 检查须知

抽血检查：需空腹 8-12 小时。

胃镜检查：需空腹 6-8 小时。

| 项目 | 说明 |
|---|---|
| 挂号 | 每日 16:00 放号 |
"""


def test_parse_keeps_heading_path():
    sections = parse_markdown(MARKDOWN, "医院信息.md")
    paths = [s.path_str for s in sections]
    assert "医院信息 / 医院概况" in paths
    assert "医院信息 / 检查须知" in paths


def test_heading_not_emitted_as_own_section():
    """标题不单独成块 —— 否则命中标题却拿不到正文"""
    sections = parse_markdown(MARKDOWN, "医院信息.md")
    for section in sections:
        assert section.content.strip() != section.heading.strip() or not section.heading


def test_table_kept_together():
    sections = parse_markdown(MARKDOWN, "医院信息.md")
    tables = [s for s in sections if "|" in s.content]
    assert tables, "表格应作为一个整体保留"
    assert any("挂号" in t.content for t in tables)


def test_file_name_preserved():
    sections = parse_directory(ROOT / "xiaozhi" / "knowledge")
    assert sections
    assert all(s.file_name for s in sections), "file_name 不能丢"
    names = {s.file_name for s in sections}
    assert "doctors.md" in names


# ---------------- 分块 ----------------

def test_chunk_ids_stable():
    """块 ID 必须稳定 —— 这是索引幂等的基础"""
    sections = parse_directory(ROOT / "xiaozhi" / "knowledge")
    first = [c.id for c in Chunker().chunk(sections)]
    second = [c.id for c in Chunker().chunk(parse_directory(ROOT / "xiaozhi" / "knowledge"))]
    assert first == second


def test_chunk_ids_unique():
    sections = parse_directory(ROOT / "xiaozhi" / "knowledge")
    chunks = Chunker().chunk(sections)
    assert len({c.id for c in chunks}) == len(chunks)


def test_chunk_id_changes_with_content():
    assert chunk_id("a.md", "x", "内容") != chunk_id("a.md", "x", "别的内容")
    assert chunk_id("a.md", "x", "内容") != chunk_id("b.md", "x", "内容")


def test_chunk_respects_token_budget():
    sections = parse_directory(ROOT / "xiaozhi" / "knowledge")
    chunks = Chunker(max_tokens=120).chunk(sections)
    # 表格允许超限，其余应受限
    for chunk in chunks:
        if not chunk.text.lstrip().startswith(("|", "```")):
            assert chunk.token_count <= 120 or "\n" not in chunk.text


def test_estimate_tokens_chinese():
    assert estimate_tokens("") == 0
    assert estimate_tokens("中文字符测试") > 0


# ---------------- BM25 ----------------

def test_tokenize_chinese_bigram():
    tokens = tokenize("抽血空腹")
    assert "抽血" in tokens
    assert "血空" in tokens


def test_bm25_ranks_keyword_match_first():
    index = BM25Index()
    index.add("a", "抽血检查：需空腹 8-12 小时")
    index.add("b", "实名制就诊：需携带身份证件")
    hits = index.search("抽血需要空腹吗", top_k=2)
    assert hits and hits[0].id == "a"


def test_bm25_empty_index():
    assert BM25Index().search("任意") == []


def test_bm25_remove_keeps_index_consistent():
    index = BM25Index()
    index.add("a", "抽血检查")
    index.add("b", "胃镜检查")
    index.remove("a")
    assert len(index) == 1
    assert [h.id for h in index.search("抽血", 5)] == []


# ---------------- 混合检索 ----------------

class _FakeStore(VectorStore):
    def __init__(self, hits):
        self._hits = hits

    def ensure_collection(self, dimension): return "fake"
    def upsert(self, chunks): return 0
    def delete_by_file(self, file_name): return 0
    def flush(self): return None
    def count(self, settle=False): return len(self._hits)
    def all_texts(self): return []

    def search(self, vector, top_k, filter_expr=None, output_fields=None):
        return self._hits[:top_k]


def test_hybrid_fusion_prefers_agreement():
    hits = [
        SearchHit("both", "抽血检查：需空腹", 0.7, {}),
        SearchHit("dense_only", "实名制就诊", 0.8, {}),
        SearchHit("sparse_only", "胃镜检查", 0.6, {}),
    ]
    index = BM25Index()
    index.add("both", "抽血检查：需空腹")
    index.add("dense_only", "实名制就诊")
    index.add("sparse_only", "胃镜检查")

    async def embed_fn(text):
        return [0.0]

    retriever = HybridRetriever(_FakeStore(hits), index, embed_fn)

    import asyncio
    results = asyncio.get_event_loop().run_until_complete(
        retriever.search("抽血需要空腹吗", top_k=3)
    )
    assert results[0].id == "both", "两路都排第一的块应融合得分最高"


def test_hybrid_applies_metadata_filter():
    hits = [SearchHit("x", "内容", 0.9, {"file_name": "doctors.md"})]
    index = BM25Index()
    index.add("x", "内容", {"file_name": "doctors.md"})

    async def embed_fn(text):
        return [0.0]

    retriever = HybridRetriever(
        _FakeStore(hits), index, embed_fn,
        metadata_filter=lambda m: m.get("file_name") != "doctors.md",
    )
    import asyncio
    results = asyncio.get_event_loop().run_until_complete(retriever.search("内容", top_k=3))
    assert results == []


# ---------------- 清单 ----------------

def test_manifest_diff():
    old = Manifest(files={"a.md": FileEntry("a.md", "hash1"), "gone.md": FileEntry("gone.md", "hash2")})
    new = Manifest(files={"a.md": FileEntry("a.md", "hash1"), "b.md": FileEntry("b.md", "hash3")})
    diff = old.diff(new.files)
    assert diff["update"] == ["b.md"]
    assert diff["remove"] == ["gone.md"]


def test_manifest_diff_detects_change():
    old = Manifest(files={"a.md": FileEntry("a.md", "hash1")})
    new = Manifest(files={"a.md": FileEntry("a.md", "hash_CHANGED")})
    assert old.diff(new.files)["update"] == ["a.md"]


def test_build_manifest_reads_real_knowledge():
    manifest = build_manifest(ROOT / "xiaozhi" / "knowledge", {})
    assert manifest.files
    assert all(e.content_hash for e in manifest.files.values())
