"""自选股监控：用户关注的股票（带权重），以及 AI 发现的重大事件。"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import JSON, Boolean, Date, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.types import UTCDateTime
from app.models.base import Base, TimestampMixin, UserOwnedMixin


class WatchItemRow(TimestampMixin, UserOwnedMixin, Base):
    """一只自选股。

    weight：自选权重，0 = 不买、1 = 默认、大于 1 = 策略选中时多配一些（只作用于「自选倾斜」账户，不影响其他账户）。
    state：监控状态，如已看过的公告 / 新闻（避免重复提醒）。
    """

    __tablename__ = "watch_items"
    __table_args__ = (UniqueConstraint("user_id", "symbol"),)

    symbol: Mapped[str] = mapped_column(String(20), index=True)
    name: Mapped[str] = mapped_column(String(64), default="")
    market: Mapped[str] = mapped_column(String(4), default="CN")
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    note: Mapped[str] = mapped_column(Text, default="")
    monitor: Mapped[bool] = mapped_column(Boolean, default=True)
    state: Mapped[dict] = mapped_column(JSON, default=dict)
    last_check_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_result: Mapped[dict] = mapped_column(JSON, default=dict)     # 最近一次检查：结论、耗时、错误


class WatchEventRow(TimestampMixin, Base):
    """AI 发现的事件。level：1 一般关注 / 2 重要 / 3 重大；direction：good 利好 / bad 利空 / neutral 中性。"""

    __tablename__ = "watch_events"

    item_id: Mapped[int] = mapped_column(ForeignKey("watch_items.id", ondelete="CASCADE"), index=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)               # 检查日期（北京时间）
    level: Mapped[int] = mapped_column(Integer, default=1)
    direction: Mapped[str] = mapped_column(String(8), default="neutral")
    title: Mapped[str] = mapped_column(String(200))
    summary: Mapped[str] = mapped_column(Text, default="")              # 发生了什么
    impact: Mapped[str] = mapped_column(Text, default="")               # 意味着什么、影响多大
    sources: Mapped[list] = mapped_column(JSON, default=list)           # [{title, url, date}]
    model: Mapped[str] = mapped_column(String(255), default="")
    notified: Mapped[bool] = mapped_column(Boolean, default=False)
    read: Mapped[bool] = mapped_column(Boolean, default=False)
