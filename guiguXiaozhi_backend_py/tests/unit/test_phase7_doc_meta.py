"""Phase 7 知识条目元数据（来源/审核人/版本/失效日期）测试。

两层价值：
  1. **合规**：医疗内容必须可溯源 —— 缺 source/reviewer 的条目不能算"已审核"；
  2. **不过期**：医院指南到期必须复审，而不是"上线后一直用 2021 年的版本"。

另外钉住一条容易被忽略的事实：frontmatter 必须在**分块之前**被剥离，
否则它会变成一块"元数据正文"被检索命中并塞进模型上下文。
"""

from __future__ import annotations

import shutil
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from xiaozhi.retrieval.chunker import Chunker  # noqa: E402
from xiaozhi.retrieval.doc_meta import (  # noqa: E402
    REQUIRED_FIELDS,
    collect_meta_issues,
    parse_frontmatter,
    storable_meta,
    validate_doc_meta,
)
from xiaozhi.retrieval.parser import parse_file, parse_markdown  # noqa: E402

TMP_DIR = Path(__file__).resolve().parents[1] / "_tmp_doc_meta"

GOOD = {
    "source": "北京协和医院官网 · 就医指南",
    "reviewer": "医务处 张××",
    "reviewed_at": "2026-11-01",
    "expires_at": "2027-11-01",
    "version": "2026-10",
}

GOOD_TEXT = """---
source: 北京协和医院官网 · 就医指南
reviewer: 医务处 张××
reviewed_at: 2026-11-01
expires_at: 2027-11-01
version: 2026-10
---

# 抽血检查

## 需要空腹吗

抽血检查一般需要空腹 8 小时。
"""


@pytest.fixture(scope="module", autouse=True)
def _clean_tmp():
    shutil.rmtree(TMP_DIR, ignore_errors=True)
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    yield
    shutil.rmtree(TMP_DIR, ignore_errors=True)


# ---------- frontmatter 解析 ----------


def test_parse_frontmatter_splits_meta_and_body():
    meta, body = parse_frontmatter(GOOD_TEXT)
    assert meta["reviewer"] == "医务处 张××"
    assert meta["expires_at"] == "2027-11-01"
    assert "---" not in body.split("\n")[0]
    assert body.lstrip().startswith("# 抽血检查")


def test_parse_frontmatter_absent_returns_empty():
    meta, body = parse_frontmatter("# 标题\n\n正文")
    assert meta == {}
    assert body == "# 标题\n\n正文"


def test_unclosed_frontmatter_is_not_swallowed():
    """没闭合的 `---` 不能把整个文件当元数据吃掉（否则正文全丢）。"""
    text = "---\nsource: x\n# 标题\n正文"
    meta, body = parse_frontmatter(text)
    assert meta == {}
    assert "正文" in body


def test_quoted_values_are_unquoted():
    meta, _ = parse_frontmatter('---\nsource: "官网"\n---\n正文')
    assert meta["source"] == "官网"


# ---------- 合规校验 ----------


def test_good_meta_has_no_problems():
    assert validate_doc_meta(GOOD) == []


@pytest.mark.parametrize("field", REQUIRED_FIELDS)
def test_missing_required_field_is_reported(field):
    meta = dict(GOOD)
    meta.pop(field)
    problems = validate_doc_meta(meta)
    assert any(field in p for p in problems), problems


def test_expired_entry_is_reported():
    """过期不是"以后再说"：医院指南到期必须复审。"""
    meta = dict(GOOD, expires_at="2026-01-01")
    problems = validate_doc_meta(meta, today=date(2026, 11, 12))
    assert any("失效日期" in p for p in problems)


def test_bad_date_format_is_reported():
    problems = validate_doc_meta(dict(GOOD, expires_at="2027/11/01"))
    assert any("YYYY-MM-DD" in p for p in problems)


def test_expires_before_reviewed_is_reported():
    meta = dict(GOOD, reviewed_at="2027-01-01", expires_at="2026-01-01")
    problems = validate_doc_meta(meta, today=date(2026, 11, 12))
    assert any("时间顺序" in p for p in problems)


def test_storable_meta_whitelists_fields():
    """任意 frontmatter 键不许进向量库元数据（避免污染检索/拖慢过滤）。"""
    meta = dict(GOOD, secret_token="should-not-be-stored", random="x")
    stored = storable_meta(meta)
    assert "secret_token" not in stored
    assert "random" not in stored
    assert stored["source"] == GOOD["source"]


# ---------- 端到端：解析 → 分块 ----------


def test_frontmatter_is_stripped_before_chunking():
    meta, body = parse_frontmatter(GOOD_TEXT)
    chunks = Chunker().chunk(parse_markdown(body, "t.md", doc_meta=meta))
    assert chunks, "应当切出块"
    for chunk in chunks:
        # 元数据不能变成正文被检索命中
        assert "reviewer:" not in chunk.text
        assert "expires_at:" not in chunk.text
        assert chunk.metadata["source"] == GOOD["source"]
        assert chunk.metadata["expires_at"] == GOOD["expires_at"]


def test_parse_file_reads_frontmatter_from_disk():
    path = TMP_DIR / "guide.md"
    path.write_text(GOOD_TEXT, encoding="utf-8")
    sections = parse_file(path)
    assert sections
    assert all(s.doc_meta.get("reviewer") == "医务处 张××" for s in sections)


# ---------- 目录扫描（灌库脚本 / 启动自检的入口）----------


def test_collect_meta_issues_reports_bad_files_only():
    (TMP_DIR / "good.md").write_text(GOOD_TEXT, encoding="utf-8")
    (TMP_DIR / "bad.md").write_text("# 没有元数据\n\n正文\n", encoding="utf-8")
    (TMP_DIR / "stale.md").write_text(
        GOOD_TEXT.replace("2027-11-01", "2026-01-01"), encoding="utf-8"
    )
    issues = collect_meta_issues(TMP_DIR, today=date(2026, 11, 12))
    names = {i["file"] for i in issues}
    assert names == {"bad.md", "stale.md"}
    stale = next(i for i in issues if i["file"] == "stale.md")
    assert any("失效日期" in p for p in stale["problems"])


def test_real_knowledge_base_is_compliant():
    """仓库里的知识库本身必须合规 —— 否则启动日志会天天喊告警。"""
    knowledge_dir = ROOT / "xiaozhi" / "knowledge"
    issues = collect_meta_issues(knowledge_dir)
    assert issues == [], f"知识库元数据不合规：{issues}"


def test_txt_files_are_not_flagged():
    """纯文本知识文件不遵循 frontmatter 约定，不制造噪音。"""
    (TMP_DIR / "note.txt").write_text("纯文本知识\n", encoding="utf-8")
    issues = collect_meta_issues(TMP_DIR, patterns=("*.txt",))
    assert issues == []
