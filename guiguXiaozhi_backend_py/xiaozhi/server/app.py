"""FastAPI 应用 —— 接入层。

要点：**响应必须是裸文本分块**（text/stream;charset=utf-8），
与 Java 版保持一致，前端（axios onDownloadProgress 累积 responseText）零改动可用。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, Dict

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from xiaozhi.application import get_application
from xiaozhi.config import get_settings
from xiaozhi.core.message import Role
from xiaozhi.server.middleware import (
    BodySizeLimitMiddleware,
    DEFAULT_SECURITY_HEADERS,
    SecurityHeadersMiddleware,
    normalize_origins,
)

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    """创建应用（组合根在 lifespan 里装配）"""
    # 认证开启时不注册 FastAPI 自动文档：/docs /openapi.json 会泄露全部
    # 端点与参数结构，必须与 /metrics /audit /traces 一样收口。
    # 认证关闭（默认、开发态）保持原样，行为零变更。
    auth_enabled = get_settings().auth_enabled
    app = FastAPI(
        title="硅谷小智 · Python 后端",
        description="Spring Boot + LangChain4j 后端的 Python 重写版（自研 Harness，不依赖 Agent 框架）",
        version="1.0.0",
        docs_url=None if auth_enabled else "/docs",
        redoc_url=None if auth_enabled else "/redoc",
        openapi_url=None if auth_enabled else "/openapi.json",
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        container = await get_application()
        app.state.container = container
        logger.info("应用已就绪，模型=%s", container.chat_model.model)
        try:
            yield
        finally:
            # SIGTERM：停止收新请求 → 等在途请求完成 → 释放连接
            container.shutdown.begin()
            drained = await container.shutdown.wait_until_drained()
            logger.info("优雅停机：在途请求排空=%s", drained)
            await container.aclose()

    app.router.lifespan_context = lifespan

    # ---- Phase 8-C：HTTP 边界（安全响应头 / 请求体上限 / 可选 CORS）----
    # 都是纯入口层能力，不碰业务与协议：前端仍然收裸文本分块。
    #
    # 注意 Starlette 的 add_middleware 是**前插**：后加的在外层。
    # 所以这里按「内 → 外」的顺序注册：
    #   router → BodySizeLimit → SecurityHeaders → CORS
    # 两层顺序不能反：SecurityHeaders 必须在 BodySizeLimit 外层，
    # 否则 413 响应拿不到 no-store / nosniff（错误响应同样可能被缓存）。
    settings = get_settings()
    app.add_middleware(
        BodySizeLimitMiddleware, max_bytes=settings.max_request_bytes
    )
    app.add_middleware(
        SecurityHeadersMiddleware,
        headers=DEFAULT_SECURITY_HEADERS,
        enabled=settings.security_headers_enabled,
    )
    origins = normalize_origins(settings.cors_allowed_origins)
    if origins:
        # 前端走 Vite 代理（同源）时不需要 CORS，所以默认不挂；
        # 只有显式配置白名单才启用，且 `*` 与 allow_credentials 互斥。
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=origins != ["*"],
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-Requested-With"],
            max_age=600,
        )
        logger.info("CORS 已启用，白名单=%s", origins)

    from xiaozhi.server import routes

    app.include_router(routes.router)
    return app
