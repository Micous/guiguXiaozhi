#!/usr/bin/env python
"""预约表敏感字段保护：回填 / 切唯一索引 / 校验（Phase 8-B / 审计 P0-06）。

**为什么不是一条 SQL 迁移**

`appointment.id_card` 要改成密文，但唯一约束 `uk_appointment_active` 建在
明文列上。Fernet 是随机化加密 —— 同一个身份证号每次密文都不同，
约束要么永远不冲突（形同没有约束），要么把合法业务拒掉。

正确的顺序是三段，中间那段 SQL 干不了（HMAC 需要密钥）：

.. code-block:: text

    expand    迁移 006：加 id_card_hash / id_card_masked，放宽 id_card
      ↓
    backfill  本脚本：存量行 → 密文 + 指纹 + 掩码（幂等，可分批）
      ↓
    contract  本脚本：唯一约束从 id_card 切到 id_card_hash

把 contract 提前到 expand 里是致命的：回填没做时 `id_card_hash` 全为 NULL，
MySQL 唯一索引对 NULL 不去重 → 重复预约全部放行，而且没人会发现。

用法（在 ``guiguXiaozhi_backend_py/`` 下，需 ``PII_PROTECTION_ENABLED=true``
且 ``PII_SECRET_KEY`` 就位）：

    # 1) 先看现状（不写任何东西）
    python scripts/pii_protect_appointments.py --status

    # 2) 回填存量行（分批、幂等，可重复执行）
    python scripts/pii_protect_appointments.py --backfill

    # 3) 回填完整后切唯一约束（会先做重复检查，不通过就不动）
    python scripts/pii_protect_appointments.py --switch-index

    # 4) 验收：密文/指纹/掩码三列齐全、索引形态正确
    python scripts/pii_protect_appointments.py --verify

    # 演练：只看会改多少行
    python scripts/pii_protect_appointments.py --backfill --dry-run

退出码：
    0 成功；1 执行异常；2 配置/密钥不合规（未开启保护或密钥缺失）；
    4 校验不通过（仍有明文行 / 指纹为空 / 索引未切换）。
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
from xiaozhi.security.pii import (  # noqa: E402
    CIPHER_PREFIX,
    PiiCodec,
    PiiConfigError,
    build_pii_codec,
)

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_CONFIG = 2
EXIT_VERIFY_FAILED = 4

TABLE = "appointment"
UNIQUE_PLAIN = "uk_appointment_active"
UNIQUE_HASH = "uk_appointment_active_hash"
BATCH_SIZE = 200


def parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="预约表敏感字段保护（回填 / 切唯一索引 / 校验）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--status", action="store_true", help="现状统计（默认，不写库）")
    mode.add_argument("--backfill", action="store_true", help="把存量明文行加密 + 算指纹 + 写掩码")
    mode.add_argument("--switch-index", action="store_true", help="唯一约束切到 id_card_hash")
    mode.add_argument("--verify", action="store_true", help="验收（不通过退出码 4）")
    parser.add_argument("--dry-run", action="store_true", help="只统计要改多少，不落库")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE, help="每批处理行数")
    return parser.parse_args(argv)


def _codec_or_exit() -> PiiCodec:
    settings = get_settings()
    try:
        codec = build_pii_codec(
            enabled=settings.pii_protection_enabled,
            secret_key=settings.pii_secret_key,
        )
    except PiiConfigError as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        raise SystemExit(EXIT_CONFIG) from exc
    if codec is None:
        print(
            "[配置错误] PII_PROTECTION_ENABLED=false。\n"
            "  本脚本只用于**开启保护**之后的数据迁移；未开启时预约表按一期口径"
            "明文存储，无需也不应回填。\n"
            "  先设置 PII_PROTECTION_ENABLED=true 与 PII_SECRET_KEY 再执行。",
            file=sys.stderr,
        )
        raise SystemExit(EXIT_CONFIG)
    return codec


def _connect():
    import mysql.connector

    settings = get_settings()
    return mysql.connector.connect(
        host=settings.mysql_host,
        port=settings.mysql_port,
        user=settings.mysql_user,
        password=settings.mysql_password,
        database=settings.mysql_database,
    )


def _columns(cur) -> set:
    cur.execute(
        "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
        (TABLE,),
    )
    return {row[0] for row in cur.fetchall()}


def _unique_indexes(cur) -> set:
    cur.execute(
        "SELECT DISTINCT INDEX_NAME FROM information_schema.STATISTICS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND NON_UNIQUE = 0",
        (TABLE,),
    )
    return {row[0] for row in cur.fetchall()}


def _count(cur, where: str, params: tuple = ()) -> int:
    cur.execute(f"SELECT COUNT(*) FROM {TABLE} WHERE {where}", params)
    return int(cur.fetchone()[0])


def collect_status(cur) -> dict:
    cols = _columns(cur)
    plain_rows = _count(cur, "id_card IS NOT NULL AND id_card NOT LIKE %s", (CIPHER_PREFIX + "%",))
    total = _count(cur, "1=1")
    null_hash = _count(cur, "id_card_hash IS NULL") if "id_card_hash" in cols else -1
    null_masked = (
        _count(cur, "id_card_masked IS NULL") if "id_card_masked" in cols else -1
    )
    return {
        "columns": cols,
        "total": total,
        "plaintext_rows": plain_rows,
        "null_hash": null_hash,
        "null_masked": null_masked,
        "indexes": _unique_indexes(cur),
    }


def print_status(status: dict) -> None:
    print("== appointment 表现状 ==")
    print(f"  总行数            : {status['total']}")
    print(f"  仍为明文的行       : {status['plaintext_rows']}")
    print(f"  指纹为空的行       : {status['null_hash'] if status['null_hash'] >= 0 else '（无该列，需先跑迁移 006）'}")
    print(f"  掩码为空的行       : {status['null_masked'] if status['null_masked'] >= 0 else '（无该列，需先跑迁移 006）'}")
    print(f"  唯一索引           : {sorted(status['indexes'])}")
    print(f"  明文唯一约束在否   : {UNIQUE_PLAIN in status['indexes']}")
    print(f"  指纹唯一约束在否   : {UNIQUE_HASH in status['indexes']}")


def backfill(codec: PiiCodec, batch_size: int, dry_run: bool) -> int:
    """把存量明文行改成 密文 + 指纹 + 掩码。

    幂等：只处理 ``id_card`` 不以 ``enc:v1:`` 开头的行，
    中途失败可直接重跑（不会二次加密已加密的行）。
    """
    conn = _connect()
    changed = 0
    try:
        # 两个游标：dictionary 游标取行数据，元组游标给 information_schema 辅助函数
        # （辅助函数按 (index) 取值，混用会 KeyError: 0）
        schema_cur = conn.cursor()
        cur = conn.cursor(dictionary=True)
        cols = _columns(schema_cur)
        missing = {"id_card_hash", "id_card_masked"} - cols
        if missing:
            print(
                f"[前置条件不满足] 缺列 {sorted(missing)}：请先执行迁移 006\n"
                "  python -m xiaozhi.migrations --apply   # 或按 runbook 手工执行",
                file=sys.stderr,
            )
            return EXIT_CONFIG
        while True:
            cur.execute(
                f"SELECT id, id_card FROM {TABLE} "
                "WHERE id_card IS NOT NULL AND id_card <> '' AND id_card NOT LIKE %s "
                "ORDER BY id ASC LIMIT %s",
                (CIPHER_PREFIX + "%", int(batch_size)),
            )
            rows = cur.fetchall()
            if not rows:
                break
            if dry_run:
                changed += len(rows)
                print(f"  [dry-run] 本批将处理 {len(rows)} 行，示例 id={rows[0]['id']}")
                break
            for row in rows:
                plain = row["id_card"]
                encrypted, fingerprint, masked = codec.protect(plain)
                cur.execute(
                    f"UPDATE {TABLE} SET id_card=%s, id_card_hash=%s, id_card_masked=%s "
                    "WHERE id=%s",
                    (encrypted, fingerprint, masked, row["id"]),
                )
                changed += 1
            conn.commit()
            print(f"  已回填 {changed} 行…")
    finally:
        conn.close()
    print(f"回填完成：{changed} 行{'（dry-run，未落库）' if dry_run else ''}")
    return EXIT_OK


def switch_index(dry_run: bool) -> int:
    """唯一约束从明文列切到指纹列（contract 阶段）。

    切换前必须确认：①指纹全非空；②指纹维度没有重复的活跃预约。
    否则唯一索引建不上（或更糟：建上了但语义不对）。
    """
    conn = _connect()
    try:
        cur = conn.cursor()
        cols = _columns(cur)
        if "id_card_hash" not in cols:
            print("[前置条件不满足] 缺 id_card_hash 列，请先执行迁移 006", file=sys.stderr)
            return EXIT_CONFIG
        cur.execute(
            f"SELECT COUNT(*) FROM {TABLE} WHERE id_card_hash IS NULL OR id_card_hash = ''"
        )
        missing = int(cur.fetchone()[0])
        if missing:
            print(
                f"[拒绝切换] 还有 {missing} 行没有指纹，唯一约束切过去等于放弃去重。\n"
                "  先跑：python scripts/pii_protect_appointments.py --backfill",
                file=sys.stderr,
            )
            return EXIT_VERIFY_FAILED
        cur.execute(
            f"SELECT username, id_card_hash, department, date, time, COUNT(*) c "
            f"FROM {TABLE} WHERE status NOT IN ('CANCELLED','FAILED') "
            "GROUP BY username, id_card_hash, department, date, time HAVING c > 1 LIMIT 5"
        )
        dup = cur.fetchall()
        if dup:
            print(
                "[拒绝切换] 指纹维度已存在重复的活跃预约（切唯一索引会失败）："
                f"{dup[:3]}\n  先人工/脚本清理重复记录再重试。",
                file=sys.stderr,
            )
            return EXIT_VERIFY_FAILED

        indexes = _unique_indexes(cur)
        print(f"当前唯一索引：{sorted(indexes)}")
        if UNIQUE_HASH in indexes:
            print("指纹唯一约束已存在，无需切换")
            return EXIT_OK
        if dry_run:
            print(
                f"[dry-run] 将执行：DROP INDEX {UNIQUE_PLAIN}（如存在）→ "
                f"CREATE UNIQUE INDEX {UNIQUE_HASH} "
                "(username, id_card_hash, department, date, time, active_marker)"
            )
            return EXIT_OK

        if UNIQUE_PLAIN in indexes:
            cur.execute(f"ALTER TABLE {TABLE} DROP INDEX {UNIQUE_PLAIN}")
            print(f"已删除明文唯一约束 {UNIQUE_PLAIN}")
        cur.execute(
            f"ALTER TABLE {TABLE} ADD UNIQUE KEY {UNIQUE_HASH} "
            "(username, id_card_hash, department, date, time, active_marker)"
        )
        conn.commit()
        print(f"已建立指纹唯一约束 {UNIQUE_HASH}")
        return EXIT_OK
    finally:
        conn.close()


def verify() -> int:
    conn = _connect()
    try:
        cur = conn.cursor()
        status = collect_status(cur)
    finally:
        conn.close()
    print_status(status)

    problems = []
    if status["plaintext_rows"]:
        problems.append(f"{status['plaintext_rows']} 行仍是明文身份证号")
    if status["null_hash"] != 0:
        problems.append(f"{status['null_hash']} 行指纹为空")
    if status["null_masked"] != 0:
        problems.append(f"{status['null_masked']} 行掩码为空")
    if UNIQUE_HASH not in status["indexes"]:
        problems.append(f"唯一约束 {UNIQUE_HASH} 未建立")
    if UNIQUE_PLAIN in status["indexes"]:
        # 明文唯一约束保留无害（密文随机化不会误冲突），但语义上应已移除
        problems.append(f"明文唯一约束 {UNIQUE_PLAIN} 仍在（应为 {UNIQUE_HASH}）")
    if problems:
        print("\n[验收不通过]\n  - " + "\n  - ".join(problems), file=sys.stderr)
        return EXIT_VERIFY_FAILED
    print("\n[验收通过] 预约表已无明文身份证号，查重走 HMAC 指纹")
    return EXIT_OK


def main(argv: Optional[list] = None) -> int:
    args = parse_args(argv)
    if args.backfill or args.switch_index:
        codec = _codec_or_exit()  # 回填需要密钥；切索引也需要同一口径确认
    else:
        codec = None

    if args.backfill:
        return backfill(codec, args.batch_size, args.dry_run)
    if args.switch_index:
        return switch_index(args.dry_run)
    if args.verify:
        return verify()

    conn = _connect()
    try:
        cur = conn.cursor()
        print_status(collect_status(cur))
    finally:
        conn.close()
    return EXIT_OK


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - CLI 统一出口
        print(f"[执行失败] {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(EXIT_FAILED) from exc
