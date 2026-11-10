"""FastAPI 应用 —— 接入层。

要点：**响应必须是裸文本分块**（text/stream;charset=utf-8），
与 Java 版保持一致，前端（axios onDownloadProgress 累积 responseText）零改动可用。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, Dict

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from xiaozhi.application import get_application
from xiaozhi.core.message import Role

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    """创建应用（组合根在 lifespan 里装配）"""
    app = FastAPI(
        title="硅谷小智 · Python 后端",
        description="Spring Boot + LangChain4j 后端的 Python 重写版（自研 Harness，不依赖 Agent 框架）",
        version="1.0.0",
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

    from xiaozhi.server import routes

    app.include_router(routes.router)
    return app
