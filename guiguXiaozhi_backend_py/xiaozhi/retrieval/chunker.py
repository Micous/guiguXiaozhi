"""分块 —— RAG 的第二道工序。

与"按空行盲切"相比，生产可用的分块要满足：
  * 层级感知：同一标题下的内容归为一块，块内带标题路径（检索命中后模型知道语境）
  * token 预算：超长块按中文标点切，带 overlap，避免语义在边界断裂
  * 表格 / 代码块不拆
  * 块 ID 稳定：sha256(file_name + heading_path + text)，同一内容永远同一 ID
    —— 这是索引幂等的基础（Java 版用随机 UUID，重复灌库会堆重复数据）
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from xiaozhi.retrieval.parser import Section, content_hash

# 中文句子边界：句末标点之后，或行尾（换行本身必须保留，
# 否则列表项会被粘成一行 —— "- 王笠…神经介入。- 崔丽英…" 就没法再按行切了）
SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？；!?;])|(?<=\n)")

# 列表项（一行一条）
LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]\s+|\d+[.、]\s*)")

# 中文里 1 个字 ≈ 0.6 token（经验值），用于估算
CHARS_PER_TOKEN = 1.7


def estimate_tokens(text: str) -> int:
    """估算 token 数（中文按字数折算）"""
    if not text:
        return 0
    ascii_chars = sum(1 for c in text if ord(c) < 128)
    cjk_chars = len(text) - ascii_chars
    return int(ascii_chars / 4 + cjk_chars / CHARS_PER_TOKEN) + 1


@dataclass
class Chunk:
    """一个向量块"""

    id: str
    text: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    token_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "metadata": self.metadata,
            "token_count": self.token_count,
        }


def chunk_id(file_name: str, heading_path: str, text: str) -> str:
    """稳定的块 ID —— 同样的内容永远得到同样的 ID"""
    raw = f"{file_name}||{heading_path}||{text}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _split_by_sentence(text: str, max_tokens: int, overlap_tokens: int) -> List[str]:
    """按句子切分超长文本，带 overlap。

    要点：overlap 的额度必须**小于** max_tokens（否则回溯会把整段都吞进重叠区，
    每个块都从同一句开始、且都超预算）。这里取 min(overlap, max_tokens // 3)。
    """
    sentences = [s for s in SENTENCE_SPLIT_RE.split(text) if s and s.strip()]
    if not sentences:
        return [text]

    # 重叠额度：不能超过块预算的三分之一
    overlap_budget = max(0, min(overlap_tokens, max_tokens // 3))

    chunks: List[str] = []
    current: List[str] = []
    current_tokens = 0

    for sentence in sentences:
        sentence_tokens = estimate_tokens(sentence)
        if current and current_tokens + sentence_tokens > max_tokens:
            chunks.append("".join(current))
            # 回溯构造 overlap（从后往前取，但总量不超过 overlap_budget）
            overlap: List[str] = []
            overlap_used = 0
            for prev in reversed(current):
                t = estimate_tokens(prev)
                if overlap_used + t > overlap_budget:
                    break
                overlap.insert(0, prev)
                overlap_used += t
            current = list(overlap)
            current_tokens = overlap_used
        current.append(sentence)
        current_tokens += sentence_tokens

    if current:
        chunks.append("".join(current))
    return chunks


class Chunker:
    """层级感知分块器"""

    def __init__(
        self,
        max_tokens: int = 500,
        overlap_tokens: int = 80,
        include_heading_path: bool = True,
        merge_same_path: bool = False,
    ) -> None:
        self.max_tokens = max_tokens
        self.overlap_tokens = overlap_tokens
        self.include_heading_path = include_heading_path
        # 是否把同一标题路径下的多个小节合并成一块。
        # 默认 False —— 段落级粒度的向量更"纯"，实测合并后语义被稀释
        # （"抽血需要空腹吗"的相似度从 0.74 掉到 0.68）。
        self.merge_same_path = merge_same_path

    def chunk(self, sections: Iterable[Section]) -> List[Chunk]:
        """把解析出的小节切成向量块"""
        # 1) 按标题路径归组，合并同一路径下的碎片
        grouped: Dict[str, Dict[str, Any]] = {}
        order: List[str] = []
        for section in sections:
            key = section.path_str
            if key not in grouped:
                grouped[key] = {
                    "file_name": section.file_name,
                    "heading_path": section.heading_path,
                    "heading": section.heading,
                    "parts": [],
                    "start_line": section.start_line,
                }
                order.append(key)
            grouped[key]["parts"].append(section.content)

        chunks: List[Chunk] = []
        for key in order:
            group = grouped[key]
            if self.merge_same_path:
                body = "\n\n".join(p for p in group["parts"] if p.strip()).strip()
            else:
                # 不合并：每个小节独立成块
                body = "\n\n".join(p for p in group["parts"] if p.strip()).strip()
            if not body:
                continue

            file_name = group["file_name"]
            heading_path = group["heading_path"]
            path_str = " / ".join(heading_path)

            # 2) 超长块按句子切。
            #    注意：标题路径前缀也会占 token，所以要先把前缀的额度从预算里扣掉，
            #    否则加上前缀后块会超预算（实测 131 > 120）。
            prefix = ""
            if self.include_heading_path and heading_path:
                prefix = f"# {path_str}\n"
            budget = max(64, self.max_tokens - estimate_tokens(prefix))

            pieces = self._split_oversized(body, budget)
            for piece in pieces:
                text = prefix + piece
                cid = chunk_id(file_name, path_str, text)
                metadata: Dict[str, Any] = {
                    "file_name": file_name,
                    "heading": group["heading"],
                    "heading_path": path_str,
                    "section": heading_path[-1] if heading_path else "",
                    "start_line": group["start_line"],
                    "content_hash": content_hash(piece),
                    "chunk_index": len(chunks),
                }
                chunks.append(
                    Chunk(
                        id=cid,
                        text=text,
                        metadata=metadata,
                        token_count=estimate_tokens(text),
                    )
                )
        return chunks

    # 注：曾尝试"列表按项切分"，把"症状→科室"清单拆成单条块。
    # 实测召回率反而从 9/10 降到 7/10 —— 单条块丢失了同类项之间的共现语境，
    # 例如"头痛"命中时不再带出"头晕/呕吐"等同列症状。故保留整块策略。

    def _split_oversized(self, text: str, budget: Optional[int] = None) -> List[str]:
        """超长文本再切；表格与代码块不拆"""
        limit = budget if budget is not None else self.max_tokens
        if estimate_tokens(text) <= limit:
            return [text]
        # 表格 / 代码块整体保留
        if text.lstrip().startswith("```") or text.lstrip().startswith("|"):
            return [text]
        return _split_by_sentence(text, limit, self.overlap_tokens)
