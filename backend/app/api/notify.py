"""消息推送接口：推送通道的增删改、测试发送、发送记录。密钥只写不读（接口只返回掩码）。"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_user_id
from app.arena.deps import secret_box
from app.core.crypto import mask_key
from app.db.session import get_db
from app.models.notify import NotificationRow, NotifyChannelRow
from app.notify import channels, service

router = APIRouter(prefix="/api/notify", tags=["notify"])


class ChannelIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    kind: str
    secret: str | None = Field(None, max_length=300, description="新建时必填；修改时留空表示不变")
    enabled: bool = True
    events: list[str] = Field(default_factory=lambda: ["orders", "errors", "health", "skills", "watch"])


def _dict(ch: NotifyChannelRow) -> dict:
    try:
        masked = mask_key(secret_box().decrypt(ch.secret_enc))
    except ValueError:
        masked = "（无法解密，请重新填写）"
    return {"id": ch.id, "name": ch.name, "kind": ch.kind, "kind_name": channels.KINDS.get(ch.kind, ch.kind),
            "secret_masked": masked, "enabled": ch.enabled, "events": ch.events or [],
            "last_status": ch.last_status, "last_error": ch.last_error,
            "last_sent_at": ch.last_sent_at.isoformat() if ch.last_sent_at else None}


def _get(db: Session, cid: int, user_id: int) -> NotifyChannelRow:
    ch = db.get(NotifyChannelRow, cid)
    if ch is None or ch.user_id != user_id:
        raise HTTPException(404, "推送通道不存在")
    return ch


def _check(body: ChannelIn) -> None:
    if body.kind not in channels.KINDS:
        raise HTTPException(400, f"推送方式只能是：{'、'.join(channels.KINDS.values())}")
    bad = [e for e in body.events if e not in service.EVENTS]
    if bad:
        raise HTTPException(400, f"未知事件 {', '.join(bad)}")


@router.get("/meta")
def meta() -> dict:
    return {"kinds": channels.KINDS, "events": service.EVENTS}


@router.get("/channels")
def list_channels(db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> list[dict]:
    rows = db.scalars(select(NotifyChannelRow).where(NotifyChannelRow.user_id == user_id)
                      .order_by(NotifyChannelRow.id))
    return [_dict(c) for c in rows]


@router.post("/channels")
def create_channel(body: ChannelIn, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> dict:
    _check(body)
    if not body.secret:
        raise HTTPException(400, "请填写密钥")
    try:
        secret = channels.validate(body.kind, body.secret)
    except channels.NotifyError as e:
        raise HTTPException(400, str(e)) from None
    ch = NotifyChannelRow(user_id=user_id, name=body.name, kind=body.kind, enabled=body.enabled, events=body.events,
                          secret_enc=secret_box().encrypt(secret))
    db.add(ch)
    db.commit()
    return _dict(ch)


@router.patch("/channels/{cid}")
def update_channel(cid: int, body: ChannelIn, db: Session = Depends(get_db),
                   user_id: int = Depends(current_user_id)) -> dict:
    ch = _get(db, cid, user_id)
    _check(body)
    if body.secret:
        try:
            ch.secret_enc = secret_box().encrypt(channels.validate(body.kind, body.secret))
        except channels.NotifyError as e:
            raise HTTPException(400, str(e)) from None
    elif body.kind != ch.kind:
        raise HTTPException(400, "更换推送方式时需要重新填写密钥")
    ch.name, ch.kind, ch.enabled, ch.events = body.name, body.kind, body.enabled, body.events
    db.commit()
    return _dict(ch)


@router.delete("/channels/{cid}")
def delete_channel(cid: int, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> dict:
    ch = _get(db, cid, user_id)
    for n in db.scalars(select(NotificationRow).where(NotificationRow.channel_id == ch.id)):
        db.delete(n)
    db.delete(ch)
    db.commit()
    return {"ok": True}


@router.post("/channels/{cid}/test")
async def test_channel(cid: int, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> dict:
    ch = _get(db, cid, user_id)
    kind, secret = ch.kind, secret_box().decrypt(ch.secret_enc)
    db.close()
    msg = service.test_message()
    try:
        await asyncio.to_thread(channels.send, kind, secret, msg.title, msg.body)
    except channels.NotifyError as e:
        raise HTTPException(400, str(e)) from None
    return {"ok": True}


@router.post("/check")
async def run_check(user_id: int = Depends(current_user_id)) -> dict:
    """立即检查一次调仓提醒（已经发过的不会重复发送）。"""
    return {"results": await asyncio.to_thread(service.daily_check)}


@router.get("/history")
def history(db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> list[dict]:
    ids = list(db.scalars(select(NotifyChannelRow.id).where(NotifyChannelRow.user_id == user_id)))
    rows = db.scalars(select(NotificationRow).where(NotificationRow.channel_id.in_(ids))
                      .order_by(NotificationRow.id.desc()).limit(50))
    return [{"id": n.id, "channel_id": n.channel_id, "event": n.event, "title": n.title, "body": n.body,
             "status": n.status, "error": n.error, "created_at": n.created_at.isoformat()} for n in rows]
