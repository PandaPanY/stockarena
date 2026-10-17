"""性能诊断：慢请求日志与事件循环卡顿检测。"""

from __future__ import annotations

import asyncio
import logging
import time

from app.core.slowlog import SlowRequestLog, watch_loop_lag


async def test_slow_request_logged(caplog):
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def send(_msg):
        pass

    mw = SlowRequestLog(app, threshold=0)
    with caplog.at_level(logging.WARNING):
        await mw({"type": "http", "path": "/api/quant/strategies", "method": "GET"}, None, send)
        await mw({"type": "http", "path": "/assets/x.js", "method": "GET"}, None, send)    # 静态文件不记
    msgs = [r.getMessage() for r in caplog.records]
    assert len(msgs) == 1 and "GET /api/quant/strategies → 200" in msgs[0]


async def test_loop_lag_detected(caplog):
    stop = asyncio.Event()
    with caplog.at_level(logging.WARNING):
        task = asyncio.create_task(watch_loop_lag(stop, threshold=0.2, interval=0.05))
        await asyncio.sleep(0.1)
        time.sleep(0.4)                      # 同步阻塞事件循环
        await asyncio.sleep(0.1)
        stop.set()
        await task
    assert any("事件循环被阻塞" in r.getMessage() for r in caplog.records)
