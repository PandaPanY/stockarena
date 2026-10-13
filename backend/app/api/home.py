"""首页接口：各市场指数与 A股涨跌概况、明星股、我的组合今天 vs 基准。"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.deps import current_user_id
from app.db.session import get_sessionmaker
from app.stocks import home
from app.stocks.service import StockError

router = APIRouter(prefix="/api/home", tags=["home"])


@router.get("")
async def overview(uid: int = Depends(current_user_id)) -> dict:
    def work():
        with get_sessionmaker()() as s:
            return home.home(s, uid)

    return await asyncio.to_thread(work)


class StarsIn(BaseModel):
    symbols: list[str] = Field(max_length=home.MAX_STARS)


@router.put("/stars")
def set_stars(body: StarsIn, uid: int = Depends(current_user_id)) -> dict:
    with get_sessionmaker()() as s:
        try:
            symbols = home.set_stars(s, body.symbols, uid)
        except StockError as e:
            raise HTTPException(400, str(e)) from None
        s.commit()
        return {"symbols": symbols}
