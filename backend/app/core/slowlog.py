"""性能诊断：记录慢请求与事件循环卡顿，排查「接口超时」时有数据可查。

- 慢请求：/api/ 下耗时超过 SLOW_REQUEST_S 秒的请求写一条警告（方法、路径、状态码、耗时）；
- 事件循环卡顿：每秒检查一次事件循环能否按时调度，延迟超过 LOOP_LAG_S 秒写一条警告。服务只有一个进程，
  事件循环被同步的耗时操作占住时，所有请求都会一起停住，这条日志能直接说明是不是这种情况。
"""

from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger(__name__)

SLOW_REQUEST_S = 5.0
LOOP_LAG_S = 1.0
LOOP_CHECK_S = 1.0


class SlowRequestLog:
    """纯 ASGI 中间件：记录耗时超过阈值的接口请求。"""

    def __init__(self, app, threshold: float = SLOW_REQUEST_S):
        self.app = app
        self.threshold = threshold

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope.get("path", "").startswith("/api/"):
            await self.app(scope, receive, send)
            return
        started = time.monotonic()
        status: dict[str, int] = {}

        async def _send(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, _send)
        finally:
            elapsed = time.monotonic() - started
            if elapsed >= self.threshold:
                logger.warning("慢请求 %.1f 秒：%s %s → %s", elapsed, scope.get("method"), scope.get("path"),
                               status.get("code", "未返回"))


async def watch_loop_lag(stop: asyncio.Event, threshold: float = LOOP_LAG_S, interval: float = LOOP_CHECK_S) -> None:
    """每 interval 秒醒来一次，实际间隔比预期多出 threshold 秒以上，说明事件循环被占住了。"""
    while not stop.is_set():
        expected = time.monotonic() + interval
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            return
        except TimeoutError:
            pass
        lag = time.monotonic() - expected
        if lag >= threshold:
            logger.warning("事件循环被阻塞约 %.1f 秒（期间所有请求都在等待）", lag)
