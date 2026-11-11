"""P0-01 运维信息面收口：认证开启时自动文档端点不得暴露。

/docs、/redoc、/openapi.json 会泄露全部端点与参数结构，
与 /metrics /audit /traces 需要 ops:read 的口径必须一致。
认证关闭（默认、开发态）保持原样。
"""

from __future__ import annotations

from xiaozhi.config import reset_settings
from xiaozhi.server.app import create_app


def _registered_paths() -> set:
    return {getattr(route, "path", "") for route in create_app().routes}


def test_docs_hidden_when_auth_enabled(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    reset_settings()
    try:
        paths = _registered_paths()
    finally:
        monkeypatch.delenv("AUTH_ENABLED")
        reset_settings()
    for path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
        assert path not in paths, f"{path} 在认证开启时仍被注册"


def test_docs_available_in_development(monkeypatch):
    """红线：默认（认证关闭）行为与一期完全一致，文档照常可用。"""
    # 显式置 false（而非 delenv）：本机 .env 可能开启了认证，delenv 会让它生效
    monkeypatch.setenv("AUTH_ENABLED", "false")
    reset_settings()
    try:
        paths = _registered_paths()
    finally:
        reset_settings()
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert path in paths
