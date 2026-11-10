"""HTTP 路由 —— 接入层。

关键：**POST /xiaozhi/chat 返回裸文本分块**（text/stream;charset=utf-8），
和 Java 版完全一致，前端零改动可用。

运维侧：
* 响应头 ``X-Trace-Id`` 把本次对话与 ``/trace/{id}`` 关联（配合 scripts/compare.py）；
* ``/ready`` 用 HealthChecker 的探针注册表，不再把依赖判断硬编码在路由里；
* ``/metrics`` / ``/audit`` 暴露指标与审计；
* 按 memoryId + 来源 IP 限流，优雅停机期间拒绝新请求。
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse

from xiaozhi.core.event import EventType
from xiaozhi.reliability.tracing import new_trace_id, set_trace_id
from xiaozhi.security.guardrails import check_input
from xiaozhi.server.trace_view import render_trace_page

logger = logging.getLogger(__name__)
router = APIRouter()


def _container(request: Request):
    return request.app.state.container


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


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

    # 输入护栏：超长截断；注入仅留痕（是否拒绝由工具层 L3 按上下文决定）
    guard = check_input(message)
    message = guard.text
    for reason in guard.reasons:
        logger.warning("输入护栏 trace 待分配：%s", reason)

    # 优雅停机：不再接收新请求
    if container.shutdown.shutting_down:
        return PlainTextResponse(
            "服务正在重启，请稍后重试。",
            status_code=503,
            media_type="text/plain; charset=utf-8",
        )

    # 限流：同一会话 + 来源 IP
    if container.settings.enable_rate_limit:
        key = f"{memory_id}:{_client_ip(request)}"
        if not container.rate_limiter.allow(key):
            container.metrics.inc("rate_limited_total")
            return PlainTextResponse(
                "请求过于频繁，请稍后再试。",
                status_code=429,
                media_type="text/plain; charset=utf-8",
            )

    # 显式分配 trace_id，让响应头与 /trace/{id} 精确对应
    trace_id = new_trace_id()
    set_trace_id(trace_id)

    # format=events：结构化 SSE（每事件一行 data: {json}），
    # 供新前端/调试工具还原完整执行过程；默认裸文本，前端零改动
    events_mode = (request.query_params.get("format") or "text").lower() == "events"

    async def generate():
        async with container.shutdown.inflight_request():
            try:
                async for event in container.engine.run(
                    memory_id, message, user_id=user_id
                ):
                    if events_mode:
                        yield event.to_sse()
                        continue
                    if event.event_type == EventType.TEXT_RESPONSE:
                        delta = event.metadata.get("delta") or event.metadata.get("text") or ""
                        if delta:
                            yield delta
                    elif event.event_type == EventType.ERROR:
                        yield f"\n\n（出错了：{event.metadata.get('error')}）"
            except Exception as exc:  # 兜底：任何异常都要给用户一个可读的回复
                logger.exception("对话异常")
                if events_mode:
                    from xiaozhi.core.event import ErrorEvent

                    yield ErrorEvent(trace_id=trace_id, metadata={"error": f"{type(exc).__name__}"}).to_sse()
                else:
                    yield f"\n\n（服务暂时异常：{type(exc).__name__}）"

    return StreamingResponse(
        generate(),
        media_type=(
            "text/event-stream;charset=utf-8"
            if events_mode
            else "text/stream;charset=utf-8"
        ),
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Trace-Id": trace_id,
        },
    )


@router.get("/health")
async def health(request: Request):
    """存活探针"""
    container = _container(request)
    return {
        "status": "ok" if not container.shutdown.shutting_down else "shutting_down",
        "service": "xiaozhi-python",
        "shutdown": container.shutdown.snapshot(),
    }


@router.get("/ready")
async def ready(request: Request):
    """就绪探针 —— 探测各依赖（探针在组合根注册）"""
    container = _container(request)
    report = await container.health.check_all()
    return JSONResponse(report, status_code=200 if report["ready"] else 503)


@router.get("/metrics")
async def metrics(request: Request):
    """运行指标（精简视图 + 完整快照）"""
    container = _container(request)
    return {
        "summary": container.metrics.summary(),
        "snapshot": container.metrics.snapshot(),
        "circuit_breaker": (
            container.engine.circuit_breaker.snapshot()
            if container.engine.circuit_breaker
            else None
        ),
        "rate_limiter": container.rate_limiter.snapshot(),
    }


@router.get("/audit")
async def audit(request: Request, limit: int = 50):
    """最近的审计记录（已脱敏）"""
    container = _container(request)
    return {"entries": container.audit.read_recent(limit=limit)}


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
