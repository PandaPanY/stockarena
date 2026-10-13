"""消息推送：推送通道（微信等）与已发送记录。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.timeutils import utcnow
from app.db.types import UTCDateTime
from app.models.base import Base, TimestampMixin, UserOwnedMixin


class NotifyChannelRow(TimestampMixin, UserOwnedMixin, Base):
    """推送通道。secret_enc：Server 酱 SendKey / PushPlus token / 企业微信机器人地址，加密存储，接口只返回掩码。"""

    __tablename__ = "notify_channels"

    name: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16))                         # serverchan / pushplus / wecom_bot
    secret_enc: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    events: Mapped[list] = mapped_column(JSON, default=list)              # orders 调仓提醒 / errors 异常提醒
    last_status: Mapped[str] = mapped_column(String(16), default="")
    last_error: Mapped[str] = mapped_column(Text, default="")
    last_sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class NotificationRow(Base):
    """已发送的消息（按 去重键 + 通道 去重：同一天的调仓提醒只发一次）。"""

    __tablename__ = "notifications"
    __table_args__ = (UniqueConstraint("channel_id", "dedupe_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("notify_channels.id"), index=True)
    dedupe_key: Mapped[str] = mapped_column(String(128))
    event: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16))                       # ok / failed
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
