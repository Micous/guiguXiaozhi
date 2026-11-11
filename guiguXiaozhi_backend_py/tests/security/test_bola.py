"""P0-01 验收：HTTP 层跨用户访问（BOLA / IDOR）必须被拒。

用真实 FastAPI TestClient 走完整路由，只把组合根换成内存假件：
既能验 401/403 的真实 HTTP 语义，又不依赖 Ollama / Milvus / MySQL。

两条红线：
  1. auth_enabled=true：A 用 B 的 session_id 必须 403；身份只认 token；
  2. auth_enabled=false：行为与一期完全一致（无 token、任意 memoryId 可用）。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from xiaozhi.config import Settings
from xiaozhi.core.event import TextResponseEvent
from xiaozhi.repositories.session_repo import InMemorySessionRepository
from xiaozhi.security.auth import TokenService
from xiaozhi.security.session_access import SessionAccess
from xiaozhi.server import app as app_module

SECRET = "bola-test-secret-key-with-enough-length-1234567890"
BOOTSTRAP = "bola-bootstrap-secret"
ISSUER = "bola-test"
OPS_SUBJECT = "ops-admin"


class _FakeShutdown:
    shutting_down = False

    def begin(self) -> None:
        pass

    async def wait_until_drained(self) -> bool:
        return True

    def snapshot(self) -> dict:
        return {}

    @asynccontextmanager
    async def inflight_request(self):
        yield


class _FakeEngine:
    circuit_breaker = None

    def __init__(self) -> None:
        self.calls = []

    async def run(self, memory_id, message, user_id="anonymous"):
        self.calls.append((memory_id, message, user_id))
        yield TextResponseEvent(trace_id="t", metadata={"delta": "ok"})


class _FakeProfileService:
    def __init__(self) -> None:
        self.forgotten = []

    async def forget(self, user_key: str) -> int:
        self.forgotten.append(user_key)
        return 3


async def _ready() -> dict:
    return {"ready": True}


class _FakeContainer:
    def __init__(self, settings, token_service, session_access) -> None:
        self.settings = settings
        self.token_service = token_service
        self.session_access = session_access
        self.profile_service = _FakeProfileService()
        self.shutdown = _FakeShutdown()
        self.engine = _FakeEngine()
        self.chat_model = SimpleNamespace(model="fake-model")
        self.metrics = SimpleNamespace(
            inc=lambda *a, **k: None, summary=lambda: {}, snapshot=lambda: {}
        )
        self.rate_limiter = SimpleNamespace(allow=lambda k: True, snapshot=lambda: {})
        self.audit = SimpleNamespace(read_recent=lambda limit=50: [], close=lambda: None)
        self.trace_store = SimpleNamespace(
            list_traces=lambda limit=50: [], get_trace_tree=lambda tid: None
        )
        self.tool_registry = SimpleNamespace(list_definitions=lambda: [])
        self.health = SimpleNamespace(check_all=_ready)

    async def aclose(self) -> None:
        pass


def _build(monkeypatch, enabled: bool):
    settings = Settings(
        _env_file=None,
        auth_enabled=enabled,
        auth_secret_key=SECRET if enabled else "",
        auth_bootstrap_secret=BOOTSTRAP if enabled else "",
        auth_issuer=ISSUER,
        auth_token_ttl_seconds=3600,
        auth_ops_subjects=[OPS_SUBJECT],
        enable_rate_limit=False,
    )
    token_service = (
        TokenService(
            SECRET,
            ttl_seconds=3600,
            issuer=ISSUER,
            bootstrap_secret=BOOTSTRAP,
            ops_subjects=[OPS_SUBJECT],
        )
        if enabled
        else None
    )
    session_access = SessionAccess(InMemorySessionRepository()) if enabled else None
    container = _FakeContainer(settings, token_service, session_access)

    async def fake_get_application(settings=None):
        return container

    monkeypatch.setattr(app_module, "get_application", fake_get_application)
    return TestClient(app_module.create_app()), container, token_service, session_access


@pytest.fixture
def auth_on(monkeypatch):
    client, container, tokens, sessions = _build(monkeypatch, True)
    with client as c:
        yield c, container, tokens, sessions


@pytest.fixture
def auth_off(monkeypatch):
    client, container, _, _ = _build(monkeypatch, False)
    with client as c:
        yield c, container


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _new_session(client, token: str) -> str:
    resp = client.post("/v1/sessions", headers=_headers(token))
    assert resp.status_code == 200, resp.text
    return resp.json()["session_id"]


# ---------------------------------------------------------------- 对话归属


def test_chat_requires_token(auth_on):
    client, *_ = auth_on
    resp = client.post("/xiaozhi/chat", json={"memoryId": "anything", "message": "你好"})
    assert resp.status_code == 401


def test_chat_rejects_garbage_token(auth_on):
    client, *_ = auth_on
    resp = client.post(
        "/xiaozhi/chat",
        json={"memoryId": "anything", "message": "你好"},
        headers=_headers("not.a.jwt"),
    )
    assert resp.status_code == 401


def test_chat_rejects_unknown_session(auth_on):
    client, _, tokens, _ = auth_on
    token = tokens.issue("alice")
    resp = client.post(
        "/xiaozhi/chat",
        json={"memoryId": "s-never-issued", "message": "你好"},
        headers=_headers(token),
    )
    assert resp.status_code == 403


def test_chat_cross_user_session_forbidden(auth_on):
    """核心 BOLA：B 拿 A 的 session_id 读历史/继续对话必须 403。"""
    client, _, tokens, _ = auth_on
    alice_session = _new_session(client, tokens.issue("alice"))
    bob_token = tokens.issue("bob")
    resp = client.post(
        "/xiaozhi/chat",
        json={"memoryId": alice_session, "message": "继续我们刚才的对话"},
        headers=_headers(bob_token),
    )
    assert resp.status_code == 403


def test_chat_owner_ok_and_identity_from_token(auth_on):
    """合法请求：engine 收到的 user_id 必须来自 token，而非请求体自报。"""
    client, container, tokens, _ = auth_on
    session_id = _new_session(client, tokens.issue("alice"))
    resp = client.post(
        "/xiaozhi/chat",
        json={
            "memoryId": session_id,
            "message": "你好",
            "userId": "attacker",  # 自报身份必须被忽略
        },
        headers=_headers(tokens.issue("alice")),
    )
    assert resp.status_code == 200, resp.text
    assert resp.text == "ok"
    assert container.engine.calls[-1][2] == "alice"


def test_session_ids_are_not_enumerable(auth_on):
    client, _, tokens, _ = auth_on
    first, second = _new_session(client, tokens.issue("alice")), _new_session(
        client, tokens.issue("alice")
    )
    assert first != second and first.startswith("s-") and len(first) >= 30


# ---------------------------------------------------------------- 运维 / 画像端点


@pytest.mark.parametrize(
    "path",
    ["/metrics", "/audit", "/traces", "/tools", "/trace", "/trace/some-id"],
)
def test_ops_endpoints_reject_normal_user(auth_on, path):
    client, _, tokens, _ = auth_on
    resp = client.get(path, headers=_headers(tokens.issue("alice")))
    assert resp.status_code == 403, (path, resp.status_code)


@pytest.mark.parametrize(
    "path",
    ["/metrics", "/audit", "/traces", "/tools", "/trace"],
)
def test_ops_endpoints_reject_anonymous(auth_on, path):
    client, *_ = auth_on
    assert client.get(path).status_code == 401


def test_ops_subject_can_read_metrics(auth_on):
    client, _, tokens, _ = auth_on
    resp = client.get("/metrics", headers=_headers(tokens.issue(OPS_SUBJECT)))
    assert resp.status_code == 200, resp.text


def test_forget_profile_requires_scope(auth_on):
    client, _, tokens, _ = auth_on
    assert (
        client.delete("/profile/someone-else-key", headers=_headers(tokens.issue("alice"))).status_code
        == 403
    )
    assert client.delete("/profile/someone-else-key").status_code == 401


def test_ops_subject_can_forget_profile(auth_on):
    client, container, tokens, _ = auth_on
    resp = client.delete(
        "/profile/someone-else-key", headers=_headers(tokens.issue(OPS_SUBJECT))
    )
    assert resp.status_code == 200, resp.text
    assert container.profile_service.forgotten == ["someone-else-key"]


def test_health_and_ready_stay_public(auth_on):
    """探针不能被认证挡住，否则容器编排会判服务不健康。"""
    client, *_ = auth_on
    assert client.get("/health").status_code == 200
    assert client.get("/ready").status_code == 200


# ---------------------------------------------------------------- 签发端点


def test_token_endpoint_validates_bootstrap(auth_on):
    client, *_ = auth_on
    assert (
        client.post("/v1/auth/token", json={"subject_id": "alice", "secret": "wrong"}).status_code
        == 401
    )
    resp = client.post(
        "/v1/auth/token", json={"subject_id": "alice", "secret": BOOTSTRAP}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["token_type"] == "Bearer"


def test_token_endpoint_rejects_bad_subject(auth_on):
    client, *_ = auth_on
    resp = client.post(
        "/v1/auth/token", json={"subject_id": "bad subject;", "secret": BOOTSTRAP}
    )
    assert resp.status_code == 400


def test_token_endpoint_cannot_grant_ops_scope(auth_on):
    """客户端多传 scopes/roles 也不会提权（服务端按 subject_id 计算）。"""
    client, _, tokens, _ = auth_on
    resp = client.post(
        "/v1/auth/token",
        json={
            "subject_id": "alice",
            "secret": BOOTSTRAP,
            "scopes": ["ops:read"],
            "roles": ["admin"],
        },
    )
    assert resp.status_code == 200, resp.text
    principal = tokens.verify(resp.json()["access_token"])
    assert not principal.has_scope("ops:read")
    assert principal.roles == frozenset()


def test_direct_backend_alias_works(auth_on):
    """不经 Vite 代理时 /api/v1/... 别名同样可达。"""
    client, *_ = auth_on
    resp = client.post(
        "/api/v1/auth/token", json={"subject_id": "alice", "secret": BOOTSTRAP}
    )
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------- 灰度红线


def test_auth_disabled_keeps_legacy_behaviour(auth_off):
    """红线：默认关闭时，无 token / 任意 memoryId / 运维端点全部照旧。"""
    client, container = auth_off
    resp = client.post(
        "/xiaozhi/chat",
        json={"memoryId": "whatever", "message": "你好", "userId": "someone"},
    )
    assert resp.status_code == 200, resp.text
    assert container.engine.calls[-1][2] == "someone"  # 一期行为：请求体自报
    assert client.get("/metrics").status_code == 200
    assert client.get("/tools").status_code == 200
    assert client.delete("/profile/any-key").status_code == 200
    assert client.post("/v1/sessions").status_code == 404
    assert client.post("/v1/auth/token", json={}).status_code == 404



# ---------------------------------------------------------------- 会话撤销（P0-01 验收 7）


def test_revoke_session_makes_old_id_unusable(auth_on):
    client, _, tokens, _ = auth_on
    alice = tokens.issue("alice")
    sid = _new_session(client, alice)

    resp = client.delete(f"/v1/sessions/{sid}", headers=_headers(alice))
    assert resp.status_code == 200
    assert resp.json()["revoked"] is True

    chat = client.post(
        "/xiaozhi/chat",
        json={"memoryId": sid, "message": "你好"},
        headers=_headers(alice),
    )
    assert chat.status_code == 403


def test_revoke_other_users_session_forbidden(auth_on):
    client, _, tokens, _ = auth_on
    alice, bob = tokens.issue("alice"), tokens.issue("bob")
    sid = _new_session(client, alice)

    resp = client.delete(f"/v1/sessions/{sid}", headers=_headers(bob))
    assert resp.status_code == 403

    chat = client.post(
        "/xiaozhi/chat",
        json={"memoryId": sid, "message": "你好"},
        headers=_headers(alice),
    )
    assert chat.status_code == 200


def test_revoke_requires_token(auth_on):
    client, *_ = auth_on
    assert client.delete("/v1/sessions/s-whatever").status_code == 401
