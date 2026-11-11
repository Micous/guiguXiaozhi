"""数据库迁移机制 —— Phase 3a（审计 P0-04「建数据库迁移机制，停用启动脚本临时建表」）。

**为什么要有**

改造前每张表都在自己的仓储里用 ``CREATE TABLE IF NOT EXISTS`` 建：
``appointment``（工具层临时脚本）、``agent_sessions``、``approval_requests``、
``user_profile``。问题是：

* 建表语句**散落在业务代码里**，改字段要翻遍仓储；
* 没有版本概念 → "这个库现在是第几版"没人说得清；
* 存量库加字段只能靠人手写 SQL，**改错了没法回滚**；
* 生产库与应用代码**版本不匹配**时无法察觉（应用以为表有新字段，库里没有）。

本模块提供最小可用的版本化迁移：

* ``migrations/sql/NNN_name.sql`` —— 按文件名数字前缀顺序执行；
* ``schema_migrations`` 表记录已执行的版本与时间戳；
* 每个文件在**自己的事务**里执行，失败即回滚并中止后续迁移；
* 支持 ``--dry-run`` 预演（只看要执行什么，不落库）。

**与既有代码的关系**：迁移是**可选的**。`schema_migrations` 由本模块自己建，
不依赖任何业务表；未被调用时既有仓储的 ``ensure_table`` 仍然照常工作
（灰度默认行为零变更）。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 迁移脚本目录（与本文件同级）
SQL_DIR = Path(__file__).resolve().parent / "sql"

# 版本表名
VERSION_TABLE = "schema_migrations"

# 文件名形如 001_appointment_base.sql → 版本号 1
_FILENAME_RE = re.compile(r"^(\d+)[_.](.+)\.sql$")


class MigrationError(Exception):
    """迁移执行失败 —— 必须让启动失败，不能静默跳过。"""


@dataclass(frozen=True)
class Migration:
    """一条迁移。"""

    version: int
    name: str
    path: Path

    @property
    def label(self) -> str:
        return f"{self.version:03d}_{self.name}"


@dataclass(frozen=True)
class MigrationResult:
    """一次迁移执行的结果。"""

    applied: List[str]
    skipped: List[str]

    @property
    def changed(self) -> bool:
        return bool(self.applied)


def discover_migrations(sql_dir: Optional[Path] = None) -> List[Migration]:
    """按版本号升序发现迁移文件。

    文件名必须是 ``NNN_name.sql``；重名版本号视为配置错误（直接报错），
    否则执行顺序会变得不确定。
    """
    directory = Path(sql_dir or SQL_DIR)
    if not directory.exists():
        return []

    found: List[Migration] = []
    seen: Dict[int, str] = {}
    for path in sorted(directory.glob("*.sql")):
        match = _FILENAME_RE.match(path.name)
        if not match:
            raise MigrationError(
                f"迁移文件名不合规（应为 NNN_name.sql）：{path.name}"
            )
        version = int(match.group(1))
        name = match.group(2)
        if version in seen:
            raise MigrationError(
                f"迁移版本号重复：{version}（{seen[version]} 与 {path.name}）"
            )
        seen[version] = path.name
        found.append(Migration(version=version, name=name, path=path))

    found.sort(key=lambda m: m.version)
    return found


def _split_statements(script: str) -> List[str]:
    """把 SQL 文件拆成可逐条执行的语句。

    需要正确处理字符串字面量里的分号（如 ``'DO 0'`` 之外的
    ``'ALTER TABLE ... COMMENT ''状态'''``），否则会在字面量中间断开。
    """
    statements: List[str] = []
    buffer: List[str] = []
    in_single = False
    in_double = False
    in_backtick = False
    index = 0
    length = len(script)

    while index < length:
        char = script[index]
        # 注释：-- 到行尾、# 到行尾、/* ... */
        if not in_single and not in_double and not in_backtick:
            if script.startswith("--", index):
                newline = script.find("\n", index)
                index = length if newline < 0 else newline + 1
                continue
            if char == "#":
                newline = script.find("\n", index)
                index = length if newline < 0 else newline + 1
                continue
            if script.startswith("/*", index):
                end = script.find("*/", index + 2)
                index = length if end < 0 else end + 2
                continue

        if char == "'" and not in_double and not in_backtick:
            # 连续两个单引号是转义的单引号（''）
            if in_single and index + 1 < length and script[index + 1] == "'":
                buffer.append(char)
                buffer.append("'")
                index += 2
                continue
            in_single = not in_single
        elif char == '"' and not in_single and not in_backtick:
            in_double = not in_double
        elif char == "`" and not in_single and not in_double:
            in_backtick = not in_backtick

        if char == ";" and not in_single and not in_double and not in_backtick:
            statement = "".join(buffer).strip()
            if statement:
                statements.append(statement)
            buffer = []
        else:
            buffer.append(char)
        index += 1

    tail = "".join(buffer).strip()
    if tail:
        statements.append(tail)
    return statements


class Migrator:
    """迁移执行器。

    用法::

        migrator = Migrator(host, port, user, password, database)
        result = migrator.migrate()          # 执行待应用的迁移
        result = migrator.migrate(dry_run=True)  # 只看要执行什么
    """

    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        database: str,
        *,
        sql_dir: Optional[Path] = None,
    ) -> None:
        self._config = {
            "host": host,
            "port": port,
            "user": user,
            "password": password,
            "database": database,
        }
        self._sql_dir = Path(sql_dir or SQL_DIR)

    def _connect(self):
        import mysql.connector

        return mysql.connector.connect(**self._config)

    # ---- 版本表 ----

    def _ensure_version_table(self) -> None:
        conn = self._connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                f"CREATE TABLE IF NOT EXISTS {VERSION_TABLE} ("
                "  version INT NOT NULL PRIMARY KEY,"
                "  name VARCHAR(128) NOT NULL,"
                "  checksum CHAR(64) NOT NULL,"
                "  applied_at DATETIME NOT NULL"
                ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
            )
            conn.commit()
            cursor.close()
        finally:
            conn.close()

    def applied_versions(self) -> Dict[int, str]:
        """已应用的版本 → 校验和。"""
        self._ensure_version_table()
        conn = self._connect()
        try:
            cursor = conn.cursor(dictionary=True)
            cursor.execute(f"SELECT version, checksum FROM {VERSION_TABLE}")
            rows = cursor.fetchall()
            cursor.close()
            return {int(r["version"]): str(r["checksum"]) for r in rows}
        finally:
            conn.close()

    @staticmethod
    def _checksum(path: Path) -> str:
        import hashlib

        return hashlib.sha256(path.read_bytes()).hexdigest()

    # ---- 执行 ----

    def migrate(self, *, dry_run: bool = False) -> MigrationResult:
        """按版本顺序执行待应用的迁移。

        每条迁移在**自己的事务**里执行：失败 → 回滚 → 抛
        ``MigrationError``（不静默跳过、不"部分应用"）。
        """
        migrations = discover_migrations(self._sql_dir)
        applied = self.applied_versions()

        pending: List[Migration] = []
        skipped: List[str] = []
        for migration in migrations:
            if migration.version in applied:
                # 已应用的迁移文件若被改动 → 校验和不一致，必须报出来
                expected = self._checksum(migration.path)
                if applied[migration.version] != expected:
                    raise MigrationError(
                        f"迁移 {migration.label} 已被执行过，但文件内容已变更"
                        f"（校验和不一致）——已执行的迁移不允许修改，"
                        f"请新增一条迁移。"
                    )
                skipped.append(migration.label)
                continue
            pending.append(migration)

        if dry_run:
            return MigrationResult(applied=[m.label for m in pending], skipped=skipped)

        for migration in pending:
            self._apply_one(migration)
            logger.info("已应用迁移：%s", migration.label)

        return MigrationResult(applied=[m.label for m in pending], skipped=skipped)

    def _apply_one(self, migration: Migration) -> None:
        """执行一条迁移。

        **DDL 在 MySQL 里不是事务性的** —— ``ALTER TABLE`` 会隐式提交，
        ``conn.rollback()`` **回滚不了已执行的 DDL**。
        2026-11-11 首次跑 002 时踩到了：唯一索引因存量重复数据而失败，
        前面几条 ``ADD COLUMN`` 已经落库，rollback 只回滚了 DML，
        结果数据库停在"列已加、索引没加"的**部分应用**状态。

        因此这里的约定是：
        1. 迁移脚本自身必须**幂等**（每条 DDL 先判断存在与否再执行），
           这样"失败后重跑"能收敛到正确状态；
        2. 失败时**如实说明**已执行的部分 DDL 不可能自动回滚，
           并提示重跑（幂等脚本会补齐剩余部分）；
        3. 版本记录在**全部语句成功后**才写入 —— 版本表永远是真相来源。
        """
        script = migration.path.read_text(encoding="utf-8")
        statements = _split_statements(script)
        conn = self._connect()
        executed = 0
        try:
            cursor = conn.cursor()
            for statement in statements:
                cursor.execute(statement)
                executed += 1
            cursor.execute(
                f"INSERT INTO {VERSION_TABLE} (version, name, checksum, applied_at) "
                "VALUES (%s, %s, %s, NOW())",
                (
                    migration.version,
                    migration.name,
                    self._checksum(migration.path),
                ),
            )
            conn.commit()
            cursor.close()
        except Exception as exc:
            conn.rollback()
            raise MigrationError(
                f"迁移 {migration.label} 执行失败：{exc}\n"
                f"（已执行 {executed}/{len(statements)} 条语句；"
                f"注意 MySQL 的 DDL 不可回滚，已落库的 DDL 不会自动撤销 —— "
                f"本迁移脚本是幂等的，修正问题后**直接重跑**即可补齐。）"
            ) from exc
        finally:
            conn.close()

    def current_version(self) -> int:
        """当前数据库的版本号（未应用任何迁移时为 0）。"""
        applied = self.applied_versions()
        return max(applied) if applied else 0

    def pending(self) -> List[str]:
        """待应用的迁移标签（不执行）。"""
        applied = self.applied_versions()
        return [
            m.label
            for m in discover_migrations(self._sql_dir)
            if m.version not in applied
        ]


def run_migrations(
    host: str,
    port: int,
    user: str,
    password: str,
    database: str,
    *,
    dry_run: bool = False,
) -> MigrationResult:
    """便捷入口：执行迁移并返回结果。"""
    migrator = Migrator(host, port, user, password, database)
    return migrator.migrate(dry_run=dry_run)
