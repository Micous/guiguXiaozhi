"""会话归属服务 —— 把 memoryId 从「客户端自报」改成「服务端签发 + 归属绑定」。

两条设计约束：
  * session_id 用 CSPRNG 生成、带 ``s-`` 前缀，不可枚举；
  * 「不存在」与「不属于你」在路由层统一映射 403，不向攻击者泄露
    某个 session_id 是否存在（防枚举）。
"""

from __future__ import annotations

import secrets
from typing import Callable, Optional

from xiaozhi.repositories.session_repo import SessionRecord, SessionRepository, now_str
from xiaozhi.security.principal import SESSION_CREATE_SCOPE, Principal


class SessionAccessError(Exception):
    """会话访问被拒基类。"""


class SessionNotFoundError(SessionAccessError):
    """会话不存在（路由层统一 403，不区分于越权）。"""


class SessionForbiddenError(SessionAccessError):
    """会话属于别人，或缺少建会话权限。"""


class SessionAccess:
    def __init__(
        self,
        repository: SessionRepository,
        session_id_factory: Optional[Callable[[], str]] = None,
    ) -> None:
        self._repo = repository
        self._factory = session_id_factory or (
            lambda: "s-" + secrets.token_urlsafe(24)
        )

    def create(self, principal: Principal) -> str:
        """为已认证主体签发一个新会话。"""
        if not principal.has_scope(SESSION_CREATE_SCOPE):
            raise SessionForbiddenError("缺少 session:create 权限")
        session_id = self._factory()
        self._repo.create(
            SessionRecord(
                session_id=session_id,
                owner_subject_id=principal.subject_id,
                created_at=now_str(),
            )
        )
        return session_id

    def assert_owner(self, session_id: str, principal: Principal) -> None:
        """校验 memoryId 归属；失败抛异常，绝不返回「默认放行」。"""
        owner = self._repo.get_owner(session_id)
        if owner is None:
            raise SessionNotFoundError("会话不存在")
        if owner != principal.subject_id:
            raise SessionForbiddenError("会话不属于当前用户")

    def revoke(self, session_id: str, principal: Principal) -> bool:
        """撤销本人会话（审计 P0-01 验收 7）。

        以「物理删除归属记录」实现撤销：删除后 get_owner 返回 None，
        assert_owner 立即抛 SessionNotFoundError，旧 session_id 无法再读写。
        先查归属再删，保证任何人不能撤销别人的会话；返回 False 表示已不存在。
        """
        owner = self._repo.get_owner(session_id)
        if owner is None:
            return False
        if owner != principal.subject_id:
            raise SessionForbiddenError("会话不属于当前用户")
        return self._repo.delete(session_id)
