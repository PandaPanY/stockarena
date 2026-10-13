"""股票详情接口：搜索、详情（基本信息、估值、财务、与平台的关联）、K 线、实时行情、公告新闻、因子排名。"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import current_user_id
from app.db.session import get_sessionmaker
from app.stocks import service

router = APIRouter(prefix="/api/stocks", tags=["stocks"], dependencies=[Depends(current_user_id)])


async def _run(fn, *args, **kw):
    """在线程中用独立的数据库会话执行（数据源请求可能较慢，不阻塞事件循环）。"""
    def work():
        with get_sessionmaker()() as s:
            return fn(s, *args, **kw)

    try:
        return await asyncio.to_thread(work)
    except service.StockError as e:
        raise HTTPException(404, str(e)) from None


@router.get("/search")
async def search(q: str, limit: int = 20) -> list[dict]:
    return await _run(service.search, q, min(limit, 50))


@router.get("/{symbol}")
async def detail(symbol: str, uid: int = Depends(current_user_id)) -> dict:
    return await _run(lambda s: service.detail(s, symbol, uid))


@router.get("/{symbol}/bars")
async def bars(symbol: str, freq: str = "d", adjust: str = "qfq") -> list[list]:
    if freq not in ("d", "w", "m") or adjust not in ("qfq", "hfq", "none"):
        raise HTTPException(400, "freq 只能是 d / w / m，adjust 只能是 qfq / hfq / none")
    return await _run(lambda s: service.bars(s, service.normalize_symbol(symbol), freq, adjust))


@router.get("/{symbol}/quote")
async def quote(symbol: str) -> dict:
    try:
        sym = service.normalize_symbol(symbol)
    except service.StockError as e:
        raise HTTPException(404, str(e)) from None
    return (await asyncio.to_thread(service.quotes, [sym])).get(sym) or {}


@router.get("/{symbol}/news")
async def news(symbol: str) -> dict:
    try:
        return await asyncio.to_thread(service.news, symbol)
    except service.StockError as e:
        raise HTTPException(404, str(e)) from None


@router.get("/{symbol}/ranks")
async def ranks(symbol: str) -> dict:
    return await _run(lambda s: service.factor_ranks(s, service.normalize_symbol(symbol)) or {})
