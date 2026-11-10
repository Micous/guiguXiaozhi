"""BM25 稀疏检索 —— 纯 Python 实现，不引入第三方库。

为什么需要它：纯向量检索对**关键词型**问题召回不稳。
实测 "抽血需要空腹吗" 在向量检索里排第一的是"实名制就诊"（0.80），
而真正含"抽血/空腹"的段落排不进去。稀疏检索正好补这一块。

中文分词：字 + 二元组（bigram），无需词典，效果接近
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

TOKEN_RE = re.compile(r"[a-zA-Z0-9]+")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def tokenize(text: str) -> List[str]:
    """混合分词：英文/数字按词，中文按字 + 二元组"""
    text = (text or "").lower()
    tokens: List[str] = []

    # 英文与数字
    for word in TOKEN_RE.findall(text):
        tokens.append(word)

    # 中文：连续汉字段
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        tokens.extend(list(run))                       # 单字
        tokens.extend(run[i : i + 2] for i in range(len(run) - 1))  # 二元组

    return tokens


@dataclass
class ScoredDoc:
    """带分数的文档"""

    id: str
    text: str
    score: float
    metadata: Dict[str, object]


class BM25Index:
    """BM25 倒排索引"""

    def __init__(self, k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self._docs: Dict[str, str] = {}
        self._metadata: Dict[str, Dict[str, object]] = {}
        self._tokens: Dict[str, List[str]] = {}
        self._df: Counter = Counter()
        self._avg_len: float = 0.0

    def __len__(self) -> int:
        return len(self._docs)

    def add(self, doc_id: str, text: str, metadata: Optional[Dict[str, object]] = None) -> None:
        """加入/覆盖一篇文档"""
        if doc_id in self._docs:
            self.remove(doc_id)
        tokens = tokenize(text)
        self._docs[doc_id] = text
        self._metadata[doc_id] = dict(metadata or {})
        self._tokens[doc_id] = tokens
        for term in set(tokens):
            self._df[term] += 1
        self._avg_len = (
            sum(len(t) for t in self._tokens.values()) / len(self._tokens)
            if self._tokens
            else 0.0
        )

    def remove(self, doc_id: str) -> None:
        """移除文档（保持 df 与 avg_len 一致）"""
        tokens = self._tokens.pop(doc_id, None)
        if tokens is None:
            return
        for term in set(tokens):
            self._df[term] -= 1
            if self._df[term] <= 0:
                del self._df[term]
        self._docs.pop(doc_id, None)
        self._metadata.pop(doc_id, None)
        self._avg_len = (
            sum(len(t) for t in self._tokens.values()) / len(self._tokens)
            if self._tokens
            else 0.0
        )

    def search(self, query: str, top_k: int = 20) -> List[ScoredDoc]:
        """BM25 检索"""
        if not self._docs:
            return []
        query_tokens = tokenize(query)
        if not query_tokens:
            return []

        total = len(self._docs)
        scores: Dict[str, float] = {}
        for term in query_tokens:
            df = self._df.get(term, 0)
            if df == 0:
                continue
            idf = math.log(1 + (total - df + 0.5) / (df + 0.5))
            for doc_id, tokens in self._tokens.items():
                tf = tokens.count(term)
                if tf == 0:
                    continue
                length = len(tokens) or 1
                denom = tf + self.k1 * (1 - self.b + self.b * length / (self._avg_len or 1))
                scores[doc_id] = scores.get(doc_id, 0.0) + idf * (tf * (self.k1 + 1)) / denom

        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:top_k]
        return [
            ScoredDoc(
                id=doc_id,
                text=self._docs[doc_id],
                score=score,
                metadata=self._metadata.get(doc_id, {}),
            )
            for doc_id, score in ranked
        ]
