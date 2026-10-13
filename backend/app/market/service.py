"""行情服务：数据源 + 本地缓存（数据库）。

调用方负责提供 Session 并提交事务。数据源调用是同步阻塞的，异步代码中请用 asyncio.to_thread。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from datetime import date, timedelta
from typing import Protocol

import pandas as pd
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.timeutils import utcnow
from app.market.calendar import TradingCalendar
from app.market.types import CorporateAction, DailyBar, Instrument
from app.models.market import (
    CorporateActionRow,
    DailyBarRow,
    DataSourceStatusRow,
    InstrumentRow,
    TradingDayRow,
)

logger = logging.getLogger(__name__)

# 日历最后一天距今少于该天数时刷新
_CALENDAR_MIN_AHEAD_DAYS = 14


class MarketDataProvider(Protocol):
    market: str

    def trading_days(self) -> list[date]: ...
    def daily_bars(self, inst: Instrument, start: date, end: date) -> list[DailyBar]: ...
    def corporate_actions(self, inst: Instrument) -> list[CorporateAction]: ...


def _instrument_from_row(row: InstrumentRow) -> Instrument:
    return Instrument(
        symbol=row.symbol, market=row.market, name=row.name, type=row.type, board=row.board,
        lot_size=row.lot_size, tick=row.tick, is_st=row.is_st, t0=row.t0,
        price_limit_pct=row.price_limit_pct, list_date=row.list_date, stamp_duty=row.stamp_duty,
        delisted=row.delisted_date,
    )


def _bar_from_row(row: DailyBarRow) -> DailyBar:
    return DailyBar(
        symbol=row.symbol, date=row.date, open=row.open, high=row.high, low=row.low, close=row.close,
        volume=row.volume, amount=row.amount, prev_close=row.prev_close, adj_factor=row.adj_factor,
    )


class MarketDataService:
    def __init__(self, session: Session, provider: MarketDataProvider):
        self.session = session
        self.provider = provider
        self.market = provider.market

    # ---------- 数据源状态 ----------

    def _checkpoint(self) -> None:
        """逐个标的提交：数据源请求较慢，不能在网络请求期间持有 SQLite 写锁（会阻塞其他赛道的日终流程）。"""
        self.session.commit()

    def _track[T](self, name: str, fn: Callable[[], T]) -> T:
        try:
            result = fn()
        except Exception as e:
            self._record_status(name, error=str(e)[:2000])
            raise
        self._record_status(name)
        return result

    def _record_status(self, name: str, error: str | None = None) -> None:
        """记录数据源成功 / 失败。用一条 INSERT … ON CONFLICT 原子写入：多个赛道并发拉数据时，
        「先查再插」会在两边都没查到时重复插入同名记录（唯一约束冲突），导致整个数据准备失败。"""
        t = DataSourceStatusRow.__table__
        now = utcnow()
        if error is None:
            values = {"last_success_at": now, "consecutive_failures": 0}
            update = dict(values)
        else:
            values = {"last_failure_at": now, "last_error": error, "consecutive_failures": 1}
            update = {"last_failure_at": now, "last_error": error,
                      "consecutive_failures": func.coalesce(t.c.consecutive_failures, 0) + 1}
        dialect = self.session.get_bind().dialect.name
        if dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert
        elif dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        else:  # 其他数据库：退回先查再写（不保证并发安全）
            row = self.session.get(DataSourceStatusRow, name) or DataSourceStatusRow(name=name)
            self.session.add(row)
            prev = row.consecutive_failures or 0
            for k, v in values.items():
                setattr(row, k, v)
            if error is not None:
                row.consecutive_failures = prev + 1
            self.session.flush()
            return
        stmt = insert(t).values(name=name, **values)
        self.session.execute(stmt.on_conflict_do_update(index_elements=[t.c.name], set_=update))
        # 会话里已加载的同名对象作废，下次读取时从数据库刷新
        key = self.session.identity_key(DataSourceStatusRow, name)
        if key in self.session.identity_map:
            self.session.expire(self.session.identity_map[key])

    # ---------- 交易日历 ----------

    def calendar(self, today: date | None = None) -> TradingCalendar:
        today = today or date.today()
        days = list(self.session.scalars(
            select(TradingDayRow.date).where(TradingDayRow.market == self.market).order_by(TradingDayRow.date)
        ))
        if not days or days[-1] < today + timedelta(days=_CALENDAR_MIN_AHEAD_DAYS):
            try:
                fresh = self._track(f"{self.market}.calendar", self.provider.trading_days)
                self.session.execute(delete(TradingDayRow).where(TradingDayRow.market == self.market))
                self.session.add_all(TradingDayRow(market=self.market, date=d) for d in fresh)
                self.session.flush()
                days = fresh
            except Exception:
                if not days:
                    raise
                logger.warning("%s 交易日历刷新失败，继续使用缓存（覆盖到 %s）", self.market, days[-1])
        return TradingCalendar(self.market, days)

    # ---------- 证券信息 ----------

    def upsert_instruments(self, instruments: Iterable[Instrument]) -> None:
        for inst in instruments:
            row = self.session.get(InstrumentRow, inst.symbol) or InstrumentRow(symbol=inst.symbol)
            row.market, row.name, row.type, row.board = inst.market, inst.name, inst.type, inst.board
            row.lot_size, row.tick, row.is_st, row.t0 = inst.lot_size, inst.tick, inst.is_st, inst.t0
            row.price_limit_pct, row.list_date, row.stamp_duty = inst.price_limit_pct, inst.list_date, inst.stamp_duty
            if inst.delisted is not None:
                row.delisted_date = inst.delisted
            self.session.add(row)
        self.session.flush()

    def get_instrument(self, symbol: str) -> Instrument:
        row = self.session.get(InstrumentRow, symbol)
        if row is None:
            raise KeyError(f"未知证券：{symbol}")
        return _instrument_from_row(row)

    def get_instruments(self, symbols: Iterable[str]) -> dict[str, Instrument]:
        symbols = list(symbols)
        rows = self.session.scalars(select(InstrumentRow).where(InstrumentRow.symbol.in_(symbols)))
        return {r.symbol: _instrument_from_row(r) for r in rows}

    # ---------- 日线 ----------

    def last_bar_date(self, symbol: str) -> date | None:
        return self.session.scalar(select(func.max(DailyBarRow.date)).where(DailyBarRow.symbol == symbol))

    def update_bars(self, instruments: Iterable[Instrument], until: date, history_start: date) -> dict[str, str]:
        """增量更新日线缓存到 until（含）。返回 {symbol: "ok" / "up-to-date" / 错误信息}。"""
        result: dict[str, str] = {}
        for inst in instruments:
            last = self.last_bar_date(inst.symbol)
            start = last + timedelta(days=1) if last else history_start
            if start > until:
                result[inst.symbol] = "up-to-date"
                continue
            try:
                bars = self._track(f"{self.market}.daily_bars",
                                   lambda inst=inst, start=start: self.provider.daily_bars(inst, start, until))
            except Exception as e:
                result[inst.symbol] = f"error: {e}"
                self._checkpoint()
                continue
            for b in bars:
                self.session.merge(DailyBarRow(
                    symbol=b.symbol, date=b.date, open=b.open, high=b.high, low=b.low, close=b.close,
                    volume=b.volume, amount=b.amount, prev_close=b.prev_close, adj_factor=b.adj_factor,
                ))
            self._checkpoint()
            result[inst.symbol] = "ok"
        return result

    def backfill_bars(self, instruments: Iterable[Instrument], start: date) -> dict[str, str]:
        """把日线缓存向前补到 start（量化回测需要多年历史）。已请求过的不再重复；返回 {symbol: 状态}。"""
        result: dict[str, str] = {}
        for inst in instruments:
            row = self.session.get(InstrumentRow, inst.symbol)
            if row is not None and row.history_from is not None and row.history_from <= start:
                result[inst.symbol] = "up-to-date"
                continue
            first = self.session.scalar(select(func.min(DailyBarRow.date)).where(DailyBarRow.symbol == inst.symbol))
            if first is None:
                result[inst.symbol] = "no-data"
                continue
            if first > start + timedelta(days=7):
                try:
                    bars = self._track(f"{self.market}.daily_bars",
                                       lambda inst=inst, first=first: self.provider.daily_bars(
                                           inst, start, first - timedelta(days=1)))
                except Exception as e:
                    result[inst.symbol] = f"error: {e}"
                    self._checkpoint()
                    continue
                for b in bars:
                    self.session.merge(DailyBarRow(
                        symbol=b.symbol, date=b.date, open=b.open, high=b.high, low=b.low, close=b.close,
                        volume=b.volume, amount=b.amount, prev_close=b.prev_close, adj_factor=b.adj_factor,
                    ))
                result[inst.symbol] = f"ok: {len(bars)}"
            else:
                result[inst.symbol] = "up-to-date"
            if row is not None:
                row.history_from = start
            self._checkpoint()
        return result

    def refetch_bars(self, instruments: Iterable[Instrument]) -> dict[str, str]:
        """重新下载已缓存区间的全部日线并覆盖。数据源口径修正后（如 ETF 份额折算）用来修复缓存。"""
        result: dict[str, str] = {}
        for inst in instruments:
            first, last = self.session.execute(select(func.min(DailyBarRow.date), func.max(DailyBarRow.date))
                                               .where(DailyBarRow.symbol == inst.symbol)).one()
            if first is None:
                result[inst.symbol] = "no-data"
                continue
            try:
                bars = self._track(f"{self.market}.daily_bars",
                                   lambda inst=inst, first=first, last=last: self.provider.daily_bars(
                                       inst, first, last))
            except Exception as e:
                result[inst.symbol] = f"error: {e}"
                self._checkpoint()
                continue
            changed = 0
            for b in bars:
                row = self.session.get(DailyBarRow, (b.symbol, b.date))
                if row is not None and row.adj_factor == b.adj_factor and row.prev_close == b.prev_close \
                        and row.close == b.close:
                    continue
                changed += 1
                self.session.merge(DailyBarRow(
                    symbol=b.symbol, date=b.date, open=b.open, high=b.high, low=b.low, close=b.close,
                    volume=b.volume, amount=b.amount, prev_close=b.prev_close, adj_factor=b.adj_factor,
                ))
            self._checkpoint()
            result[inst.symbol] = f"ok: {len(bars)} 根，修正 {changed} 根"
        return result

    def update_index_members(self, index_code: str) -> int:
        """刷新指数成分的纳入 / 剔除记录（以数据源为准整体替换）。返回记录数。"""
        from app.market.symbols import normalize_cn
        from app.models.market import IndexMemberRow

        rows = self._track(f"{self.market}.index_members", lambda: self.provider.index_members(index_code))
        if not rows:
            return 0
        self.session.execute(delete(IndexMemberRow).where(IndexMemberRow.index_code == index_code))
        seen: set[tuple[str, date]] = set()
        for code, name, in_d, out_d in rows:
            if (code, in_d) in seen:            # 数据源偶有重复行
                continue
            seen.add((code, in_d))
            self.session.add(IndexMemberRow(index_code=index_code, symbol=normalize_cn(code), name=name,
                                            in_date=in_d, out_date=out_d, source=self.provider.__class__.__name__))
        self.session.flush()
        return len(rows)

    def index_membership(self, index_code: str) -> dict[str, list[tuple[date, date | None]]]:
        from app.models.market import IndexMemberRow

        out: dict[str, list[tuple[date, date | None]]] = {}
        for r in self.session.scalars(select(IndexMemberRow).where(IndexMemberRow.index_code == index_code)):
            out.setdefault(r.symbol, []).append((r.in_date, r.out_date))
        return out

    def get_bars(self, symbol: str, start: date, end: date) -> list[DailyBar]:
        rows = self.session.scalars(
            select(DailyBarRow)
            .where(DailyBarRow.symbol == symbol, DailyBarRow.date >= start, DailyBarRow.date <= end)
            .order_by(DailyBarRow.date)
        )
        return [_bar_from_row(r) for r in rows]

    def get_bars_on(self, symbols: Iterable[str], d: date) -> dict[str, DailyBar]:
        rows = self.session.scalars(
            select(DailyBarRow).where(DailyBarRow.symbol.in_(list(symbols)), DailyBarRow.date == d)
        )
        return {r.symbol: _bar_from_row(r) for r in rows}

    def latest_bars(self, symbols: Iterable[str], on_or_before: date) -> dict[str, DailyBar]:
        """每个标的在指定日期（含）之前的最后一根日线，用于停牌股估值。"""
        out: dict[str, DailyBar] = {}
        for s in symbols:
            row = self.session.scalars(
                select(DailyBarRow).where(DailyBarRow.symbol == s, DailyBarRow.date <= on_or_before)
                .order_by(DailyBarRow.date.desc()).limit(1)
            ).first()
            if row:
                out[s] = _bar_from_row(row)
        return out

    def history_frame(self, symbol: str, end: date, lookback: int) -> pd.DataFrame:
        """截至 end（含）的最近 lookback 根日线，附前复权价（以 end 当日为基准）。用于技术指标和展示走势。"""
        rows = list(self.session.scalars(
            select(DailyBarRow).where(DailyBarRow.symbol == symbol, DailyBarRow.date <= end)
            .order_by(DailyBarRow.date.desc()).limit(lookback)
        ))
        rows.reverse()
        if not rows:
            return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume", "amount"])
        base = rows[-1].adj_factor
        data = []
        for r in rows:
            k = r.adj_factor / base
            data.append({
                "date": r.date,
                "open": float(r.open * k), "high": float(r.high * k), "low": float(r.low * k),
                "close": float(r.close * k), "volume": r.volume, "amount": float(r.amount),
            })
        return pd.DataFrame(data)

    # ---------- 除权除息 ----------

    def update_industries(self, instruments: Iterable[Instrument], max_age_days: int = 30) -> dict[str, str]:
        """刷新股票的行业分类（数据源支持时）。最近 max_age_days 天内拉取过的跳过。"""
        from app.models.market import IndustryRow

        if not hasattr(self.provider, "industry_history"):
            return {}
        fresh_after = utcnow() - timedelta(days=max_age_days)
        stocks = [i for i in instruments if i.type == "stock"]
        fresh = set(self.session.scalars(select(IndustryRow.symbol).where(
            IndustryRow.symbol.in_([i.symbol for i in stocks]), IndustryRow.fetched_at >= fresh_after)))
        result: dict[str, str] = {}
        for inst in stocks:
            if inst.symbol in fresh:
                continue
            try:
                rows = self._track(f"{self.market}.industry",
                                   lambda inst=inst: self.provider.industry_history(inst.symbol.split(".")[0]))
            except Exception as e:
                result[inst.symbol] = f"error: {e}"
                self._checkpoint()
                continue
            if rows:
                self.session.execute(delete(IndustryRow).where(IndustryRow.symbol == inst.symbol))
                for d, l1, l2 in rows:
                    self.session.add(IndustryRow(symbol=inst.symbol, start_date=d, level1=l1, level2=l2,
                                                 source="cninfo.sw2021"))
            self._checkpoint()
            result[inst.symbol] = "ok" if rows else "empty"
        return result

    def update_corporate_actions(self, instruments: Iterable[Instrument]) -> dict[str, str]:
        result: dict[str, str] = {}
        for inst in instruments:
            if inst.type == "index":
                continue
            try:
                actions = self._track(f"{self.market}.corporate_actions",
                                      lambda inst=inst: self.provider.corporate_actions(inst))
            except Exception as e:
                result[inst.symbol] = f"error: {e}"
                self._checkpoint()
                continue
            for a in actions:
                row = self.session.scalars(select(CorporateActionRow).where(
                    CorporateActionRow.symbol == a.symbol, CorporateActionRow.ex_date == a.ex_date)).first()
                row = row or CorporateActionRow(symbol=a.symbol, ex_date=a.ex_date)
                row.cash_per_share, row.shares_per_share, row.source = (
                    a.cash_per_share, a.shares_per_share, a.source)
                self.session.add(row)
            self._checkpoint()
            result[inst.symbol] = "ok"
        return result

    def actions_on(self, symbols: Iterable[str], ex_date: date) -> list[CorporateAction]:
        rows = self.session.scalars(select(CorporateActionRow).where(
            CorporateActionRow.symbol.in_(list(symbols)), CorporateActionRow.ex_date == ex_date))
        return [CorporateAction(symbol=r.symbol, ex_date=r.ex_date, cash_per_share=r.cash_per_share,
                                shares_per_share=r.shares_per_share, source=r.source) for r in rows]

    # ---------- 状态 ----------

    def source_status(self) -> list[DataSourceStatusRow]:
        return list(self.session.scalars(select(DataSourceStatusRow).order_by(DataSourceStatusRow.name)))
