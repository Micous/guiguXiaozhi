"""HTTP 边界加固 —— 安全响应头 / CORS 白名单 / 请求体上限（Phase 8-C）。

改造前 `create_app()` 里**一个中间件都没有**（评估 P0-1 的原话：
"未挂任何认证中间件，也没有 CORS/安全响应头配置"）。认证在 Phase 1 补齐，
但响应头与请求体边界一直空着 —— 这类问题不会让功能坏掉，只会让浏览器
愿意把我们的响应当别的东西解释。

三个中间件各自独立，都可以关：

* :class:`SecurityHeadersMiddleware` —— 默认开。纯响应头，前端零感知；
  医疗对话的响应**不允许多级缓存**（``no-store``），否则身份证号与病情
  可能被中间代理落盘。
* :class:`BodySizeLimitMiddleware` —— 默认 256KB。JSON body 是
  ``await request.json()`` 一次读进内存的，没有上限就是一个廉价的内存放大器。
* CORS —— **默认不启用**（前端走 Vite 代理，同源，不需要）。
  配置 ``CORS_ALLOWED_ORIGINS`` 才挂载，且不允许 ``*`` 与凭证同时出现。

边界说明（写在这里免得被当成"已解决"）：BodySizeLimit 用 ``Content-Length``
判定；**分块传输（chunked）没有长度头，本中间件不拦截** —— 公网部署必须
在网关/Ingress 上同时配置 ``max_client_body_size``。应用层还有
``guardrails.MAX_INPUT_CHARS`` 负责截断超长问句。
"""

from __future__ import annotations

import logging
from typing import Iterable, List, Sequence, Tuple

from starlette.responses import PlainTextResponse

logger = logging.getLogger(__name__)

#: 默认安全响应头。
#: 不加 CSP —— 本服务同时提供 ``/docs`` 与 ``/trace/{id}`` 的 HTML 页面，
#: 一刀切的 CSP 会把它们弄坏；HTML 页面的 CSP 属于接入层（网关）职责。
DEFAULT_SECURITY_HEADERS: Tuple[Tuple[str, str], ...] = (
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
    ("X-Permitted-Cross-Domain-Policies", "none"),
    # 医疗对话内容不得进任何缓存（浏览器/代理）
    ("Cache-Control", "no-store"),
)

BODY_TOO_LARGE_TEXT = "请求体过大。"


class SecurityHeadersMiddleware:
    """给每个 HTTP 响应追加安全响应头（已存在的不覆盖）。"""

    def __init__(
        self,
        app,
        headers: Sequence[Tuple[str, str]] = DEFAULT_SECURITY_HEADERS,
        enabled: bool = True,
    ) -> None:
        self.app = app
        self.headers = tuple(headers)
        self.enabled = enabled

    async def __call__(self, scope, receive, send):
        if not self.enabled or scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                raw = message.setdefault("headers", [])
                existing = {k.lower() for k, _ in raw}
                for name, value in self.headers:
                    if name.lower().encode("latin-1") not in existing:
                        raw.append(
                            (name.lower().encode("latin-1"), value.encode("latin-1"))
                        )
                        existing.add(name.lower().encode("latin-1"))
            await send(message)

        await self.app(scope, receive, send_with_headers)


class BodySizeLimitMiddleware:
    """请求体上限（按 ``Content-Length`` 判定，超限直接 413）。

    在**进入路由之前**拒绝：``await request.json()`` 会把整个 body 读进内存，
    等到路由里再判断已经晚了。

    ``max_bytes<=0`` 表示不限制（默认不这么用）。
    """

    def __init__(self, app, max_bytes: int = 262_144) -> None:
        self.app = app
        self.max_bytes = int(max_bytes)

    async def __call__(self, scope, receive, send):
        if self.max_bytes <= 0 or scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = _content_length(scope)
        if declared is not None and declared > self.max_bytes:
            logger.warning(
                "拒绝超大请求体：path=%s content_length=%s limit=%s",
                scope.get("path"),
                declared,
                self.max_bytes,
            )
            response = PlainTextResponse(
                BODY_TOO_LARGE_TEXT,
                status_code=413,
                media_type="text/plain; charset=utf-8",
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def _content_length(scope) -> int | None:
    for key, value in scope.get("headers") or ():
        if key == b"content-length":
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
    return None


def normalize_origins(origins: Iterable[str]) -> List[str]:
    """清洗 CORS 白名单：去空白、去尾斜杠、去重保序。

    ``*`` 与 ``allow_credentials`` 不能同时出现（浏览器会直接拒绝），
    所以这里把 ``*`` 原样保留、由装配层关掉 credentials。
    """
    cleaned: List[str] = []
    for item in origins or ():
        text = str(item).strip().rstrip("/")
        if text and text not in cleaned:
            cleaned.append(text)
    return cleaned
