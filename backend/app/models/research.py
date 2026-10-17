"""研究数据：只有当前值、查不到历史的数据，每天存一份快照，供以后做时点正确的研究。"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import JSON, Date, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.timeutils import utcnow
from app.db.types import UTCDateTime
from app.models.base import Base


class WestockSnapshotRow(Base):
    """westock 数据快照（一致预期、个股评分等）。

    as_of 是保存时的交易日（北京时间），fetched_at 是实际获取时间：研究时只能在 fetched_at 之后使用这条数据。
    status：ok 有数据 / empty 数据源返回暂无数据。原始文本 raw 一并保存，解析规则改了可以重新解析。
    """

    __tablename__ = "westock_snapshots"
    __table_args__ = (UniqueConstraint("as_of", "symbol", "kind"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    as_of: Mapped[date] = mapped_column(Date, index=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    kind: Mapped[str] = mapped_column(String(16))                    # consensus / score
    status: Mapped[str] = mapped_column(String(8), default="ok")
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    raw: Mapped[str] = mapped_column(Text, default="")
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
