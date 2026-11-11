"""会话归属服务单测（离线，InMemory 仓储）。"""

from __future__ import annotations

import pytest

from xiaozhi.repositories.session_repo import InMemorySessionRepository
from xiaozhi.security.principal import ANONYMOUS, Principal
from xiaozhi.security.session_access import (
    SessionAccess,
    SessionForbiddenError,
    SessionNotFoundError,
)

ALICE = Principal(subject_id="alice")
BOB = Principal(subject_id="bob")


@pytest.fixture
def access() -> SessionAccess:
    return SessionAccess(InMemorySessionRepository())


def test_create_registers_owner(access):
    sid = access.create(ALICE)
    assert access._repo.get_owner(sid) == "alice"


def test_session_ids_are_unpredictable(access):
    first, second = access.create(ALICE), access.create(ALICE)
    assert first != second
    assert first.startswith("s-")
    assert len(first) >= 30  # token_urlsafe(24) → 32 字符


def test_owner_passes(access):
    sid = access.create(ALICE)
    access.assert_owner(sid, ALICE)  # 不抛


def test_cross_owner_rejected(access):
    """核心 BOLA：A 不能拿 B 的 session_id 继续对话。"""
    sid = access.create(ALICE)
    with pytest.raises(SessionForbiddenError):
        access.assert_owner(sid, BOB)


def test_unknown_session_rejected(access):
    with pytest.raises(SessionNotFoundError):
        access.assert_owner("s-never-issued", ALICE)


def test_both_failures_are_access_errors(access):
    """路由层把两者统一映射 403，不泄露 session_id 是否存在。"""
    from xiaozhi.security.session_access import SessionAccessError

    sid = access.create(ALICE)
    for target, principal in (("s-not-exist", ALICE), (sid, BOB)):
        with pytest.raises(SessionAccessError):
            access.assert_owner(target, principal)


def test_create_requires_scope(access):
    with pytest.raises(SessionForbiddenError):
        access.create(ANONYMOUS)  # 兼容主体零权限


def test_delete_session(access):
    sid = access.create(ALICE)
    assert access._repo.delete(sid) is True
    with pytest.raises(SessionNotFoundError):
        access.assert_owner(sid, ALICE)
