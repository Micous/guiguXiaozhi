#!/usr/bin/env python
"""知识库灌库 —— **单写者 Job**（Phase 6）。

为什么要把灌库从 Web 进程里拿出来：

  * 多实例部署时每个实例启动都灌一次库，N 个进程并发 upsert 同一集合 +
    互相覆盖进度清单（manifest），结果是半成品索引和"到底灌到哪一版"说不清；
  * 灌库是重操作（解析/分块/批量嵌入），把它塞进 lifespan 会让**重启变慢**
    且失败会被"降级 + 告警"吞掉，/health 依然 ok；
  * 索引更新应该是一次可审计、可回滚、可重跑的变更，而不是一个副作用。

用法（在 ``guiguXiaozhi_backend_py/`` 下）：

    # 增量灌库（默认；只重建改动过的文件）
    python scripts/ingest_knowledge.py

    # 全量重建到新版本集合 → 校验行数 → 原子切别名 → 清理旧版本
    python scripts/ingest_knowledge.py --force --keep 2

    # 只看会灌什么（不连 Milvus、不写任何东西）
    python scripts/ingest_knowledge.py --dry-run

    # 看有哪些版本、别名指向哪一版
    python scripts/ingest_knowledge.py --list

    # 回滚到上一版本（或 --version N 指定版本）
    python scripts/ingest_knowledge.py --rollback
    python scripts/ingest_knowledge.py --rollback --version 3

退出码（给 CI / CronJob 判定用）：
    0 成功；1 失败（配置/依赖/灌库异常）；3 已有灌库任务在跑（抢锁失败）；
    4 校验不通过（新版本行数不足，**别名未切换**，旧索引继续服务）。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Optional

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_LOCKED = 3
EXIT_VERIFY_FAILED = 4


class SingleWriterLock:
    """跨进程独占锁（fcntl.flock）。

    用文件锁而不是"查一下有没有别人在跑"：竞态窗口正是两个 CronJob 同时
    启动的那一秒。flock 是内核级排他，非阻塞获取，拿不到直接退出 3。
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("w")
        try:
            import fcntl
        except ImportError:  # pragma: no cover - Windows 开发机
            print("[warn] 当前平台不支持 fcntl，跳过单写者锁", file=sys.stderr)
            return True
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        self._handle.write(str(os.getpid()))
        self._handle.flush()
        return True

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            import fcntl

            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        self._handle.close()
        self._handle = None

    def __enter__(self) -> "SingleWriterLock":
        return self

    def __exit__(self, *exc_info) -> None:
        self.release()


def build_stack(settings):
    """装配灌库所需的最小依赖栈（不连 Mongo / MySQL）。"""
    from xiaozhi.models.ollama import OllamaEmbeddingModel
    from xiaozhi.retrieval.rag import RagService
    from xiaozhi.retrieval.vector_store import MilvusVectorStore

    store = MilvusVectorStore(
        uri=settings.milvus_uri,
        alias=settings.milvus_alias,
        metric_type=settings.milvus_metric_type,
        index_type=settings.milvus_index_type,
    )
    embedding = OllamaEmbeddingModel(
        base_url=settings.ollama_base_url,
        model=settings.ollama_embedding_model,
        timeout=settings.ollama_timeout,
    )
    manifest_path = Path(
        settings.manifest_path
        or (Path(settings.trace_persist_dir).parent / f"rag_manifest_{settings.milvus_alias}.json")
    )
    rag = RagService(
        vector_store=store,
        embedding_model=embedding,
        knowledge_dir=Path(settings.knowledge_dir),
        manifest_path=manifest_path,
        dense_top_k=settings.rag_dense_top_k,
        sparse_top_k=settings.rag_sparse_top_k,
        dense_min_score=settings.rag_dense_min_score,
        excluded_files=settings.rag_excluded_files,
    )
    return rag, store, embedding


def dry_run(settings) -> int:
    """只解析 + 分块 + 统计，不连任何外部服务。"""
    from xiaozhi.retrieval.chunker import Chunker
    from xiaozhi.retrieval.parser import parse_directory

    sections = parse_directory(Path(settings.knowledge_dir))
    chunks = Chunker().chunk(sections)
    counts = {}
    for chunk in chunks:
        name = chunk.metadata.get("file_name", "?")
        counts[name] = counts.get(name, 0) + 1
    print(f"[dry-run] 知识库：{settings.knowledge_dir}")
    print(f"[dry-run] 文件 {len(counts)} 个，块 {len(chunks)} 个")
    for name in sorted(counts):
        print(f"  - {name}: {counts[name]} 块")
    return EXIT_OK


def list_versions(store) -> int:
    try:
        versions = store.list_versions()
        current = store.current_version()
    except Exception as exc:
        print(f"[error] 无法读取 Milvus 集合：{type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_FAILED
    print(f"[list] 别名 {store.alias} → 版本 {current if current is not None else '（未设置）'}")
    if not versions:
        print("[list] 尚无版本化集合")
    for version in versions:
        mark = " ← 当前" if version == current else ""
        print(f"  - v{version}{mark}")
    return EXIT_OK


async def ingest(settings, *, force: bool, keep: int) -> int:
    rag, store, embedding = build_stack(settings)

    if not force:
        stats = await rag.bootstrap()
        if stats.degraded:
            print(f"[error] 增量灌库失败：{stats.error}", file=sys.stderr)
            return EXIT_FAILED
        print(
            f"[ok] 增量灌库完成：{stats.chunks} 块 / {stats.files} 文件，"
            f"更新 {len(stats.updated_files)} 个、删除 {len(stats.removed_files)} 个，"
            f"耗时 {stats.duration_ms:.0f}ms"
        )
        if stats.updated_files:
            print("  更新：" + ", ".join(stats.updated_files))
        return EXIT_OK

    # ---- 全量重建：写新版本 → 校验 → 原子切别名 ----
    old_version = None
    try:
        old_version = store.current_version()
        dimension = await embedding.ensure_dimension()
        new_collection = store.create_new_version(dimension)
    except Exception as exc:
        print(f"[error] 创建新版本集合失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_FAILED

    print(f"[info] 新版本集合：{new_collection}（旧版本 v{old_version} 保留）")
    store.pin_collection(new_collection)
    try:
        stats = await rag.bootstrap()
        if stats.degraded:
            print(f"[error] 灌库失败，别名未切换：{stats.error}", file=sys.stderr)
            return EXIT_FAILED

        # 校验：新集合必须真的能查到数据，绝不"灌完就切"。
        # Milvus 的可见性是最终一致的，count(settle=True) 会轮询到稳定值。
        observed = store.count(settle=True)
        print(f"[info] 写入 {stats.chunks} 块，集合可见 {observed} 行")
        if observed < stats.chunks:
            print(
                f"[error] 校验不通过：可见行数 {observed} < 应有块数 {stats.chunks}。"
                "别名保持指向旧版本，旧索引继续服务。",
                file=sys.stderr,
            )
            return EXIT_VERIFY_FAILED

        store.switch_alias(new_collection)
        print(f"[ok] 别名 {store.alias} 已切到 {new_collection}")
    finally:
        store.pin_collection(None)

    if keep >= 0:
        dropped = store.drop_old_versions(keep=keep)
        if dropped:
            print("[clean] 已清理旧版本：" + ", ".join(dropped))
    return EXIT_OK


def rollback(settings, *, target_version: Optional[int]) -> int:
    rag, store, _embedding = build_stack(settings)
    try:
        versions = store.list_versions()
        current = store.current_version()
    except Exception as exc:
        print(f"[error] 无法读取 Milvus 集合：{type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_FAILED

    if not versions:
        print("[error] 没有任何版本化集合可回滚", file=sys.stderr)
        return EXIT_FAILED

    if target_version is None:
        older = [v for v in versions if current is None or v < current]
        if not older:
            print(
                f"[error] 当前 v{current} 已是最旧版本，无可回滚目标", file=sys.stderr
            )
            return EXIT_FAILED
        target_version = older[-1]

    if target_version not in versions:
        print(f"[error] 版本 v{target_version} 不存在（已有 {versions}）", file=sys.stderr)
        return EXIT_FAILED
    if target_version == current:
        print(f"[info] 别名已指向 v{target_version}，无需切换")
        return EXIT_OK

    try:
        name = store.switch_to_version(target_version)
    except Exception as exc:
        print(f"[error] 切换别名失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_FAILED
    print(f"[ok] 别名 {store.alias} 已回滚到 v{target_version}（{name}）")

    # 清单描述的是"最新一次灌库"的内容，回滚后它与集合不一致 ——
    # 留着会让下一次增量灌库误判"文件没变"从而跳过重建（集合停在旧版）。
    # 移走清单，下次增量灌库会按磁盘内容全量收敛。
    manifest_path = rag.manifest_path
    if manifest_path.exists():
        backup = manifest_path.with_suffix(manifest_path.suffix + ".rollback")
        try:
            os.replace(manifest_path, backup)
            print(f"[warn] 进度清单已移到 {backup.name}，下次增量灌库会全量收敛")
        except OSError as exc:
            print(f"[warn] 清单移走失败（{exc}），请手工检查 {manifest_path}", file=sys.stderr)
    return EXIT_OK


def parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="知识库灌库单写者 Job（Phase 6）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--force", action="store_true", help="全量重建到新版本集合并原子切别名")
    parser.add_argument("--dry-run", action="store_true", help="只解析统计，不连外部服务")
    parser.add_argument("--list", action="store_true", help="列出版本与别名指向")
    parser.add_argument("--rollback", action="store_true", help="把别名切回上一个版本")
    parser.add_argument("--version", type=int, default=None, help="配合 --rollback 指定版本号")
    parser.add_argument("--keep", type=int, default=2, help="保留最近 N 个版本（默认 2）")
    parser.add_argument(
        "--lock-file",
        default=str(BACKEND_ROOT / "logs" / ".ingest.lock"),
        help="单写者锁文件路径",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list] = None) -> int:
    args = parse_args(argv)
    from xiaozhi.config import get_settings

    settings = get_settings()

    if args.dry_run:
        return dry_run(settings)

    lock = SingleWriterLock(Path(args.lock_file))
    if not lock.acquire():
        print(
            f"[locked] 另一个灌库任务正在运行（锁 {args.lock_file}）；"
            "同一次灌库只允许一个写者",
            file=sys.stderr,
        )
        return EXIT_LOCKED

    try:
        if args.list:
            _rag, store, _embedding = build_stack(settings)
            return list_versions(store)
        if args.rollback:
            return rollback(settings, target_version=args.version)
        return asyncio.run(ingest(settings, force=args.force, keep=args.keep))
    except Exception as exc:  # 兜底：脚本不能把 traceback 直接当退出码 1 以外的东西
        print(f"[error] {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_FAILED
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
