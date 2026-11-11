"""Phase 3a 验收测试 —— 预约业务服务、仓储异常分类、迁移机制。

对应路线图 3a（P0-04 数据一致性）的验收口径：

1. **并发重复预约只进一条**：原子 ``create_if_absent`` + 唯一约束兜底；
2. **异常分类**：基础设施故障与业务规则冲突**不再混为一谈**，
   工具层能区分"系统坏了"和"重复预约"并如实转述；
3. **取消 = 状态迁移**：``CANCELLED`` 保留审计痕迹，不物理删除；
4. **迁移机制幂等**：版本表、校验和、失败重跑收敛。
"""

from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from xiaozhi.repositories.appointment_repo import (  # noqa: E402
    Appointment,
    AppointmentStatus,
    InMemoryAppointmentRepository,
)
from xiaozhi.repositories.errors import (  # noqa: E402
    NotFound,
    RepositoryUnavailable,
    UniqueConflict,
)
from xiaozhi.service.appointment_service import (  # noqa: E402
    AppointmentService,
    BookingOutcome,
)
from xiaozhi.service.slot_service import SlotAvailability  # noqa: E402

VALID_ID = "110101199005054322"


# ============================================================
# Fake 组件
# ============================================================

class FakeSlotService:
    def __init__(self, available=True, doctors=("王笠",)):
        self.available = available
        self.doctors = list(doctors)

    def check(self, department, date_str, time_slot, doctor_name=None):
        if not self.available:
            return SlotAvailability.none("号源已满")
        return SlotAvailability(
            available=True, message="有号源", doctors=self.doctors, remaining=3
        )


def make_appointment(**overrides) -> Appointment:
    fields = dict(
        username="张三",
        id_card=VALID_ID,
        department="神经内科",
        date="2026-11-20",
        time="上午",
        doctor_name="王笠",
    )
    fields.update(overrides)
    return Appointment(**fields)


def make_service(repo, slot=None) -> AppointmentService:
    return AppointmentService(repo, slot if slot is not None else FakeSlotService())


# ============================================================
# 1. book —— 正常流与业务分支
# ============================================================

def test_book_success_creates_confirmed_record():
    repo = InMemoryAppointmentRepository()
    service = make_service(repo)
    result = service.book(
        username="张三", id_card=VALID_ID,
        department="神经内科", date="2026-11-20", time="上午", doctor_name="王笠",
    )
    assert result.outcome == BookingOutcome.CREATED
    assert result.appointment.id is not None
    assert result.appointment.status == AppointmentStatus.CONFIRMED
    assert len(repo.list_all()) == 1


def test_book_without_doctor_picks_first_on_duty():
    """未指定医生 → 服务层自动选第一个出诊医生（与旧行为一致）"""
    repo = InMemoryAppointmentRepository()
    service = make_service(repo, FakeSlotService(doctors=("王笠", "崔丽英")))
    result = service.book(
        username="张三", id_card=VALID_ID,
        department="神经内科", date="2026-11-20", time="上午",
    )
    assert result.outcome == BookingOutcome.CREATED
    assert result.appointment.doctor_name == "王笠"


def test_book_no_slot_does_not_write():
    """号源不足 → 不落库（宁可失败也不写脏数据）"""
    repo = InMemoryAppointmentRepository()
    service = make_service(repo, FakeSlotService(available=False))
    result = service.book(
        username="张三", id_card=VALID_ID,
        department="神经内科", date="2026-11-20", time="上午",
    )
    assert result.outcome == BookingOutcome.NO_SLOT
    assert repo.list_all() == []


def test_book_duplicate_returns_existing_not_error():
    """同一五元组重复预约 → 返回既有记录，不重复写入"""
    repo = InMemoryAppointmentRepository()
    service = make_service(repo)
    first = service.book(
        username="张三", id_card=VALID_ID,
        department="神经内科", date="2026-11-20", time="上午",
    )
    assert first.outcome == BookingOutcome.CREATED
    second = service.book(
        username="张三", id_card=VALID_ID,
        department="神经内科", date="2026-11-20", time="上午",
    )
    assert second.outcome == BookingOutcome.ALREADY_EXISTS
    assert second.appointment.id == first.appointment.id
    assert len(repo.list_all()) == 1


def test_book_after_cancel_allows_rebooking():
    """取消后的同一时段可以重新预约（唯一约束只约束未取消记录）"""
    repo = InMemoryAppointmentRepository()
    service = make_service(repo)
    first = service.book(
        username="张三", id_card=VALID_ID,
        department="神经内科", date="2026-11-20", time="上午",
    )
    service.cancel(
        username="张三", id_card=VALID_ID,
        department="神经内科", date="2026-11-20", time="上午",
    )
    rebook = service.book(
        username="张三", id_card=VALID_ID,
        department="神经内科", date="2026-11-20", time="上午",
    )
    assert rebook.outcome == BookingOutcome.CREATED
    assert rebook.appointment.id != first.appointment.id
    # 两条记录都在：一条 CANCELLED（审计痕迹），一条 CONFIRMED
    rows = repo.list_all()
    assert len(rows) == 2
    assert {r.status for r in rows} == {
        AppointmentStatus.CANCELLED,
        AppointmentStatus.CONFIRMED,
    }


# ============================================================
# 2. 并发重复预约只进一条（P0-04 核心验收）
# ============================================================

def test_concurrent_duplicate_bookings_only_one_row():
    """多线程并发提交同一预约 → 库里只进一条。

    旧版 find_duplicate + save 两次调用之间存在竞态窗口，
    可能写出两条重复预约；新版收敛到带锁的原子里程程。
    """
    repo = InMemoryAppointmentRepository()
    service = make_service(repo)
    outcomes: list = []
    lock = threading.Lock()

    def worker():
        result = service.book(
            username="张三", id_card=VALID_ID,
            department="神经内科", date="2026-11-20", time="上午",
        )
        with lock:
            outcomes.append(result.outcome)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    created = outcomes.count(BookingOutcome.CREATED)
    already = outcomes.count(BookingOutcome.ALREADY_EXISTS)
    assert created == 1, f"应只有 1 条 CREATED，实际 {created}"
    assert created + already == 10, f"有请求落到了其它分支：{outcomes}"
    assert len(repo.list_all()) == 1


# ============================================================
# 3. 异常分类 —— 基础设施故障不再伪装成业务失败
# ============================================================

class ExplodingRepository:
    """按预置异常抛错的仓储，用来验证服务层的分类路径。"""

    def __init__(self, exc=None, existing=None):
        self.exc = exc
        self.existing = existing

    def find_duplicate(self, appointment):
        if self.exc is not None:
            raise self.exc
        return self.existing

    def find_by_key(self, appointment):
        if self.exc is not None:
            raise self.exc
        return self.existing

    def save(self, appointment):
        self.saved = getattr(self, "saved", [])
        self.saved.append(appointment)
        return appointment

    def create_if_absent(self, appointment):
        if self.exc is not None:
            raise self.exc
        return appointment, True

    def cancel(self, appointment_id):
        if self.exc is not None:
            raise self.exc
        raise NotFound(f"预约记录不存在（id={appointment_id}）")

    def delete(self, appointment_id):
        return True

    def list_all(self, limit=100):
        return []


def test_book_repository_unavailable_is_classified():
    """MySQL 挂了 → REPOSITORY_UNAVAILABLE，让用户听到"系统故障"而非"没有号" """
    service = make_service(
        ExplodingRepository(RepositoryUnavailable("连接超时"))
    )
    result = service.book(
        username="张三", id_card=VALID_ID,
        department="神经内科", date="2026-11-20", time="上午",
    )
    assert result.outcome == BookingOutcome.REPOSITORY_UNAVAILABLE
    assert "不可用" in result.message


def test_book_unique_conflict_maps_to_already_exists():
    """并发下撞唯一约束 → 业务规则冲突（重复预约），不是系统故障"""
    service = make_service(ExplodingRepository(UniqueConflict("唯一约束冲突")))
    result = service.book(
        username="张三", id_card=VALID_ID,
        department="神经内科", date="2026-11-20", time="上午",
    )
    assert result.outcome == BookingOutcome.ALREADY_EXISTS


def test_book_generic_repo_error_maps_to_failed():
    service = make_service(ExplodingRepository(NotFound("意外")))
    result = service.book(
        username="张三", id_card=VALID_ID,
        department="神经内科", date="2026-11-20", time="上午",
    )
    assert result.outcome == BookingOutcome.FAILED


def test_book_requires_slot_service():
    """book 依赖号源服务；没配置就 fail-fast，而不是静默给出错误结果"""
    service = AppointmentService(InMemoryAppointmentRepository(), None)
    with pytest.raises(RuntimeError, match="slot_service"):
        service.book(
            username="张三", id_card=VALID_ID,
            department="神经内科", date="2026-11-20", time="上午",
        )


# ============================================================
# 4. cancel —— 状态迁移保留审计痕迹
# ============================================================

def test_cancel_is_state_transition_not_delete():
    """取消后记录仍在，status=CANCELLED、version+1"""
    repo = InMemoryAppointmentRepository()
    service = make_service(repo)
    created = service.book(
        username="张三", id_card=VALID_ID,
        department="神经内科", date="2026-11-20", time="上午",
    )
    # InMemory 实现返回内部同一对象引用，先快照 cancel 前的版本号
    version_before = created.appointment.version
    result = service.cancel(
        username="张三", id_card=VALID_ID,
        department="神经内科", date="2026-11-20", time="上午",
    )
    assert result.outcome == BookingOutcome.CREATED
    assert result.appointment.id == created.appointment.id
    assert result.appointment.status == AppointmentStatus.CANCELLED
    assert result.appointment.version == version_before + 1
    # 记录还在（不物理删除）
    rows = repo.list_all()
    assert len(rows) == 1
    assert rows[0].status == AppointmentStatus.CANCELLED


def test_cancel_nonexistent_returns_readable_failure():
    service = make_service(InMemoryAppointmentRepository())
    result = service.cancel(
        username="李四", id_card=VALID_ID,
        department="神经内科", date="2026-11-20", time="上午",
    )
    assert result.outcome == BookingOutcome.FAILED
    assert "未找到" in result.message


def test_cancel_twice_is_invalid_state():
    repo = InMemoryAppointmentRepository()
    service = make_service(repo)
    service.book(
        username="张三", id_card=VALID_ID,
        department="神经内科", date="2026-11-20", time="上午",
    )
    service.cancel(
        username="张三", id_card=VALID_ID,
        department="神经内科", date="2026-11-20", time="上午",
    )
    again = service.cancel(
        username="张三", id_card=VALID_ID,
        department="神经内科", date="2026-11-20", time="上午",
    )
    assert again.outcome == BookingOutcome.FAILED
    assert "已取消" in again.message


def test_cancel_repository_unavailable_is_classified():
    service = make_service(
        ExplodingRepository(RepositoryUnavailable("连接超时"))
    )
    result = service.cancel(
        username="张三", id_card=VALID_ID,
        department="神经内科", date="2026-11-20", time="上午",
    )
    assert result.outcome == BookingOutcome.REPOSITORY_UNAVAILABLE


# ============================================================
# 5. 工具层转述 —— 异常分类要透传到模型可见的 error_type
# ============================================================

def _book_tool_with(repo):
    from xiaozhi.tools.appointment_tools import BookAppointmentTool, CancelAppointmentTool
    from tests.unit.test_appointment_tools import FakeDoctorService

    book = BookAppointmentTool(repo, FakeSlotService(), FakeDoctorService())
    cancel = CancelAppointmentTool(repo)
    return book, cancel


def test_tool_reports_repository_unavailable_honestly():
    """基础设施故障 → error_type=RepositoryUnavailable（如实说系统问题）"""
    book, _ = _book_tool_with(ExplodingRepository(RepositoryUnavailable("连接超时")))
    result = asyncio.run(book.call({
        "username": "张三", "id_card": VALID_ID,
        "department": "神经内科", "date": "2026-11-20", "time": "上午",
    }))
    assert not result.success
    assert result.error_type == "RepositoryUnavailable"


def test_cancel_tool_success_and_duplicate_cancel():
    from xiaozhi.repositories.appointment_repo import AppointmentRepository

    class Mem(InMemoryAppointmentRepository, AppointmentRepository):
        pass

    _book, cancel = _book_tool_with(Mem())
    # 没有可取消的预约 → NotFound 文案
    miss = asyncio.run(cancel.call({
        "username": "张三", "id_card": VALID_ID,
        "department": "神经内科", "date": "2026-11-20", "time": "上午",
    }))
    assert not miss.success
    assert miss.error_type == "NotFound"


# ============================================================
# 6. 迁移机制（离线：fake 连接，不依赖真 MySQL）
# ============================================================

class FakeCursor:
    """最小 MySQL cursor 模拟：记录语句，SELECT 时返回预置行。"""

    def __init__(self, conn, dictionary=False):
        self._conn = conn
        self._dictionary = dictionary

    def execute(self, statement, params=None):
        self._conn.executed.append(statement)
        normalized = " ".join(statement.split()).upper()
        if normalized.startswith("SELECT VERSION, CHECKSUM"):
            self._conn.result_rows = [
                {"version": v, "checksum": c} for v, c in self._conn.applied.items()
            ]
        elif normalized.startswith("INSERT INTO SCHEMA_MIGRATIONS"):
            version, _name, checksum = params[:3]
            self._conn.applied[int(version)] = str(checksum)
            self._conn.result_rows = []
        else:
            self._conn.result_rows = []
        self._conn.last_params = params

    def fetchone(self):
        rows = self._conn.result_rows
        return rows[0] if rows else None

    def fetchall(self):
        return list(self._conn.result_rows)

    def close(self):
        pass


class FakeConnection:
    def __init__(self, applied=None):
        self.executed: list = []
        self.applied: dict = dict(applied or {})
        self.result_rows: list = []
        self.committed = 0
        self.rolled_back = 0
        self._cursor = None

    def cursor(self, dictionary=False):
        self._cursor = FakeCursor(self, dictionary=dictionary)
        return self._cursor

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolled_back += 1

    def close(self):
        pass


def make_migrator(sql_dir: Path, applied=None):
    from xiaozhi.migrations import Migrator

    migrator = Migrator("h", 1, "u", "p", "db", sql_dir=sql_dir)
    migrator._fake_conn = FakeConnection(applied=applied)
    migrator._connect = lambda: migrator._fake_conn  # type: ignore[method-assign]
    return migrator


def write_migration(sql_dir: Path, version: int, name: str, body: str) -> Path:
    path = sql_dir / f"{version:03d}_{name}.sql"
    path.write_text(body, encoding="utf-8")
    return path


# 注意：不用 pytest 的 tmp_path fixture —— 它要在 /private/var/folders 下
# 建目录，受限环境里会失败；改用项目内临时目录，测试结束后清理。
_MIGRATION_TMP = Path(__file__).resolve().parent / ".migration_test_tmp"


@pytest.fixture
def sql_dir():
    _MIGRATION_TMP.mkdir(parents=True, exist_ok=True)
    yield _MIGRATION_TMP
    for f in _MIGRATION_TMP.glob("*.sql"):
        f.unlink()


def test_discover_migrations_orders_and_validates(sql_dir):
    write_migration(sql_dir, 2, "second", "SELECT 2;")
    write_migration(sql_dir, 1, "first", "SELECT 1;")
    from xiaozhi.migrations import discover_migrations

    found = discover_migrations(sql_dir)
    assert [m.version for m in found] == [1, 2]

    # 版本号重复 → 配置错误，直接报错
    write_migration(sql_dir, 2, "dup", "SELECT 3;")
    with pytest.raises(Exception, match="重复"):
        discover_migrations(sql_dir)


def _business_statements(conn) -> list:
    """过滤掉版本表自身的 DDL/DML，只看业务语句。"""
    return [
        s for s in conn.executed
        if "schema_migrations" not in s.lower()
    ]


def test_migrator_skips_applied_and_is_idempotent(sql_dir):
    """已应用的迁移重跑 → 全部 skip，零业务语句执行（幂等）"""
    body = "SELECT 1;"
    path = write_migration(sql_dir, 1, "first", body)
    migrator = make_migrator(sql_dir)
    checksum = migrator._checksum(path)

    # 第一轮：应用
    result = migrator.migrate()
    assert result.applied == ["001_first"]
    assert migrator._fake_conn.applied == {1: checksum}

    # 第二轮（新 Migrator，模拟重启）：已记录 → skip，不执行任何业务语句
    again = make_migrator(sql_dir, applied={1: checksum})
    result2 = again.migrate()
    assert result2.applied == []
    assert result2.skipped == ["001_first"]
    assert _business_statements(again._fake_conn) == []


def test_migrator_rejects_modified_applied_migration(sql_dir):
    """已执行的迁移文件被改动 → 校验和不一致必须报错（不许悄悄变更历史）"""
    from xiaozhi.migrations import MigrationError

    path = write_migration(sql_dir, 1, "first", "SELECT 1;")
    migrator = make_migrator(sql_dir)
    checksum = migrator._checksum(path)
    migrator.migrate()

    # 改动文件内容
    path.write_text("SELECT 999;", encoding="utf-8")
    modified = make_migrator(sql_dir, applied={1: checksum})
    with pytest.raises(MigrationError, match="校验和"):
        modified.migrate()


def test_migrator_dry_run_executes_nothing(sql_dir):
    write_migration(sql_dir, 1, "first", "CREATE TABLE t (id INT);")
    migrator = make_migrator(sql_dir)
    result = migrator.migrate(dry_run=True)
    assert result.applied == ["001_first"]
    assert _business_statements(migrator._fake_conn) == []  # 一条业务语句都没跑
    assert migrator._fake_conn.applied == {}


def test_split_statements_handles_literals_and_comments():
    """SQL 拆分要正确处理字符串字面量里的分号、'' 转义和注释"""
    from xiaozhi.migrations import _split_statements

    script = """
    -- 行注释 SELECT 1;
    SELECT 'a;b' FROM t;  # 行注释
    SELECT 'it''s ok' AS c;
    /* 块注释; 里的分号 */
    ALTER TABLE t COMMENT '状态';
    """
    statements = _split_statements(script)
    assert len(statements) == 3
    assert statements[0] == "SELECT 'a;b' FROM t"
    assert statements[1] == "SELECT 'it''s ok' AS c"
    assert statements[2] == "ALTER TABLE t COMMENT '状态'"
