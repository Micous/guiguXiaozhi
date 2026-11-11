"""Phase 1 身份基础设施单测（纯离线，不依赖任何外部服务）。

钉死的安全红线：权限不可自报、算法固定 HS256、签名/过期/签发方任一不符即拒。
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import jwt
import pytest

from xiaozhi.security.auth import (
    BootstrapRejected,
    ExpiredTokenError,
    InvalidTokenError,
    TokenService,
    bearer_token,
    validate_subject_id,
)
from xiaozhi.security.principal import (
    ANONYMOUS,
    CHAT_WRITE_SCOPE,
    OPS_READ_SCOPE,
    PROFILE_FORGET_SCOPE,
    Principal,
    SESSION_CREATE_SCOPE,
)

SECRET = "test-secret-key-with-enough-length-1234567890"
BOOTSTRAP = "local-bootstrap-secret"
ISSUER = "xiaozhi-local-test"


@pytest.fixture
def service() -> TokenService:
    return TokenService(
        SECRET,
        ttl_seconds=3600,
        issuer=ISSUER,
        bootstrap_secret=BOOTSTRAP,
        ops_subjects=["ops-admin"],
    )


# ---------------------------------------------------------------- 签发/校验


def test_issue_verify_roundtrip(service):
    principal = service.verify(service.issue("alice"))
    assert principal.subject_id == "alice"
    assert principal.auth_method == "jwt"
    assert principal.has_scope(CHAT_WRITE_SCOPE)
    assert principal.has_scope(SESSION_CREATE_SCOPE)


def test_default_subject_has_no_ops_scope(service):
    """普通用户不能读运维端点，也不能删他人画像。"""
    principal = service.verify(service.issue("alice"))
    assert not principal.has_scope(OPS_READ_SCOPE)
    assert not principal.has_scope(PROFILE_FORGET_SCOPE)


def test_ops_subject_gets_ops_scopes(service):
    principal = service.verify(service.issue("ops-admin"))
    assert principal.has_scope(OPS_READ_SCOPE)
    assert principal.has_scope(PROFILE_FORGET_SCOPE)


def test_client_cannot_inject_scopes(service):
    """客户端自报 scopes 无效：服务端签发时根本不读客户端输入。"""
    forged = jwt.encode(
        {
            "sub": "alice",
            "scopes": [OPS_READ_SCOPE, PROFILE_FORGET_SCOPE],
            "roles": ["admin"],
            "iss": ISSUER,
            "iat": 1,
            "exp": 4102444800,
        },
        "a-completely-different-secret",
        algorithm="HS256",
    )
    with pytest.raises(InvalidTokenError):
        service.verify(forged)


def test_tampered_signature_rejected(service):
    token = service.issue("alice")
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    with pytest.raises(InvalidTokenError):
        service.verify(tampered)


def test_expired_token_rejected(service):
    """now=1 造出 exp 落在 1970 年的令牌。"""
    with pytest.raises(ExpiredTokenError):
        service.verify(service.issue("alice", now=1))


def test_wrong_issuer_rejected(service):
    other = TokenService(SECRET, issuer="another-issuer")
    with pytest.raises(InvalidTokenError):
        service.verify(other.issue("alice"))


def test_wrong_secret_rejected(service):
    other = TokenService("another-secret-key-with-enough-length-000", issuer=ISSUER)
    with pytest.raises(InvalidTokenError):
        service.verify(other.issue("alice"))


def test_alg_none_rejected(service):
    unsigned = jwt.encode(
        {"sub": "alice", "iss": ISSUER, "iat": 1, "exp": 4102444800, "scopes": []},
        key="",
        algorithm="none",
    )
    with pytest.raises(InvalidTokenError):
        service.verify(unsigned)


def test_missing_required_claims_rejected(service):
    no_exp = jwt.encode({"sub": "alice", "iss": ISSUER}, SECRET, algorithm="HS256")
    with pytest.raises(InvalidTokenError):
        service.verify(no_exp)


def test_empty_secret_rejected():
    with pytest.raises(ValueError):
        TokenService("")


# ---------------------------------------------------------------- 请求头/凭证


def test_bearer_token_parsing():
    assert bearer_token("Bearer abc.def.ghi") == "abc.def.ghi"
    assert bearer_token("bearer abc") == "abc"
    for bad in (None, "", "abc", "Basic abc", "Bearer ", "Bearer"):
        with pytest.raises(InvalidTokenError):
            bearer_token(bad)


def test_bootstrap_verify(service):
    service.verify_bootstrap(BOOTSTRAP)
    for bad in ("wrong", "", None):
        with pytest.raises(BootstrapRejected):
            service.verify_bootstrap(bad)


def test_bootstrap_requires_server_secret():
    with pytest.raises(BootstrapRejected):
        TokenService(SECRET).verify_bootstrap("anything")


def test_subject_id_validation(service):
    for bad in ("", "a b", "中文", "a" * 65, "x;drop table", "a/b"):
        with pytest.raises(ValueError):
            validate_subject_id(bad)
        with pytest.raises(ValueError):
            service.issue(bad)
    assert validate_subject_id(" u-1.2 ") == "u-1.2"


# ---------------------------------------------------------------- Principal


def test_principal_is_immutable():
    principal = Principal("alice")
    with pytest.raises(FrozenInstanceError):
        principal.subject_id = "bob"


def test_anonymous_has_no_scopes():
    """兼容路径的主体绝不能带任何权限。"""
    assert ANONYMOUS.subject_id == "anonymous"
    assert ANONYMOUS.scopes == frozenset()
    assert not ANONYMOUS.has_scope(CHAT_WRITE_SCOPE)


# ---------------------------------------------------------------- audience（审计 P0-01 验收 6）


def test_issued_token_carries_audience(service):
    payload = jwt.decode(
        service.issue("alice"), SECRET, algorithms=["HS256"], audience="xiaozhi-chat"
    )
    assert payload["aud"] == "xiaozhi-chat"


def test_wrong_audience_rejected(service):
    """给别的系统签发的 token（同密钥、同签发方）不得被本服务接受。"""
    other = TokenService(SECRET, issuer=ISSUER, audience="another-service")
    with pytest.raises(InvalidTokenError):
        service.verify(other.issue("alice"))


def test_missing_audience_rejected(service):
    import time

    now = int(time.time())
    token = jwt.encode(
        {
            "sub": "alice",
            "scopes": [],
            "roles": [],
            "iss": ISSUER,
            "iat": now,
            "exp": now + 3600,
        },
        SECRET,
        algorithm="HS256",
    )
    with pytest.raises(InvalidTokenError):
        service.verify(token)
