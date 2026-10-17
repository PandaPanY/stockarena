"""MCP 服务（Model Context Protocol，Streamable HTTP 传输的无状态子集）：POST /api/mcp。

- 认证：请求头 Authorization: Bearer <个人 API 令牌>（在「配置中心 → MCP 接入」创建）。关闭访问保护时
  （auth_enabled=false，本机调试）可以不带令牌，此时为管理员只读。
- 每个请求独立（不建立会话、不推送通知），响应直接是 JSON，不用 SSE。
- 只读令牌只列出、只能调用只读工具；写操作要令牌勾选「允许修改」。
"""

from __future__ import annotations

import json
import logging
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from app.auth import tokens
from app.core.config import get_settings
from app.mcp.tools import BY_NAME, TOOLS, Context, ToolError
from app.models.base import DEFAULT_USER_ID

logger = logging.getLogger(__name__)

router = APIRouter(tags=["mcp"])

PATH = "/api/mcp"
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
MAX_TEXT = 200_000           # 单个工具结果的最大字符数

READ_ONLY_TEXT = "当前令牌只读：不能新建策略、提交版本或启动 AI 优化（创建令牌时勾选「允许修改」才可以）。"
INSTRUCTIONS = """StockArena 是规则化 A 股策略的研究平台：AI 在历史行情上反复回测、修改策略，
新版本要通过开发期的显著性检验才会替换旧版本。
使用约定：
1. 只提供开发期（训练 + 验证）结果。留出期从不参与决定，也不通过 MCP 提供，避免按留出期挑选策略。
2. 写或改策略规格（spec）前先调用 get_spec_reference；可以参考已有策略冠军版本的 spec。
3. preview_backtest 的每次试算都会被记录，下次 create_strategy 时计入测试次数（试得越多，升级门槛越严）。
4. 修改已有策略用 submit_version：每个版本都会保存并做升级检验。
5. 结果只用于研究和模拟交易，不构成投资建议。"""


def _rpc_error(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _result(mid, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _bearer(request: Request) -> str | None:
    h = request.headers.get("authorization") or ""
    return h[7:].strip() if h[:7].lower() == "bearer " else None


def _context(request: Request) -> Context | None:
    info = tokens.verify(_bearer(request))
    if info is not None:
        return Context(request.app, info.user_id, info.token_id, info.can_write)
    if not get_settings().auth_enabled:
        return Context(request.app, DEFAULT_USER_ID, None, False)
    return None


def _origin_ok(request: Request) -> bool:
    """防 DNS 重绑定：带 Origin 头时必须与访问的主机一致（MCP 客户端一般不带 Origin）。"""
    origin = request.headers.get("origin")
    if not origin:
        return True
    host = request.headers.get("host", "")
    return urlsplit(origin).netloc == host or origin in get_settings().cors_origins


def _tools_for(ctx: Context) -> list:
    return [t for t in TOOLS if ctx.can_write or not t.write]


async def _call_tool(ctx: Context, params: dict) -> dict:
    name, args = params.get("name"), params.get("arguments") or {}
    tool = BY_NAME.get(name) if isinstance(name, str) else None
    if tool is None:
        raise ValueError(f"没有这个工具：{name}")
    if not isinstance(args, dict):
        raise ValueError("arguments 必须是对象")
    if tool.write and not ctx.can_write:
        return {"content": [{"type": "text", "text": READ_ONLY_TEXT}], "isError": True}
    try:
        out = await tool.handler(ctx, args)
    except ToolError as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    except Exception:
        logger.exception("MCP 工具 %s 出错", name)
        return {"content": [{"type": "text", "text": "服务器内部错误，详情见服务日志"}], "isError": True}
    text = json.dumps(out, ensure_ascii=False, default=str)
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT] + "…（结果过长已截断）"
    return {"content": [{"type": "text", "text": text}], "isError": False}


async def handle(ctx: Context, msg) -> dict | None:
    """处理一条 JSON-RPC 消息；通知（没有 id）返回 None。"""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
        return _rpc_error(msg.get("id") if isinstance(msg, dict) else None, -32600, "Invalid Request")
    mid, method, params = msg.get("id"), msg["method"], msg.get("params") or {}
    if "id" not in msg:
        return None
    if not isinstance(params, dict):
        return _rpc_error(mid, -32602, "params 必须是对象")
    if method == "initialize":
        asked = params.get("protocolVersion")
        return _result(mid, {
            "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "stockarena", "title": "StockArena", "version": get_settings().version},
            "instructions": INSTRUCTIONS + ("" if ctx.can_write else "\n" + READ_ONLY_TEXT),
        })
    if method == "ping":
        return _result(mid, {})
    if method == "tools/list":
        return _result(mid, {"tools": [t.describe() for t in _tools_for(ctx)]})
    if method == "tools/call":
        try:
            return _result(mid, await _call_tool(ctx, params))
        except ValueError as e:
            return _rpc_error(mid, -32602, str(e))
    if method in ("resources/list", "prompts/list"):
        return _result(mid, {method.split("/")[0]: []})
    return _rpc_error(mid, -32601, f"Method not found: {method}")


@router.post(PATH, include_in_schema=False)
async def mcp_post(request: Request) -> Response:
    if not get_settings().mcp_enabled:
        return JSONResponse({"detail": "MCP 服务未开启"}, status_code=404)
    if not _origin_ok(request):
        return JSONResponse({"detail": "Origin 不允许"}, status_code=403)
    ctx = _context(request)
    if ctx is None:
        return JSONResponse({"detail": "需要有效的 API 令牌（Authorization: Bearer …）"}, status_code=401,
                            headers={"WWW-Authenticate": 'Bearer realm="stockarena-mcp"'})
    try:
        body = json.loads(await request.body())
    except (ValueError, UnicodeDecodeError):
        return JSONResponse(_rpc_error(None, -32700, "Parse error"), status_code=400)
    if isinstance(body, list):
        out = [r for m in body if (r := await handle(ctx, m)) is not None]
        return JSONResponse(out) if out else Response(status_code=202)
    r = await handle(ctx, body)
    return JSONResponse(r) if r is not None else Response(status_code=202)


@router.get(PATH, include_in_schema=False)
@router.delete(PATH, include_in_schema=False)
async def mcp_other() -> Response:
    """不提供服务端推送流，也没有会话可以删除。"""
    return Response(status_code=405, headers={"Allow": "POST"})
