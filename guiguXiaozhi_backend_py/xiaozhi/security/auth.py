"""本地 JWT 认证（Phase 1 过渡方案）。

正式环境切换 OIDC / 网关时只需替换「签发方」：``TokenService.verify``
返回的 ``Principal`` 契约不变，业务层与路由层零改动。

安全约束：
  * **不手写加密**：HS256 的签名与校验交给 PyJWT；
  * **权限不可自报**：token 里的 scopes 由服务端按 subject_id 计算
    （``scopes_for_subject``），客户端传什么都不会进 token；
  * **校验算法固定**：只接受 HS256，杜绝 alg=none / 算法混淆；
  * **校验受众**：``aud`` 必须与本服务一致，别处签发的 token 不接受；
  * ``bootstrap_secret`` 仅用于内网演示换 token，比较用 ``compare_digest``
    防时序侧信道；正式环境必须换掉这个端点。
"""

from __future__ import annotations

import hmac
import re
import secrets
import time
from typing import FrozenSet, Iterable, Optional

import jwt

from xiaozhi.security.principal import (
    DEFAULT_SCOPES,
    OPS_READ_SCOPE,
    PROFILE_FORGET_SCOPE,
    Principal,
)

_SUBJECT_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")


class AuthError(Exception):
    """认证失败基类（路由层据此映射 401）。"""


class InvalidTokenError(AuthError):
    """缺失、格式错误、签名不符或 claim 不合法。"""


class ExpiredTokenError(AuthError):
    """令牌已过期。"""


class BootstrapRejected(AuthError):
    """换取 token 的本地凭证不正确。"""


def bearer_token(authorization: Optional[str]) -> str:
    """从 Authorization 头提取 Bearer token。"""
    if not authorization:
        raise InvalidTokenError("缺少 Authorization 头")
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise InvalidTokenError("Authorization 必须是 'Bearer <token>'")
    return parts[1].strip()


def validate_subject_id(subject_id: str) -> str:
    """主体标识白名单：防止把任意字符串/注入内容写进 token 与会话表。"""
    value = (subject_id or "").strip()
    if not _SUBJECT_RE.fullmatch(value):
        raise ValueError("subject_id 只能是 1-64 位字母、数字、_ . -")
    return value


class TokenService:
    """签发与校验 HS256 JWT。"""

    def __init__(
        self,
        secret_key: str,
        ttl_seconds: int = 3600,
        issuer: str = "xiaozhi-local",
        audience: str = "xiaozhi-chat",
        bootstrap_secret: str = "",
        ops_subjects: Iterable[str] = (),
    ) -> None:
        if not secret_key or not secret_key.strip():
            raise ValueError("auth_secret_key 不能为空")
        self._secret = secret_key
        self._ttl = max(1, int(ttl_seconds))
        self._issuer = issuer
        self._audience = audience
        self._bootstrap_secret = bootstrap_secret
        self._ops_subjects = frozenset(ops_subjects or ())
        self._algorithm = "HS256"

    def scopes_for_subject(self, subject_id: str) -> FrozenSet[str]:
        """服务端决定权限：只有 ops 白名单主体能读运维端点 / 删画像。"""
        scopes = set(DEFAULT_SCOPES)
        if subject_id in self._ops_subjects:
            scopes.add(OPS_READ_SCOPE)
            scopes.add(PROFILE_FORGET_SCOPE)
        return frozenset(scopes)

    def verify_bootstrap(self, provided: str) -> None:
        """本地过渡端点用：校验换取 token 的共享凭证。"""
        if not self._bootstrap_secret:
            raise BootstrapRejected("服务端未配置 auth_bootstrap_secret")
        if not hmac.compare_digest(provided or "", self._bootstrap_secret):
            raise BootstrapRejected("凭证不正确")

    def issue(self, subject_id: str, *, now: Optional[int] = None) -> str:
        """签发 token。``now`` 仅供测试构造过期令牌。"""
        subject = validate_subject_id(subject_id)
        ts = int(time.time()) if now is None else int(now)
        payload = {
            "sub": subject,
            "scopes": sorted(self.scopes_for_subject(subject)),
            "roles": [],
            "iss": self._issuer,
            "aud": self._audience,
            "iat": ts,
            "exp": ts + self._ttl,
            "jti": secrets.token_hex(8),
        }
        return jwt.encode(payload, self._secret, algorithm=self._algorithm)

    def verify(self, token: str) -> Principal:
        """校验签名/过期/签发方/受众，返回不可变身份主体。"""
        if not token or not token.strip():
            raise InvalidTokenError("token 为空")
        try:
            payload = jwt.decode(
                token.strip(),
                self._secret,
                algorithms=[self._algorithm],  # 固定算法，杜绝 alg 混淆
                issuer=self._issuer,
                audience=self._audience,
                options={"require": ["exp", "iat", "sub", "iss", "aud"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise ExpiredTokenError("令牌已过期") from exc
        except jwt.InvalidTokenError as exc:
            raise InvalidTokenError(f"令牌无效：{exc}") from exc

        sub = payload.get("sub")
        if not isinstance(sub, str) or not sub.strip():
            raise InvalidTokenError("令牌缺少合法 sub")
        scopes = payload.get("scopes") or []
        roles = payload.get("roles") or []
        if not isinstance(scopes, list) or not isinstance(roles, list):
            raise InvalidTokenError("令牌 claims 类型不合法")
        return Principal(
            subject_id=sub,
            roles=frozenset(str(r) for r in roles),
            scopes=frozenset(str(s) for s in scopes),
            auth_method="jwt",
        )
