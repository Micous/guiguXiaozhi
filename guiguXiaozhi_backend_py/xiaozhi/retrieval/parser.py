"""Markdown 结构化解析 —— RAG 的第一道工序。

生产要求：
  * 识别标题层级，维护标题路径（chunk 带上"这是哪一节"，检索命中后模型才知道语境）
  * 表格、代码块视为不可分割单元
  * 元数据齐全：file_name / heading_path / section / start_line / content_hash
  * 编码健壮：BOM 剥离、非法字节替换，单文件失败不影响整体
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from xiaozhi.retrieval.doc_meta import parse_frontmatter

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
FENCE_RE = re.compile(r"^\s*```")
FRONTMATTER_RE = re.compile(r"^---\s*\n.*?\n---\s*\n", re.DOTALL)


@dataclass
class Section:
    """一个逻辑小节（标题 + 正文）"""

    heading: str = ""
    heading_path: List[str] = field(default_factory=list)
    content: str = ""
    level: int = 0
    start_line: int = 1
    file_name: str = ""
    # 文档级 frontmatter（来源/审核人/版本/失效日期）—— Phase 7 合规要求。
    # 挂在每个 Section 上而不是返回二元组，避免改动所有调用方的签名。
    doc_meta: Dict[str, str] = field(default_factory=dict)

    @property
    def path_str(self) -> str:
        return " / ".join(self.heading_path)

    def to_metadata(self, file_name: str = "") -> Dict[str, Any]:
        return {
            "file_name": file_name or self.file_name,
            "heading": self.heading,
            "heading_path": self.path_str,
            "section": self.heading_path[-1] if self.heading_path else "",
            "start_line": self.start_line,
            "content_hash": content_hash(self.content),
        }


def content_hash(text: str) -> str:
    """内容哈希 —— 用于块 ID 与增量更新判断"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def read_text(path: Path) -> str:
    """健壮读取文本文件：剥离 BOM、非法字节替换"""
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    return raw.decode("utf-8", errors="replace")


def _split_blocks(text: str) -> List[Dict[str, Any]]:
    """把 markdown 正文切成 block 序列（保留行号）"""
    lines = text.split("\n")
    blocks: List[Dict[str, Any]] = []
    buf: List[str] = []
    buf_start = 1
    in_fence = False
    in_table = False
    idx = 0

    def flush(end_line: int) -> None:
        nonlocal buf, buf_start
        content = "\n".join(buf).strip()
        if content:
            blocks.append({"text": content, "start_line": buf_start, "end_line": end_line})
        buf = []
        buf_start = end_line + 1

    while idx < len(lines):
        line = lines[idx]
        line_no = idx + 1

        if not buf:
            buf_start = line_no

        # 代码块：整块保留
        if FENCE_RE.match(line):
            if buf and not in_fence:
                flush(line_no - 1)
            in_fence = not in_fence
            buf.append(line)
            idx += 1
            continue

        # 标题：独立成块
        if not in_fence and HEADING_RE.match(line):
            flush(line_no - 1)
            buf.append(line)
            flush(line_no)
            idx += 1
            continue

        # 表格：整块保留（连续的表格行合并）
        is_table_row = bool(TABLE_ROW_RE.match(line))
        if is_table_row and not in_fence:
            if not in_table:
                if buf:
                    flush(line_no - 1)
                in_table = True
                buf_start = line_no
            buf.append(line)
            idx += 1
            continue
        if in_table and not is_table_row:
            in_table = False
            flush(line_no - 1)
            continue

        # 空行：段落边界
        if not line.strip() and not in_fence:
            flush(line_no - 1)
            idx += 1
            continue

        buf.append(line)
        idx += 1

    flush(len(lines))
    return blocks


def parse_markdown(
    text: str, file_name: str, doc_meta: Optional[Dict[str, str]] = None
) -> List[Section]:
    """把 markdown 解析成带标题路径的小节列表。

    ``doc_meta``：文档级 frontmatter（来源/审核人/失效日期），会原样挂到
    每个 Section 上，最终进 chunk metadata —— 检索命中后模型与审计都能
    知道这段内容出自哪份文件、谁审过、什么时候到期。
    """
    text = FRONTMATTER_RE.sub("", text, count=1)
    meta = dict(doc_meta or {})
    blocks = _split_blocks(text)

    sections: List[Section] = []
    heading_stack: List[tuple[int, str]] = []  # (level, title)

    for block in blocks:
        content = block["text"]
        heading_match = HEADING_RE.match(content)
        if heading_match:
            level = len(heading_match.group(1))
            title = heading_match.group(2).strip()
            # 维护标题栈
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, title))
            # 标题不单独成块 —— 它是后续正文的上下文，
            # 单独切出来会让"标题块"与"正文块"分离，检索时命中标题却拿不到正文
            continue

        if not heading_stack:
            # 文档前言（首个标题之前）
            sections.append(
                Section(
                    heading="",
                    heading_path=[],
                    content=content,
                    level=0,
                    start_line=block["start_line"],
                    file_name=file_name,
                    doc_meta=meta,
                )
            )
            continue

        sections.append(
            Section(
                heading=heading_stack[-1][1],
                heading_path=[t for _, t in heading_stack],
                content=content,
                level=heading_stack[-1][0],
                start_line=block["start_line"],
                file_name=file_name,
                doc_meta=meta,
            )
        )

    return sections


def parse_file(path: Path) -> List[Section]:
    """解析单个文件（.md / .txt）"""
    file_name = path.name
    text = read_text(path)
    if path.suffix.lower() == ".md":
        meta, body = parse_frontmatter(text)
        return parse_markdown(body, file_name, doc_meta=meta)
    # 纯文本：按空行分段
    sections: List[Section] = []
    for i, para in enumerate(p for p in text.split("\n\n") if p.strip()):
        sections.append(
            Section(
                heading="",
                heading_path=[],
                content=para.strip(),
                level=0,
                start_line=i + 1,
                file_name=file_name,
            )
        )
    return sections


def parse_directory(
    directory: Path, patterns: Iterable[str] = ("*.md", "*.txt")
) -> List[Section]:
    """解析目录下所有知识库文件（单文件失败不影响整体）"""
    sections: List[Section] = []
    files: List[Path] = []
    for pattern in patterns:
        files.extend(sorted(directory.glob(pattern)))

    for file_path in sorted(set(files)):
        try:
            sections.extend(parse_file(file_path))
        except Exception as exc:  # 单文件失败只告警
            print(f"[RAG] 解析文件失败 {file_path}: {type(exc).__name__}: {exc}")
    return sections
