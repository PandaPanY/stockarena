"""访问保护中间件：没有有效登录凭证的接口请求返回 401（WebSocket 直接关闭）。

页面静态文件不拦截（未登录时前端自己跳到登录页）；只保护 /api、/ws 与接口文档。
"""

from __future__ import annotations

import json
from http.cookies import CookieError, SimpleCookie

from starlette.types import ASGIApp, Receive, Scope, Send

from app.auth import core
from app.core.config import get_settings

# 不需要登录的接口：登录状态、首次设置密码、注册、登录、退出；MCP 服务自己校验 API 令牌（app/mcp/server.py）
PUBLIC_PATHS = frozenset({"/api/auth/status", "/api/auth/setup", "/api/auth/register", "/api/auth/login",
                          "/api/auth/logout", "/api/mcp"})
DOC_PATHS = frozenset({"/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"})


def is_protected(path: str) -> bool:
    if path in PUBLIC_PATHS:
        return False
    return path.startswith(("/api/", "/ws/")) or path in ("/api", "/ws") or path in DOC_PATHS


def cookie_from_scope(scope: Scope) -> str | None:
    for name, value in scope.get("headers", []):
        if name == b"cookie":
            try:
                jar = SimpleCookie(value.decode("latin-1"))
            except CookieError:
                return None
            morsel = jar.get(core.COOKIE_NAME)
            return morsel.value if morsel else None
    return None


class AuthMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (scope["type"] not in ("http", "websocket") or not get_settings().auth_enabled
                or scope.get("method") == "OPTIONS" or not is_protected(scope["path"])):
            await self.app(scope, receive, send)
            return
        uid = core.token_valid(cookie_from_scope(scope))
        if uid is not None:
            # 当前用户：接口通过 request.state.user_id 读取（app/api/deps.py current_user_id）
            scope.setdefault("state", {})["user_id"] = uid
            await self.app(scope, receive, send)
            return

        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 4401})
            return
        configured = core.admin_configured()
        body = json.dumps({"detail": "请先登录" if configured else "请先设置登录密码",
                           "code": "login_required" if configured else "setup_required"},
                          ensure_ascii=False).encode("utf-8")
        await send({"type": "http.response.start", "status": 401,
                    "headers": [(b"content-type", b"application/json; charset=utf-8"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})
