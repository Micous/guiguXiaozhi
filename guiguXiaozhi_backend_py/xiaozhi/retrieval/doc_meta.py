"""知识库条目的来源与审核元数据 —— Phase 7 医疗合规要求。

**为什么医疗知识库必须记来源与审核人**：

    审计 P1-07 要求"知识库内容可溯源"。医院场景下，一条"抽血需空腹"和一条
    "布洛芬不能和××同服"的可信度完全不同；出事时第一个问题是"这条内容
    谁写的、谁审的、依据是什么、什么时候到期"。没有这四个字段，
    整个 RAG 的回答在法律上都不可举证。

**为什么用 frontmatter 而不是外挂 JSON 清单**：

    元数据必须跟着内容走。外挂清单在"改文件忘了改清单"时会给出错误的
    来源（比没有更糟）。frontmatter 与正文同一个文件、同一次提交、
    同一个 diff，评审时一眼能看出来源变了。

格式（markdown 头部，`---` 包裹，逐行 `key: value`）：

    ---
    source: 北京协和医院官网 · 门诊指南（2026-10 版）
    reviewer: 医务处 张××
    reviewed_at: 2026-11-01
    expires_at: 2027-11-01
    version: 2026-10
    ---

只解析 `key: value`，不引入 YAML 依赖：这几个字段是给人和审计看的，
不需要嵌套结构；用完整 YAML 反而会让"缩进出错"变成静默解析失败。
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

# 合规必需字段（缺失即"不可溯源"）
REQUIRED_FIELDS: Tuple[str, ...] = ("source", "reviewer", "reviewed_at", "expires_at")

# 允许进入向量库 metadata 的字段（白名单：避免任意 frontmatter 键污染检索）
STORABLE_FIELDS: Tuple[str, ...] = REQUIRED_FIELDS + ("version", "department")

_DATE_FORMAT = "%Y-%m-%d"


def parse_frontmatter(text: str) -> Tuple[Dict[str, str], str]:
    """拆出 frontmatter 与正文。无 frontmatter 时返回 ({}, 原文)。

    只有**文件开头**的 `---` 块才算 frontmatter（正文里的分隔线不算）。
    """
    if not text.startswith("---"):
        return {}, text
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text
    meta: Dict[str, str] = {}
    end = None
    for idx in range(1, len(lines)):
        line = lines[idx]
        if line.strip() == "---":
            end = idx
            break
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            meta[key] = value
    if end is None:
        # 没有闭合的 `---`：当作没有 frontmatter，正文原样返回
        return {}, text
    body = "\n".join(lines[end + 1 :])
    return meta, body


def _parse_date(value: str) -> Optional[date]:
    try:
        return datetime.strptime(value.strip(), _DATE_FORMAT).date()
    except (ValueError, AttributeError):
        return None


def validate_doc_meta(
    meta: Dict[str, str], *, today: Optional[date] = None
) -> List[str]:
    """校验一个文档的 frontmatter，返回问题列表（空 = 合规）。

    检查三类问题，**分开报**（缺失 / 格式错 / 已过期）：
    它们对应完全不同的修复动作，混成一句会让人只改一半。
    """
    problems: List[str] = []
    for field in REQUIRED_FIELDS:
        if not str(meta.get(field, "")).strip():
            problems.append(f"缺少 {field}")
    for field in ("reviewed_at", "expires_at"):
        value = str(meta.get(field, "")).strip()
        if value and _parse_date(value) is None:
            problems.append(f"{field} 格式应为 YYYY-MM-DD（当前 {value!r}）")

    expires = _parse_date(str(meta.get("expires_at", "")))
    if expires is not None:
        now = today or date.today()
        if expires < now:
            problems.append(f"已过失效日期（expires_at={expires.isoformat()}）")

    reviewed = _parse_date(str(meta.get("reviewed_at", "")))
    if reviewed is not None and expires is not None and expires < reviewed:
        problems.append("expires_at 早于 reviewed_at（时间顺序错误）")
    return problems


def storable_meta(meta: Dict[str, str]) -> Dict[str, str]:
    """挑出允许写进向量库元数据的字段（白名单 + 去空值）。"""
    return {
        key: str(meta[key]).strip()
        for key in STORABLE_FIELDS
        if key in meta and str(meta[key]).strip()
    }


def read_doc_meta(path: Path) -> Dict[str, str]:
    """读取单个知识库文件的 frontmatter（不存在/解析失败返回空字典）。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    meta, _ = parse_frontmatter(text)
    return meta


def collect_meta_issues(
    directory: Path,
    *,
    patterns: Iterable[str] = ("*.md", "*.txt"),
    today: Optional[date] = None,
) -> List[Dict[str, Any]]:
    """扫描目录下全部知识库文件的元数据问题（供灌库脚本与只读挂载自检）。

    返回 ``[{"file": 文件名, "problems": [...]}]``，只含有问题的文件。
    """
    issues: List[Dict[str, Any]] = []
    files: List[Path] = []
    for pattern in patterns:
        files.extend(sorted(directory.glob(pattern)))
    for path in sorted(set(files)):
        if path.suffix.lower() != ".md":
            # 纯文本知识文件没有 frontmatter 约定，跳过（不制造噪音）
            continue
        problems = validate_doc_meta(read_doc_meta(path), today=today)
        if problems:
            issues.append({"file": path.name, "problems": problems})
    return issues
