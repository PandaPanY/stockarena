"""自选股接口：增删改（权重、备注、是否监控）、事件列表、立即检查、监控设置。每个用户只能看到自己的自选股。"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.api.deps import current_user_id
from app.db.session import get_sessionmaker
from app.models.watch import WatchEventRow, WatchItemRow
from app.stocks import service as sv
from app.stocks.service import StockError
from app.watch import service as watch

router = APIRouter(prefix="/api/watch", tags=["watch"])


def _err(e: Exception) -> HTTPException:
    return HTTPException(400, str(e))


def _own(s: Session, item_id: int, uid: int) -> WatchItemRow:
    row = s.get(WatchItemRow, item_id)
    if row is None or row.user_id != uid:
        raise HTTPException(404, "自选股不存在")
    return row


def _own_item_ids(s: Session, uid: int) -> list[int]:
    return list(s.scalars(select(WatchItemRow.id).where(WatchItemRow.user_id == uid)))


@router.get("")
async def overview(uid: int = Depends(current_user_id)) -> dict:
    """自选股列表（实时价、最近事件、平台里的关联）、监控设置与检查状态。"""
    def work():
        with get_sessionmaker()() as s:
            rows = watch.items(s, uid)
            quotes = sv.quotes([r.symbol for r in rows]) if rows else {}
            out = []
            for r in rows:
                d = watch.to_dict(r)
                q = quotes.get(r.symbol) or {}
                d["quote"] = {"price": q.get("price"), "change_pct": q.get("change_pct"), "time": q.get("time")}
                last = s.scalars(select(WatchEventRow).where(WatchEventRow.item_id == r.id)
                                 .order_by(WatchEventRow.id.desc()).limit(1)).first()
                d["last_event"] = watch.event_dict(last, r.name) if last else None
                d["unread"] = len(s.scalars(select(WatchEventRow.id).where(
                    WatchEventRow.item_id == r.id, WatchEventRow.read.is_(False))).all())
                rel = sv.relations(s, r.symbol, uid)
                d["picks"] = sorted({m for p in rel["picks"] for m in p["members"]})
                d["held"] = [h["label"] for h in rel["holdings"]]
                out.append(d)
            return {"items": out, "settings": watch.settings(s, uid), "task": watch.status(uid),
                    "limits": {"max_items": watch.MAX_ITEMS, "weight_min": watch.WEIGHT_MIN,
                               "weight_max": watch.WEIGHT_MAX}}

    return await asyncio.to_thread(work)


class ItemIn(BaseModel):
    symbol: str
    weight: float = 1.0
    note: str = ""


@router.post("")
async def add(body: ItemIn, uid: int = Depends(current_user_id)) -> dict:
    def work():
        with get_sessionmaker()() as s:
            try:
                row = watch.add(s, body.symbol, body.weight, body.note, uid)
            except (watch.WatchError, StockError) as e:
                raise _err(e) from None
            s.commit()
            return watch.to_dict(row)

    return await asyncio.to_thread(work)


class ItemPatch(BaseModel):
    weight: float | None = None
    note: str | None = None
    monitor: bool | None = None


@router.patch("/{item_id}")
def patch(item_id: int, body: ItemPatch, uid: int = Depends(current_user_id)) -> dict:
    with get_sessionmaker()() as s:
        row = _own(s, item_id, uid)
        try:
            watch.update(s, row, weight=body.weight, note=body.note, monitor=body.monitor)
        except watch.WatchError as e:
            raise _err(e) from None
        s.commit()
        return watch.to_dict(row)


@router.delete("/{item_id}")
def remove(item_id: int, uid: int = Depends(current_user_id)) -> dict:
    with get_sessionmaker()() as s:
        row = _own(s, item_id, uid)
        s.query(WatchEventRow).filter(WatchEventRow.item_id == item_id).delete()
        s.delete(row)
        s.commit()
        return {"ok": True}


@router.get("/by-symbol/{symbol}")
def by_symbol(symbol: str, uid: int = Depends(current_user_id)) -> dict | None:
    """股票详情页用：这只股票是否在自己的自选股中（及权重、最近的事件）。"""
    try:
        sym = sv.normalize_symbol(symbol)
    except StockError:
        return None
    with get_sessionmaker()() as s:
        row = s.scalar(select(WatchItemRow).where(WatchItemRow.symbol == sym, WatchItemRow.user_id == uid))
        if row is None:
            return None
        evs = s.scalars(select(WatchEventRow).where(WatchEventRow.item_id == row.id)
                        .order_by(WatchEventRow.id.desc()).limit(10))
        return {**watch.to_dict(row), "events": [watch.event_dict(e, row.name) for e in evs]}


@router.get("/events")
def events(item_id: int | None = None, symbol: str | None = None, limit: int = 100,
           uid: int = Depends(current_user_id)) -> list[dict]:
    with get_sessionmaker()() as s:
        names = {r.id: r.name for r in watch.items(s, uid)}
        q = (select(WatchEventRow).where(WatchEventRow.item_id.in_(list(names)))
             .order_by(WatchEventRow.id.desc()).limit(min(limit, 500)))
        if item_id is not None:
            q = q.where(WatchEventRow.item_id == item_id)
        if symbol:
            q = q.where(WatchEventRow.symbol == symbol)
        return [watch.event_dict(e, names.get(e.item_id, "")) for e in s.scalars(q)]


class ReadIn(BaseModel):
    ids: list[int] | None = None


@router.post("/events/read")
def mark_read(body: ReadIn, uid: int = Depends(current_user_id)) -> dict:
    with get_sessionmaker()() as s:
        q = update(WatchEventRow).where(WatchEventRow.item_id.in_(_own_item_ids(s, uid))).values(read=True)
        if body.ids:
            q = q.where(WatchEventRow.id.in_(body.ids))
        s.execute(q)
        s.commit()
        return {"ok": True}


class CheckIn(BaseModel):
    item_ids: list[int] | None = Field(default=None, max_length=watch.MAX_ITEMS)


@router.post("/check")
async def check(body: CheckIn, uid: int = Depends(current_user_id)) -> dict:
    """在后台立即检查自己的自选股（全部或指定的）。"""
    try:
        with get_sessionmaker()() as s:
            watch._llm(s, uid)            # 先确认有可用的模型
        return watch.run_in_background(body.item_ids, uid=uid)
    except watch.WatchError as e:
        raise _err(e) from None


@router.get("/check")
def check_status(uid: int = Depends(current_user_id)) -> dict:
    return watch.status(uid)


class SettingsIn(BaseModel):
    enabled: bool | None = None
    provider_id: int | None = None
    model: str | None = None
    push_level: int | None = None


@router.put("/settings")
def put_settings(body: SettingsIn, uid: int = Depends(current_user_id)) -> dict:
    with get_sessionmaker()() as s:
        try:
            cfg = watch.set_settings(s, uid, **body.model_dump())
        except watch.WatchError as e:
            raise _err(e) from None
        s.commit()
        return cfg
