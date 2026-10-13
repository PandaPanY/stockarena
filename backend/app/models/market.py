"""行情相关的表：全局共享，不归属任何用户。"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import JSON, Boolean, Date, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.timeutils import utcnow
from app.db.types import DecimalString, UTCDateTime
from app.models.base import Base


class InstrumentRow(Base):
    __tablename__ = "instruments"

    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    market: Mapped[str] = mapped_column(String(4), index=True)
    name: Mapped[str] = mapped_column(String(64))
    type: Mapped[str] = mapped_column(String(10))
    board: Mapped[str] = mapped_column(String(10))
    lot_size: Mapped[int] = mapped_column(Integer, default=100)
    tick: Mapped[Decimal] = mapped_column(DecimalString())
    is_st: Mapped[bool] = mapped_column(Boolean, default=False)
    t0: Mapped[bool] = mapped_column(Boolean, default=False)
    price_limit_pct: Mapped[Decimal | None] = mapped_column(DecimalString(), nullable=True)
    list_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    stamp_duty: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    delisted_date: Mapped[date | None] = mapped_column(Date, nullable=True)   # 退市日期（之后不再更新行情）
    # 已向数据源请求过的最早日期：早于该日期没有数据（未上市等）时，不再重复回填
    history_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class IndexMemberRow(Base):
    """指数成分的纳入 / 剔除记录（时点股票池用）。out_date 为空表示仍在指数中。"""

    __tablename__ = "index_members"
    __table_args__ = (UniqueConstraint("index_code", "symbol", "in_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    index_code: Mapped[str] = mapped_column(String(16), index=True)
    symbol: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(64), default="")
    in_date: Mapped[date] = mapped_column(Date)
    out_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="")
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class IndustryRow(Base):
    """行业分类（申万 2021 一级 / 二级）及其生效日期，来自巨潮资讯的行业变更记录。

    同一只股票可能有多条（行业调整）；某日的行业取 start_date ≤ 该日的最近一条。
    分类标准 2021-07 才发布，之前的日期沿用最早一条（行业很少变化，近似处理）。
    """

    __tablename__ = "industries"
    __table_args__ = (UniqueConstraint("symbol", "start_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    start_date: Mapped[date] = mapped_column(Date)
    level1: Mapped[str] = mapped_column(String(32))
    level2: Mapped[str] = mapped_column(String(32), default="")
    source: Mapped[str] = mapped_column(String(32), default="")
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class DailyBarRow(Base):
    __tablename__ = "daily_bars"

    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    open: Mapped[Decimal] = mapped_column(DecimalString())
    high: Mapped[Decimal] = mapped_column(DecimalString())
    low: Mapped[Decimal] = mapped_column(DecimalString())
    close: Mapped[Decimal] = mapped_column(DecimalString())
    volume: Mapped[int] = mapped_column(Integer)
    amount: Mapped[Decimal] = mapped_column(DecimalString())
    prev_close: Mapped[Decimal | None] = mapped_column(DecimalString(), nullable=True)
    adj_factor: Mapped[Decimal] = mapped_column(DecimalString())
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class CorporateActionRow(Base):
    __tablename__ = "corporate_actions"
    __table_args__ = (UniqueConstraint("symbol", "ex_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    ex_date: Mapped[date] = mapped_column(Date, index=True)
    cash_per_share: Mapped[Decimal] = mapped_column(DecimalString())
    shares_per_share: Mapped[Decimal] = mapped_column(DecimalString())
    source: Mapped[str] = mapped_column(String(40), default="")


class TradingDayRow(Base):
    __tablename__ = "trading_days"

    market: Mapped[str] = mapped_column(String(4), primary_key=True)
    date: Mapped[date] = mapped_column(Date, primary_key=True)


class DataSourceStatusRow(Base):
    """数据源健康状态，系统页展示。"""

    __tablename__ = "data_source_status"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_success_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_failure_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)


class MinuteBarRow(Base):
    """由实时快照轮询合成的 1 分钟 K 线（分钟级赛道使用）。ts 为该分钟开始时刻（UTC）。"""

    __tablename__ = "minute_bars"

    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime(), primary_key=True)
    open: Mapped[Decimal] = mapped_column(DecimalString())
    high: Mapped[Decimal] = mapped_column(DecimalString())
    low: Mapped[Decimal] = mapped_column(DecimalString())
    close: Mapped[Decimal] = mapped_column(DecimalString())
    volume: Mapped[int] = mapped_column(Integer, default=0)       # 本分钟成交量（由累计成交量相减得到）
    cum_volume: Mapped[int] = mapped_column(Integer, default=0)   # 截至本分钟的当日累计成交量
    prev_close: Mapped[Decimal | None] = mapped_column(DecimalString(), nullable=True)   # 当日前收盘价


class NewsRow(Base):
    """个股新闻（按发布时间过滤，决策只能看到快照时刻之前发布的新闻）。"""

    __tablename__ = "news"
    __table_args__ = (UniqueConstraint("symbol", "url"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    published_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    title: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(64), default="")
    url: Mapped[str] = mapped_column(String(512))
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class FundamentalRow(Base):
    """财报指标。available_from 为可以使用这份财报的最早日期（公告日；没有公告日时按法定披露截止日，宁晚勿早）。"""

    __tablename__ = "fundamentals"

    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    period: Mapped[date] = mapped_column(Date, primary_key=True)          # 报告期末
    available_from: Mapped[date] = mapped_column(Date, index=True)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    source: Mapped[str] = mapped_column(String(32), default="")
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
