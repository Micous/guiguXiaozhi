#!/usr/bin/env python
"""过期数据清理作业（Phase 8-D）。

**为什么必须有它**

Phase 2/5 落了两类"只写不删"的表：

* ``approval_requests``：每次写操作审批一条。审批是一次性凭证，
  ``expires_at`` 过了就永久无意义，但行会一直留着（真实姓名、身份证号
  打码后的参数预览、canonical_args 都在里面）；
* ``agent_tasks`` / ``agent_executions`` / ``tool_executions`` / ``task_events``：
  每个请求 1 + N + M 行事件流水，只增不减。

不清理的后果不是"磁盘满"这么温和：留存即风险 —— 数据留存越久，
泄露的影响面越大，也无法回答"这些数据为什么还在"。PIPL 的最小必要
原则要求有明确的留存期限与删除动作。

用法（``guiguXiaozhi_backend_py/`` 下，可挂 CronJob）：

    # 只看会删多少行（不写库）
    python scripts/purge_expired.py --dry-run

    # 执行（审批 7 天、任务 30 天）
    python scripts/purge_expired.py

    # 自定义留存期
    python scripts/purge_expired.py --approval-days 3 --task-days 14 --batch-size 500

退出码：0 成功；1 执行异常。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from xiaozhi.config import get_settings  # noqa: E402

EXIT_OK = 0
EXIT_FAILED = 1

#: 任务的终态（这些状态下任务不会再被恢复扫描处理，可以安全回收）
TERMINAL_TASK_STATUSES = ("SUCCEEDED", "FAILED", "WAITING_APPROVAL", "CANCELLED")


def parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="清理过期的审批与任务流水")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不删除")
    parser.add_argument("--approval-days", type=int, default=7, help="审批记录留存天数（默认 7）")
    parser.add_argument("--task-days", type=int, default=30, help="任务流水留存天数（默认 30）")
    parser.add_argument("--batch-size", type=int, default=200, help="每批删除行数（默认 200）")
    return parser.parse_args(argv)


def _connect():
    import mysql.connector

    s = get_settings()
    return mysql.connector.connect(
        host=s.mysql_host,
        port=s.mysql_port,
        user=s.mysql_user,
        password=s.mysql_password,
        database=s.mysql_database,
    )


def _table_exists(cur, name: str) -> bool:
    cur.execute(
        "SELECT COUNT(*) FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
        (name,),
    )
    return int(cur.fetchone()[0]) > 0


def purge_approvals(cur, days: int, batch: int, dry_run: bool) -> int:
    """删除 ``expires_at`` 早于留存窗口的审批记录。

    ``expires_at`` 是审批的一次性凭证有效期（TTL）。过了这个时刻，
    token 无论如何都换不出可执行的审批 —— 所以按它判定"已死"是安全的，
    不需要看 status（PENDING/APPROVED 过期后同样不可消费）。
    """
    if not _table_exists(cur, "approval_requests"):
        print("  跳过 approval_requests（表不存在）")
        return 0
    if dry_run:
        cur.execute(
            "SELECT COUNT(*) FROM approval_requests "
            "WHERE expires_at < NOW() - INTERVAL %s DAY",
            (days,),
        )
        count = int(cur.fetchone()[0])
        print(f"  [dry-run] approval_requests 将删除 {count} 行（expires_at 早于 {days} 天前）")
        return count

    total = 0
    while True:
        cur.execute(
            "DELETE FROM approval_requests WHERE expires_at < NOW() - INTERVAL %s DAY "
            "ORDER BY expires_at ASC LIMIT %s",
            (days, int(batch)),
        )
        deleted = cur.rowcount
        total += deleted
        if deleted < batch:
            break
    print(f"  approval_requests 删除 {total} 行")
    return total


def purge_tasks(cur, days: int, batch: int, dry_run: bool) -> int:
    """回收终态且超过留存期的任务及其子表行。

    子表有外键（``task_events`` / ``tool_executions`` / ``agent_executions``
    → ``agent_tasks``），必须**先删子再删父**；顺序错了会直接撞 FK 报错。
    """
    if not _table_exists(cur, "agent_tasks"):
        print("  跳过 agent_tasks（表不存在）")
        return 0

    placeholders = ",".join(["%s"] * len(TERMINAL_TASK_STATUSES))
    if dry_run:
        cur.execute(
            f"SELECT COUNT(*) FROM agent_tasks WHERE status IN ({placeholders}) "
            "AND updated_at < NOW() - INTERVAL %s DAY",
            (*TERMINAL_TASK_STATUSES, days),
        )
        count = int(cur.fetchone()[0])
        print(f"  [dry-run] agent_tasks 将回收 {count} 个任务（终态且超过 {days} 天）")
        return count

    total = 0
    while True:
        cur.execute(
            f"SELECT task_id FROM agent_tasks WHERE status IN ({placeholders}) "
            "AND updated_at < NOW() - INTERVAL %s DAY ORDER BY updated_at ASC LIMIT %s",
            (*TERMINAL_TASK_STATUSES, days, int(batch)),
        )
        task_ids = [row[0] for row in cur.fetchall()]
        if not task_ids:
            break
        marks = ",".join(["%s"] * len(task_ids))
        for table in ("task_events", "tool_executions", "agent_executions"):
            if _table_exists(cur, table):
                cur.execute(f"DELETE FROM {table} WHERE task_id IN ({marks})", tuple(task_ids))
        cur.execute(f"DELETE FROM agent_tasks WHERE task_id IN ({marks})", tuple(task_ids))
        total += len(task_ids)
        if len(task_ids) < batch:
            break
    print(f"  agent_tasks 回收 {total} 个任务（含子表流水）")
    return total


def main(argv: Optional[list] = None) -> int:
    args = parse_args(argv)
    if args.approval_days < 0 or args.task_days < 0:
        print("[参数错误] 留存天数不能为负", file=sys.stderr)
        return EXIT_FAILED
    print(f"== 过期数据清理（{'dry-run' if args.dry_run else '执行'}）==")
    conn = _connect()
    try:
        cur = conn.cursor()
        purged = purge_approvals(cur, args.approval_days, args.batch_size, args.dry_run)
        purged += purge_tasks(cur, args.task_days, args.batch_size, args.dry_run)
        if not args.dry_run:
            conn.commit()
    finally:
        conn.close()
    print(f"合计：{'将清理' if args.dry_run else '已清理'} {purged} 行/条")
    return EXIT_OK


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - CLI 统一出口
        print(f"[执行失败] {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(EXIT_FAILED) from exc
