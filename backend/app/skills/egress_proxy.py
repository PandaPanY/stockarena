"""技能沙箱的出网过滤代理（独立脚本，在代理容器中用 python3 运行，不依赖平台代码）。

- 技能容器所在的网络没有外网出口，只能访问本代理；代理同时连着外网。
- 每个技能有一个令牌，经 HTTP(S)_PROXY 的用户名传入（http://<令牌>:x@<网关地址>:3128）；
  /proxy/allow.json 记录 {令牌: {"skill": 名称, "domains": [允许的域名]}}，文件变化后自动重新读取。
- 支持 HTTPS（CONNECT 隧道）与普通 HTTP；目标主机等于某个允许域名或是其子域名时放行，否则返回 403。
- 每次请求在 /logs/proxy.log 记一行 JSON：{ts, token, host, allowed}。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import sys
import time

ALLOW_FILE = os.environ.get("ALLOW_FILE", "/proxy/allow.json")
LOG_FILE = os.environ.get("LOG_FILE", "/logs/proxy.log")
PORT = int(os.environ.get("PORT", "3128"))
BIND = os.environ.get("BIND", "127.0.0.1")       # 只绑定技能内部网络的网关地址

_cache: dict = {"mtime": None, "data": {}}


def load_allow() -> dict:
    try:
        mtime = os.path.getmtime(ALLOW_FILE)
    except OSError:
        return {}
    if mtime != _cache["mtime"]:
        try:
            with open(ALLOW_FILE, encoding="utf-8") as f:
                _cache["data"] = json.load(f)
        except (OSError, ValueError):
            _cache["data"] = {}
        _cache["mtime"] = mtime
    return _cache["data"]


def host_allowed(host: str, domains: list[str]) -> bool:
    host = host.lower().rstrip(".")
    for d in domains:
        d = str(d).lower().strip().lstrip("*").lstrip(".")
        if d and (host == d or host.endswith("." + d)):
            return True
    return False


def token_of(headers: dict[str, str]) -> str:
    auth = headers.get("proxy-authorization", "")
    if auth.lower().startswith("basic "):
        try:
            return base64.b64decode(auth[6:].strip()).decode().split(":", 1)[0]
        except (ValueError, UnicodeDecodeError):
            return ""
    return ""


def log(token: str, host: str, allowed: bool) -> None:
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), "token": token, "host": host, "allowed": allowed}) + "\n")
    except OSError:
        pass


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        with contextlib.suppress(Exception):
            writer.close()


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=30)
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
        writer.close()
        return
    lines = head.decode("latin-1").split("\r\n")
    method, target, _ = (lines[0].split(" ", 2) + ["", ""])[:3]
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    token = token_of(headers)
    entry = load_allow().get(token) or {}
    if method.upper() == "CONNECT":
        host, _, port = target.rpartition(":")
        port = int(port or 443)
    else:
        rest = target.split("://", 1)[-1]
        hostport = rest.split("/", 1)[0]
        host, _, p = hostport.partition(":")
        port = int(p or 80)
    allowed = bool(entry) and host_allowed(host, entry.get("domains", []))
    log(token[:8], host, allowed)
    if not allowed:
        writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        await writer.drain()
        writer.close()
        return
    try:
        up_reader, up_writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=20)
    except (OSError, TimeoutError):
        writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        await writer.drain()
        writer.close()
        return
    if method.upper() == "CONNECT":
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
    else:
        # 普通 HTTP：去掉代理鉴权头，改成相对路径后转发
        path = "/" + target.split("://", 1)[-1].split("/", 1)[1] if "/" in target.split("://", 1)[-1] else "/"
        skip = ("proxy-authorization", "proxy-connection")
        out = [f"{method} {path} HTTP/1.1"] + [x for x in lines[1:] if x and not x.lower().startswith(skip)]
        up_writer.write(("\r\n".join(out) + "\r\n\r\n").encode("latin-1"))
        await up_writer.drain()
    await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer))


async def main() -> None:
    server = await asyncio.start_server(handle, BIND, PORT)
    print(f"skill egress proxy listening on {BIND}:{PORT}", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
