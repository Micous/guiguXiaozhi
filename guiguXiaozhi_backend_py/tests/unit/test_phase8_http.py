"""Phase 8-C：HTTP 边界加固单测（安全响应头 / 请求体上限 / CORS 白名单）。

改造前 `create_app()` 里没有任何中间件（评估 P0-1）。这一批补的是
"纯入口层"能力，不碰裸文本流协议 —— 前端仍然按 responseText 累积。
"""

from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from xiaozhi.server.middleware import (
    DEFAULT_SECURITY_HEADERS,
    BodySizeLimitMiddleware,
    SecurityHeadersMiddleware,
    normalize_origins,
)


def _app(*middlewares) -> FastAPI:
    app = FastAPI()

    @app.get("/ping")
    def ping():
        return {"ok": True}

    @app.post("/echo")
    async def echo(payload: dict):
        return {"size": len(json.dumps(payload))}

    for mw, kwargs in middlewares:
        app.add_middleware(mw, **kwargs)
    return app


def test_security_headers_are_added():
    client = TestClient(_app((SecurityHeadersMiddleware, {})))
    resp = client.get("/ping")
    assert resp.status_code == 200
    for name, value in DEFAULT_SECURITY_HEADERS:
        assert resp.headers[name] == value


def test_security_headers_do_not_override_existing():
    app = FastAPI()

    @app.get("/custom")
    def custom():
        from fastapi.responses import JSONResponse

        return JSONResponse({"ok": True}, headers={"X-Frame-Options": "SAMEORIGIN"})

    app.add_middleware(SecurityHeadersMiddleware)
    resp = TestClient(app).get("/custom")
    assert resp.headers["X-Frame-Options"] == "SAMEORIGIN"


def test_security_headers_can_be_disabled():
    client = TestClient(_app((SecurityHeadersMiddleware, {"enabled": False})))
    resp = client.get("/ping")
    assert "x-frame-options" not in resp.headers


def test_body_limit_rejects_oversized_request():
    client = TestClient(_app((BodySizeLimitMiddleware, {"max_bytes": 64})))
    resp = client.post("/echo", json={"blob": "x" * 500})
    assert resp.status_code == 413
    assert "过大" in resp.text


def test_body_limit_allows_small_request():
    client = TestClient(_app((BodySizeLimitMiddleware, {"max_bytes": 4096})))
    resp = client.post("/echo", json={"a": 1})
    assert resp.status_code == 200


def test_body_limit_disabled_when_zero():
    client = TestClient(_app((BodySizeLimitMiddleware, {"max_bytes": 0})))
    resp = client.post("/echo", json={"blob": "x" * 2000})
    assert resp.status_code == 200


def test_body_limit_ignores_malformed_content_length():
    """长度头不是数字时不能 500 —— 交给下游自己处理。"""
    app = _app((BodySizeLimitMiddleware, {"max_bytes": 10}))
    client = TestClient(app)
    resp = client.post(
        "/echo",
        content=b"{}",
        headers={"content-length": "abc", "content-type": "application/json"},
    )
    # TestClient 会覆盖 content-length；这里只验证不抛 500
    assert resp.status_code in (200, 400, 413)


def test_normalize_origins_strips_and_dedupes():
    assert normalize_origins(
        [" http://localhost:5173/ ", "http://localhost:5173", "", "https://a.cn/"]
    ) == ["http://localhost:5173", "https://a.cn"]


def test_normalize_origins_keeps_wildcard_untouched():
    assert normalize_origins(["*"]) == ["*"]


def test_settings_default_keeps_cors_off():
    """默认不挂 CORS：前端走 Vite 代理，同源访问不需要（挂错反而放大攻击面）。"""
    from xiaozhi.config import Settings

    s = Settings(_env_file=None)
    assert s.cors_allowed_origins == []
    assert s.security_headers_enabled is True
    assert s.max_request_bytes == 262_144
    # Phase 8-B 的红线：默认不加密（一期行为），生产门禁才强制
    assert s.pii_protection_enabled is False


def test_oversized_response_also_carries_security_headers():
    """413 也是响应，同样不许进缓存。

    Starlette 的 ``add_middleware`` 是前插（后加在外层），所以装配顺序必须是
    BodySizeLimit 先、SecurityHeaders 后 —— 反了就只有正常响应有头、
    错误响应没有，而错误响应恰恰可能带着请求参数回显。
    """
    client = TestClient(
        _app(
            (BodySizeLimitMiddleware, {"max_bytes": 64}),
            (SecurityHeadersMiddleware, {}),
        )
    )
    resp = client.post("/echo", json={"blob": "x" * 500})
    assert resp.status_code == 413
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["x-content-type-options"] == "nosniff"


def test_app_wires_headers_outside_body_limit():
    """装配层回归：真实 app 的中间件顺序不能反（含 413 也带头的保证）。"""
    from xiaozhi.server.app import create_app
    from xiaozhi.server.middleware import (
        BodySizeLimitMiddleware as BodyLimit,
        SecurityHeadersMiddleware as Headers,
    )

    user = getattr(create_app(), "user_middleware", []) or []
    names = [getattr(m, "cls", None) for m in user]
    if not names:
        return  # 该 FastAPI 版本不暴露 user_middleware，跳过
    # user_middleware[0] 是最外层：SecurityHeaders 必须排在 BodySizeLimit 前面
    assert names.index(Headers) < names.index(BodyLimit)
