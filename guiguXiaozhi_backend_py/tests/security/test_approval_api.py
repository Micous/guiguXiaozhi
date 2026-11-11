"""P2-D：审批 API 与裸文本流 sentinel 的 HTTP 层验收。

用真实 FastAPI TestClient 走完整路由，只把组合根换成内存假件：
既能验 401/403/409 的真实语义与 contextvar 透传，又不依赖外部服务。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from xiaozhi.config import Settings
from xiaozhi.core.event import ApprovalRequiredEvent, TextResponseEvent
from xiaozhi.repositories.approval_repo import InMemoryApprovalRepository
from xiaozhi.repositories.session_repo import InMemorySessionRepository
from xiaozhi.security.approval_service import (
    ApprovalService,
    get_approval_context,
    reset_approval_context,
)
from xiaozhi.security.auth import TokenService
from xiaozhi.security.principal import DEFAULT_SCOPES, Principal
from xiaozhi.security.session_access import SessionAccess
from xiaozhi.server import app as app_module
from xiaozhi.server.routes import APPROVAL_SENTINEL_PREFIX, APPROVAL_SENTINEL_SUFFIX

SECRET = "approval-api-test-secret-key-with-enough-length-1234"
BOOTSTRAP = "approval-bootstrap"
ISSUER = "approval-test"
ALICE = Principal(subject_id="alice", scopes=DEFAULT_SCOPES, auth_method="jwt")
BOB = Principal(subject_id="bob", scopes=DEFAULT_SCOPES, auth_method="jwt")
ARGS = {"id_card": "11010119900307123X", "date": "2026-11-20"}


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
        self.contexts = []

    async def run(self, memory_id, message, user_id="anonymous"):
        # 捕获请求级审批上下文，验证 contextvar 确实穿过裸文本流生成器
        ctx = get_approval_context()
        self.contexts.append(
            (ctx.principal.subject_id, ctx.approval_token, ctx.session_id, ctx.auth_enabled)
        )
        yield TextResponseEvent(trace_id="t", metadata={"delta": "请确认本次操作"})
        yield ApprovalRequiredEvent(
            trace_id="t",
            metadata={
                "decision": "REQUIRE_APPROVAL",
                "detail": {"approval_id": "ap-test", "tool_name": "预约挂号", "status": "PENDING"},
            },
        )


async def _ready() -> dict:
    return {"ready": True}


class _FakeContainer:
    def __init__(self, settings, token_service, session_access, approval_service) -> None:
        self.settings = settings
        self.token_service = token_service
        self.session_access = session_access
        self.approval_service = approval_service
        self.profile_service = None
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


def _build(monkeypatch, approval_enabled: bool = True, auth_enabled: bool = True):
    settings = Settings(
        _env_file=None,
        auth_enabled=auth_enabled,
        auth_secret_key=SECRET if auth_enabled else "",
        auth_bootstrap_secret=BOOTSTRAP if auth_enabled else "",
        auth_issuer=ISSUER,
        auth_token_ttl_seconds=3600,
        approval_enabled=approval_enabled,
        approval_ttl_seconds=120.0,
        enable_rate_limit=False,
        permission_no_ui="deny",
    )
    token_service = (
        TokenService(
            SECRET,
            ttl_seconds=3600,
            issuer=ISSUER,
            bootstrap_secret=BOOTSTRAP,
            ops_subjects=[],
        )
        if auth_enabled
        else None
    )
    session_access = SessionAccess(InMemorySessionRepository()) if auth_enabled else None
    approval_service = (
        ApprovalService(InMemoryApprovalRepository(), ttl_seconds=120.0)
        if approval_enabled
        else None
    )
    container = _FakeContainer(settings, token_service, session_access, approval_service)

    async def fake_get_application(settings=None):
        return container

    monkeypatch.setattr(app_module, "get_application", fake_get_application)
    return TestClient(app_module.create_app()), container, token_service, approval_service


@pytest.fixture(autouse=True)
def _clean_context():
    reset_approval_context()
    yield
    reset_approval_context()


@pytest.fixture
def api(monkeypatch):
    client, container, tokens, service = _build(monkeypatch)
    with client as c:
        yield c, container, tokens, service


@pytest.fixture
def api_off(monkeypatch):
    client, container, _, _ = _build(monkeypatch, approval_enabled=False)
    with client as c:
        yield c, container


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _new_session(client, token: str) -> str:
    resp = client.post("/v1/sessions", headers=_headers(token))
    assert resp.status_code == 200, resp.text
    return resp.json()["session_id"]


def _pending(service, principal=ALICE, session_id="s1") -> str:
    record = service.create(principal, session_id, "预约挂号", dict(ARGS))
    return record.approval_id


# ---------------------------------------------------------------- 裸文本流 sentinel


def test_chat_stream_emits_parseable_approval_sentinel(api):
    client, _, tokens, _ = api
    sid = _new_session(client, tokens.issue("alice"))
    resp = client.post(
        "/xiaozhi/chat",
        json={"memoryId": sid, "message": "帮我预约挂号"},
        headers=_headers(tokens.issue("alice")),
    )
    assert resp.status_code == 200, resp.text
    text = resp.text
    assert "请确认本次操作" in text
    assert APPROVAL_SENTINEL_PREFIX in text and APPROVAL_SENTINEL_SUFFIX in text
    import json

    body = text.split(APPROVAL_SENTINEL_PREFIX)[1].split(APPROVAL_SENTINEL_SUFFIX)[0]
    payload = json.loads(body)
    assert payload["decision"] == "REQUIRE_APPROVAL"
    assert payload["detail"]["tool_name"] == "预约挂号"


def test_events_mode_emits_approval_required_frame(api):
    client, _, tokens, _ = api
    sid = _new_session(client, tokens.issue("alice"))
    resp = client.post(
        "/xiaozhi/chat?format=events",
        json={"memoryId": sid, "message": "帮我预约挂号"},
        headers=_headers(tokens.issue("alice")),
    )
    assert resp.status_code == 200
    assert '"event_type": "approval_required"' in resp.text


def test_chat_passes_approval_token_through_contextvar(api):
    """审批 token 只从请求体读入，经 contextvar 送到执行层，模型输出碰不到。"""
    client, container, tokens, service = api
    sid = _new_session(client, tokens.issue("alice"))
    token = service.approve(_pending(service, session_id=sid), ALICE)
    resp = client.post(
        "/xiaozhi/chat",
        json={
            "memoryId": sid,
            "message": "帮我预约挂号",
            "approvalToken": token,
        },
        headers=_headers(tokens.issue("alice")),
    )
    assert resp.status_code == 200
    subject, seen_token, session, auth_enabled = container.engine.contexts[-1]
    assert subject == "alice"
    assert seen_token == token
    assert session == sid
    assert auth_enabled is True


def test_chat_context_is_reset_between_requests(api):
    client, container, tokens, service = api
    sid = _new_session(client, tokens.issue("alice"))
    h = _headers(tokens.issue("alice"))
    token = service.approve(_pending(service, session_id=sid), ALICE)
    client.post(
        "/xiaozhi/chat",
        json={"memoryId": sid, "message": "a", "approvalToken": token},
        headers=h,
    )
    client.post("/xiaozhi/chat", json={"memoryId": sid, "message": "b"}, headers=h)
    assert container.engine.contexts[-2][1] == token
    assert container.engine.contexts[-1][1] == ""


# ---------------------------------------------------------------- 审批 API


def test_approval_endpoints_require_token(api):
    client, _, _, service = api
    assert client.get("/v1/approvals").status_code == 401
    assert client.post("/v1/approvals/ap-x/approve").status_code == 401


def test_approval_endpoints_hidden_when_disabled(api_off):
    client, _ = api_off
    assert client.get("/v1/approvals").status_code == 404
    assert client.post("/v1/approvals/ap-x/approve").status_code == 404


def test_list_and_detail_only_show_owner_records(api):
    client, _, tokens, service = api
    approval_id = _pending(service)
    _pending(service, principal=BOB)
    alice = _headers(tokens.issue("alice"))
    bob = _headers(tokens.issue("bob"))

    items = client.get("/v1/approvals", headers=alice).json()["items"]
    assert [i["approval_id"] for i in items] == [approval_id]
    assert client.get(f"/v1/approvals/{approval_id}", headers=alice).status_code == 200
    # 跨用户查询统一 403，不泄露这条 id 属于谁
    assert client.get(f"/v1/approvals/{approval_id}", headers=bob).status_code == 403


def test_approve_returns_one_time_token_and_rejects_second_call(api):
    client, _, tokens, service = api
    approval_id = _pending(service)
    alice = _headers(tokens.issue("alice"))
    resp = client.post(f"/v1/approvals/{approval_id}/approve", headers=alice)
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["approval_token"]) >= 32
    again = client.post(f"/v1/approvals/{approval_id}/approve", headers=alice)
    assert again.status_code == 409
    assert again.json()["reason_code"] == "not_pending"


def test_cross_user_approve_and_revoke_forbidden(api):
    client, _, tokens, service = api
    approval_id = _pending(service)
    bob = _headers(tokens.issue("bob"))
    assert client.post(f"/v1/approvals/{approval_id}/approve", headers=bob).status_code == 403
    assert client.post(f"/v1/approvals/{approval_id}/revoke", headers=bob).status_code == 403


def test_revoke_prevents_later_approval(api):
    client, _, tokens, service = api
    approval_id = _pending(service)
    alice = _headers(tokens.issue("alice"))
    assert client.post(f"/v1/approvals/{approval_id}/revoke", headers=alice).status_code == 200
    assert client.post(f"/v1/approvals/{approval_id}/approve", headers=alice).status_code == 409


# ---------------------------------------------------------------- HTTP 层 token 预检


def test_chat_rejects_unknown_approval_token_before_model_runs(api):
    client, container, tokens, _ = api
    sid = _new_session(client, tokens.issue("alice"))
    jwt = tokens.issue("alice")
    resp = client.post(
        "/xiaozhi/chat",
        json={"memoryId": sid, "message": "帮我预约挂号", "approvalToken": jwt},
        headers=_headers(jwt),
    )
    # 这里故意把 JWT 当审批 token 用；预检应在进模型前 409
    assert resp.status_code == 409
    assert "invalid_or_replayed" in resp.text
    assert container.engine.contexts == []


def test_chat_rejects_replayed_approval_token(api):
    """真机验证发现：重放 token 若不拦，模型会照抄历史里的「预约成功」。"""
    client, container, tokens, service = api
    sid = _new_session(client, tokens.issue("alice"))
    token = service.approve(_pending(service, session_id=sid), ALICE)
    h = _headers(tokens.issue("alice"))  # 固定 headers，避免混淆 JWT 与审批 token

    first = client.post(
        "/xiaozhi/chat",
        json={"memoryId": sid, "message": "帮我预约挂号", "approvalToken": token},
        headers=h,
    )
    assert first.status_code == 200
    # 模拟执行器已经把这条审批消费掉
    service.consume(
        token, ALICE, session_id=sid, tool_name="预约挂号", canonical_args=dict(ARGS)
    )
    second = client.post(
        "/xiaozhi/chat",
        json={"memoryId": sid, "message": "帮我预约挂号", "approvalToken": token},
        headers=h,
    )
    assert second.status_code == 409
    assert "失效" in second.text
    assert len(container.engine.contexts) == 1  # 第二次根本没进模型


def test_chat_rejects_cross_user_approval_token(api):
    client, _, tokens, service = api
    sid = _new_session(client, tokens.issue("alice"))
    token = service.approve(_pending(service, session_id=sid), ALICE)
    bob_token = tokens.issue("bob")
    bob_session = client.post("/v1/sessions", headers=_headers(bob_token)).json()["session_id"]
    resp = client.post(
        "/xiaozhi/chat",
        json={"memoryId": bob_session, "message": "帮我预约挂号", "approvalToken": token},
        headers=_headers(bob_token),
    )
    assert resp.status_code == 409
    assert "forbidden" in resp.text


def test_chat_without_token_is_unaffected(api):
    """普通对话（无 approvalToken）不经过预检，行为零变更。"""
    client, container, tokens, _ = api
    sid = _new_session(client, tokens.issue("alice"))
    resp = client.post(
        "/xiaozhi/chat",
        json={"memoryId": sid, "message": "你好"},
        headers=_headers(tokens.issue("alice")),
    )
    assert resp.status_code == 200
    assert "请确认本次操作" in resp.text


def test_api_alias_and_args_are_masked(api):
    client, _, tokens, service = api
    approval_id = _pending(service)
    alice = _headers(tokens.issue("alice"))
    detail = client.get(f"/api/v1/approvals/{approval_id}", headers=alice).json()
    assert detail["args"]["id_card"] != ARGS["id_card"]
    assert "*" in detail["args"]["id_card"]
    assert "approval_token" not in detail
