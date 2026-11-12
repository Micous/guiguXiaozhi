"""Phase 8-D：过期数据清理作业（参数与安全检查）。

真实删除行为在 ``scripts/purge_expired.py`` 里、需要 MySQL；
单测只钉住"不会误删/不会崩"的边界：

* 留存天数不允许为负（负数会让 ``NOW() - INTERVAL -7 DAY`` 变成未来，
  把还没过期的记录全删掉 —— 这是最容易写错的一行）；
* 默认留存期与任务终态集合是**契约**，改动要让调用方知道。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "purge_expired.py"


def _load():
    spec = importlib.util.spec_from_file_location("purge_expired", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["purge_expired"] = module
    spec.loader.exec_module(module)
    return module


def test_default_retention_windows():
    args = _load().parse_args([])
    assert args.approval_days == 7
    assert args.task_days == 30
    assert args.dry_run is False
    assert args.batch_size == 200


def test_negative_retention_is_rejected():
    module = _load()
    assert module.main(["--approval-days", "-1"]) == module.EXIT_FAILED


def test_terminal_statuses_cover_all_finished_states():
    """拦截"漏掉一个终态"：任务永远清不掉，表只增不减。"""
    module = _load()
    assert set(module.TERMINAL_TASK_STATUSES) == {
        "SUCCEEDED",
        "FAILED",
        "WAITING_APPROVAL",
        "CANCELLED",
    }


@pytest.mark.parametrize("table", ["approval_requests", "agent_tasks"])
def test_purge_tables_are_existence_checked(table):
    """表不存在时必须跳过而不是抛错（全新库/未开审批的场景要能跑）。"""
    module = _load()
    source = Path(module.__file__).read_text()
    assert f'_table_exists(cur, "{table}")' in source
