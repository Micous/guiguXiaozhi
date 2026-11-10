"""HTTP 路由 —— 接入层。

关键：**POST /xiaozhi/chat 返回裸文本分块**（text/stream;charset=utf-8），
和 Java 版完全一致，前端零改动可用。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse

from xiaozhi.core.event import EventType
from xiaozhi.core.message import Role
from xiaozhi.server.trace_view import render_trace_page

logger = logging.getLogger(__name__)
router = APIRouter()


def _container(request: Request):
    return request.app.state.container


@router.post("/xiaozhi/chat")
async def chat(request: Request):
    """对话接口 —— 返回裸文本流（与 Java 版一致，前端零改动）"""
    container = _container(request)
    payload: Dict[str, Any] = await request.json()
    memory_id = str(payload.get("memoryId") or payload.get("memory_id") or "default")
    message = payload.get("message") or ""
    user_id = str(payload.get("userId") or payload.get("user_id") or "anonymous")

    if not message.strip():
        return PlainTextResponse("请输入您的问题。", media_type="text/plain; charset=utf-8")

    async def generate():
        try:
            async for event in container.engine.run(memory_id, message, user_id=user_id):
                if event.event_type == EventType.TEXT_RESPONSE:
                    delta = event.metadata.get("delta") or event.metadata.get("text") or ""
                    if delta:
                        yield delta
                elif event.event_type == EventType.ERROR:
                    yield f"\n\n（出错了：{event.metadata.get('error')}）"
        except Exception as exc:  # 兜底：任何异常都要给用户一个可读的回复
            logger.exception("对话异常")
            yield f"\n\n（服务暂时异常：{type(exc).__name__}）"

    return StreamingResponse(
        generate(),
        media_type="text/stream;charset=utf-8",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/health")
async def health(request: Request):
    """存活探针"""
    return {"status": "ok", "service": "xiaozhi-python"}


@router.get("/ready")
async def ready(request: Request):
    """就绪探针 —— 探测各依赖"""
    container = _container(request)
    checks: Dict[str, Any] = {}

    # Milvus
    try:
        checks["milvus"] = {
            "ok": container.vector_store.count() >= 0,
            "rows": container.vector_store.count(),
        }
    except Exception as exc:
        checks["milvus"] = {"ok": False, "error": str(exc)}

    # 模型
    try:
        checks["model"] = {"ok": True, "name": container.chat_model.model}
    except Exception as exc:
        checks["model"] = {"ok": False, "error": str(exc)}

    # 数据库
    try:
        rows = container.repository.list_all(limit=1)
        checks["mysql"] = {"ok": True, "reachable": rows is not None}
    except Exception as exc:
        checks["mysql"] = {"ok": False, "error": str(exc)}

    ok = all(c.get("ok") for c in checks.values())
    return JSONResponse(
        {"ready": ok, "checks": checks}, status_code=200 if ok else 503
    )


@router.get("/traces")
async def list_traces(request: Request, limit: int = 50):
    """最近的追踪列表"""
    container = _container(request)
    return {"traces": container.trace_store.list_traces(limit=limit)}


@router.get("/trace/{trace_id}")
async def get_trace(trace_id: str, request: Request):
    """某次对话的完整追踪树"""
    container = _container(request)
    tree = container.trace_store.get_trace_tree(trace_id)
    if tree is None:
        return JSONResponse({"detail": "追踪不存在"}, status_code=404)
    return tree


@router.get("/trace")
async def trace_page(request: Request):
    """追踪可视化页面（纯 HTML + 原生 JS，无前端框架）"""
    return HTMLResponse(render_trace_page())


@router.get("/tools")
async def list_tools(request: Request):
    """当前注册的工具（便于排查模型为什么调了某个工具）"""
    container = _container(request)
    return {
        "tools": [
            {
                "name": d.name,
                "description": d.description,
                "parameters": d.input_schema,
                "risk_level": d.risk_level.value,
                "permissions_required": d.permissions_required,
            }
            for d in container.tool_registry.list_definitions()
        ]
    }
