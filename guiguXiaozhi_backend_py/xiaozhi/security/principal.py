"""身份主体 —— 认证层与业务层之间唯一的身份契约。

Phase 1 之前，``memoryId`` / ``userId`` 由请求体自报，任何访客都能冒充。
现在所有需要判断「这是谁」的地方只认 ``Principal``：它由 TokenService
校验签名后生成，业务层不再相信请求体里的身份字段。

scope 常量集中在此，避免路由层散落字符串字面量导致拼错即静默放行。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import FrozenSet

CHAT_WRITE_SCOPE = "chat:write"
SESSION_CREATE_SCOPE = "session:create"
OPS_READ_SCOPE = "ops:read"
PROFILE_FORGET_SCOPE = "profile:forget"

# 普通登录主体只具备聊天与建会话权限；运维与「忘记我」是额外授予的。
DEFAULT_SCOPES: FrozenSet[str] = frozenset({CHAT_WRITE_SCOPE, SESSION_CREATE_SCOPE})


@dataclass(frozen=True)
class Principal:
    """一次请求的已认证身份（不可变，防止下游被篡改）。"""

    subject_id: str
    roles: FrozenSet[str] = frozenset()
    scopes: FrozenSet[str] = field(default_factory=lambda: DEFAULT_SCOPES)
    auth_method: str = "jwt"

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes

    @classmethod
    def anonymous(cls) -> "Principal":
        """仅用于 ``auth_enabled=false`` 的兼容路径，**不得**用于任何授权判断。"""
        return cls(subject_id="anonymous", scopes=frozenset(), auth_method="none")


ANONYMOUS = Principal.anonymous()
