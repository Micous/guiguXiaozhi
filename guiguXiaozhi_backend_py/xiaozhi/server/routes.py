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

import json
import logging
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse

from xiaozhi.core.event import EventType
from xiaozhi.reliability.tracing import new_trace_id, set_trace_id
from xiaozhi.security.approval_service import (
    ApprovalContext,
    ApprovalError,
    reset_approval_context,
    set_approval_context,
)
from xiaozhi.security.auth import AuthError, BootstrapRejected, bearer_token
from xiaozhi.security.guardrails import check_input
from xiaozhi.security.principal import (
    ANONYMOUS,
    CHAT_WRITE_SCOPE,
    OPS_READ_SCOPE,
    PROFILE_FORGET_SCOPE,
    SESSION_CREATE_SCOPE,
    Principal,
)
from xiaozhi.security.session_access import SessionAccessError, SessionForbiddenError
from xiaozhi.server.trace_view import render_trace_page

logger = logging.getLogger(__name__)
router = APIRouter()

# Phase 2：裸文本流里夹带的结构化审批标记。审批关闭时永不出现，
# 旧前端/Java 版协议零感知；新前端在流结束后从累积文本里剥离并弹确认框。
APPROVAL_SENTINEL_PREFIX = "[[XIAOZHI_APPROVAL]]"
APPROVAL_SENTINEL_SUFFIX = "[[/XIAOZHI_APPROVAL]]"


def _container(request: Request):
    return request.app.state.container


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _require_principal(request: Request) -> Principal:
    """认证关闭 → ANONYMOUS（兼容路径）；开启 → 校验 Bearer token。

    不改变任何端点签名（既有调用方零感知），认证开启时缺/坏 token 直接 401。
    """
    container = _container(request)
    if not container.settings.auth_enabled:
        return ANONYMOUS
    if container.token_service is None:
        raise HTTPException(status_code=503, detail="认证组件未就绪")
    try:
        return container.token_service.verify(
            bearer_token(request.headers.get("authorization"))
        )
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


def _require_ops(request: Request) -> None:
    """运维端点：认证开启时需要 ops:read scope。"""
    container = _container(request)
    if not container.settings.auth_enabled:
        return
    principal = _require_principal(request)
    if not principal.has_scope(OPS_READ_SCOPE):
        raise HTTPException(status_code=403, detail="需要 ops:read 权限")


@router.post("/xiaozhi/chat")
async def chat(request: Request):
    """对话接口 —— 返回裸文本流（与 Java 版一致，前端零改动）"""
    container = _container(request)
    principal = _require_principal(request)
    payload: Dict[str, Any] = await request.json()
    memory_id = str(payload.get("memoryId") or payload.get("memory_id") or "default")
    message = payload.get("message") or ""
    user_id = str(payload.get("userId") or payload.get("user_id") or "anonymous")
    # 一次性审批 token 只从请求体读入，经 contextvar 下发；模型输出永远碰不到它
    approval_token = str(
        payload.get("approvalToken") or payload.get("approval_token") or ""
    )

    if container.settings.auth_enabled:
        # 身份不再取自请求体：一律以 token 主体为准（P0-01）。
        user_id = principal.subject_id
        if not principal.has_scope(CHAT_WRITE_SCOPE):
            return PlainTextResponse(
                "无权发起对话。", status_code=403, media_type="text/plain; charset=utf-8"
            )
        try:
            container.session_access.assert_owner(memory_id, principal)
        except SessionAccessError:
            # 「不存在」与「不属于你」统一 403，不泄露 session_id 是否真实存在。
            return PlainTextResponse(
                "会话不存在或不属于当前用户。",
                status_code=403,
                media_type="text/plain; charset=utf-8",
            )

    if not message.strip():
        return PlainTextResponse("请输入您的问题。", media_type="text/plain; charset=utf-8")

    # Phase 2：携带一次性审批 token 的请求先做**非消费式**预检。
    # 失效 token 若放行进模型，历史里的成功记录会让模型复述「已预约成功」，
    # 误导用户以为重放也执行了；真正消费仍由执行器在写工具执行前原子完成。
    if (
        container.settings.approval_enabled
        and approval_token
        and container.approval_service is not None
    ):
        try:
            container.approval_service.inspect_token(
                approval_token, principal, session_id=memory_id
            )
        except ApprovalError as exc:
            return PlainTextResponse(
                f"本次确认已失效（{exc.reason_code}），操作未执行，请重新发起。",
                status_code=409,
                media_type="text/plain; charset=utf-8",
            )

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
        # 请求级审批上下文：engine.run() 签名不变，靠 contextvar 把
        # principal / session / 一次性 token 送到 SecureToolExecutor。
        set_approval_context(
            ApprovalContext(
                principal=principal,
                approval_token=approval_token,
                session_id=memory_id,
                auth_enabled=container.settings.auth_enabled,
            )
        )
        try:
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
                        elif event.event_type == EventType.APPROVAL_REQUIRED:
                            # 裸文本流里的结构化标记：前端在流结束后剥离并弹确认框
                            yield (
                                "\n\n"
                                + APPROVAL_SENTINEL_PREFIX
                                + json.dumps(event.metadata, ensure_ascii=False, default=str)
                                + APPROVAL_SENTINEL_SUFFIX
                            )
                        elif event.event_type == EventType.ERROR:
                            yield f"\n\n（出错了：{event.metadata.get('error')}）"
                except Exception as exc:  # 兜底：任何异常都要给用户一个可读的回复
                    logger.exception("对话异常")
                    if events_mode:
                        from xiaozhi.core.event import ErrorEvent

                        yield ErrorEvent(trace_id=trace_id, metadata={"error": f"{type(exc).__name__}"}).to_sse()
                    else:
                        yield f"\n\n（服务暂时异常：{type(exc).__name__}）"
        finally:
            reset_approval_context()

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


@router.post("/v1/auth/token")
@router.post("/api/v1/auth/token")  # 直连后端（不经 Vite 代理）的别名
async def issue_token(request: Request):
    """本地过渡：用 bootstrap 凭证换 JWT（正式环境换 OIDC/网关，只换签发方）。

    scopes 由服务端按 subject_id 计算，客户端传什么都不会提权。
    """
    container = _container(request)
    if not container.settings.auth_enabled:
        return JSONResponse({"detail": "认证未启用"}, status_code=404)
    try:
        body = await request.json()
    except Exception:
        body = {}
    subject_id = str(body.get("subject_id") or "")
    try:
        container.token_service.verify_bootstrap(str(body.get("secret") or ""))
        token = container.token_service.issue(subject_id)
    except BootstrapRejected:
        return JSONResponse({"detail": "凭证不正确"}, status_code=401)
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    return {
        "access_token": token,
        "token_type": "Bearer",
        "expires_in": container.settings.auth_token_ttl_seconds,
        "subject_id": subject_id.strip(),
    }


@router.post("/v1/sessions")
@router.post("/api/v1/sessions")  # 直连后端的别名
async def create_session(request: Request):
    """服务端签发不可枚举的 session_id，作为 /xiaozhi/chat 的 memoryId。"""
    container = _container(request)
    if not container.settings.auth_enabled:
        return JSONResponse({"detail": "认证未启用"}, status_code=404)
    principal = _require_principal(request)
    if not principal.has_scope(SESSION_CREATE_SCOPE):
        return JSONResponse({"detail": "需要 session:create 权限"}, status_code=403)
    return {"session_id": container.session_access.create(principal)}


@router.delete("/v1/sessions/{session_id}")
@router.delete("/api/v1/sessions/{session_id}")  # 直连后端的别名
async def revoke_session(session_id: str, request: Request):
    """撤销本人会话（审计 P0-01 验收 7）：撤销后旧 id 不能再读写。

    先查归属再删，跨用户撤销一律 403；「已不存在」返回 revoked=false，
    不泄露别人的 session_id 是否曾存在。
    """
    container = _container(request)
    if not container.settings.auth_enabled:
        return JSONResponse({"detail": "认证未启用"}, status_code=404)
    principal = _require_principal(request)
    try:
        removed = container.session_access.revoke(session_id, principal)
    except SessionForbiddenError:
        return JSONResponse({"detail": "无权操作该会话"}, status_code=403)
    return {"revoked": removed, "session_id": session_id}


def _approval_service(request: Request):
    """审批组件未启用时返回 None（端点统一 404，不暴露存在性）。"""
    container = _container(request)
    if not container.settings.approval_enabled or container.approval_service is None:
        return None
    return container.approval_service


def _approval_error(exc: ApprovalError) -> JSONResponse:
    """状态机拒绝 → 结构化原因码；跨用户与不存在统一 403 防枚举。"""
    code = exc.reason_code
    status = 403 if code in ("forbidden", "not_found") else 409
    return JSONResponse(
        {"detail": str(exc), "reason_code": code}, status_code=status
    )


def _approval_principal(request: Request):
    """审批端点必须有真实主体；认证开启时还要求 chat:write scope。"""
    principal = _require_principal(request)
    if _container(request).settings.auth_enabled and not principal.has_scope(
        CHAT_WRITE_SCOPE
    ):
        raise HTTPException(status_code=403, detail="需要 chat:write 权限")
    return principal


@router.get("/v1/approvals")
@router.get("/api/v1/approvals")
async def list_approvals(request: Request):
    """当前主体的待确认/已确认请求（刷新页面后恢复确认弹窗用）。"""
    service = _approval_service(request)
    if service is None:
        return JSONResponse({"detail": "审批未启用"}, status_code=404)
    principal = _approval_principal(request)
    return {"items": service.list_for_subject(principal)}


@router.get("/v1/approvals/{approval_id}")
@router.get("/api/v1/approvals/{approval_id}")
async def get_approval(approval_id: str, request: Request):
    service = _approval_service(request)
    if service is None:
        return JSONResponse({"detail": "审批未启用"}, status_code=404)
    principal = _approval_principal(request)
    try:
        return service.detail(approval_id, principal)
    except ApprovalError as exc:
        return _approval_error(exc)


@router.post("/v1/approvals/{approval_id}/approve")
@router.post("/api/v1/approvals/{approval_id}/approve")
async def approve_approval(approval_id: str, request: Request):
    """用户**明确点击确认**：签发短时一次性 token（明文只在此返回一次）。"""
    service = _approval_service(request)
    if service is None:
        return JSONResponse({"detail": "审批未启用"}, status_code=404)
    container = _container(request)
    principal = _approval_principal(request)
    try:
        token = service.approve(approval_id, principal)
    except ApprovalError as exc:
        return _approval_error(exc)
    return {
        "approval_token": token,
        "approval_id": approval_id,
        "expires_in": container.settings.approval_ttl_seconds,
    }


@router.post("/v1/approvals/{approval_id}/revoke")
@router.post("/api/v1/approvals/{approval_id}/revoke")
async def revoke_approval(approval_id: str, request: Request):
    """用户拒绝/取消：撤销后即便持有明文 token 也无法消费。"""
    service = _approval_service(request)
    if service is None:
        return JSONResponse({"detail": "审批未启用"}, status_code=404)
    principal = _approval_principal(request)
    try:
        service.revoke(approval_id, principal)
    except ApprovalError as exc:
        return _approval_error(exc)
    return {"revoked": True, "approval_id": approval_id}


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
    _require_ops(request)
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


@router.delete("/profile/{user_key}")
async def forget_profile(user_key: str, request: Request):
    """"忘记我"——删除该用户全部画像事实（MySQL + 向量）。合规要求端点。"""
    container = _container(request)
    if container.settings.auth_enabled:
        principal = _require_principal(request)
        if not principal.has_scope(PROFILE_FORGET_SCOPE):
            return JSONResponse({"detail": "需要 profile:forget 权限"}, status_code=403)
    if container.profile_service is None:
        return JSONResponse({"detail": "画像功能未启用"}, status_code=404)
    removed = await container.profile_service.forget(user_key)
    return {"user_key": user_key, "removed": removed}


@router.get("/audit")
async def audit(request: Request, limit: int = 50):
    """最近的审计记录（已脱敏）"""
    _require_ops(request)
    container = _container(request)
    return {"entries": container.audit.read_recent(limit=limit)}


@router.get("/traces")
async def list_traces(request: Request, limit: int = 50):
    """最近的追踪列表"""
    _require_ops(request)
    container = _container(request)
    return {"traces": container.trace_store.list_traces(limit=limit)}


@router.get("/trace/{trace_id}")
async def get_trace(trace_id: str, request: Request):
    """某次对话的完整追踪树"""
    _require_ops(request)
    container = _container(request)
    tree = container.trace_store.get_trace_tree(trace_id)
    if tree is None:
        return JSONResponse({"detail": "追踪不存在"}, status_code=404)
    return tree


@router.get("/trace")
async def trace_page(request: Request):
    """追踪可视化页面（纯 HTML + 原生 JS，无前端框架）"""
    _require_ops(request)
    return HTMLResponse(render_trace_page())


@router.get("/tools")
async def list_tools(request: Request):
    """当前注册的工具（便于排查模型为什么调了某个工具）"""
    _require_ops(request)
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
