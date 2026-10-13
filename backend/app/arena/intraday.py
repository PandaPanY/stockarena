"""盘中引擎：分钟级赛道的实时行情轮询、分钟 K 线合成、逐分钟撮合、定时决策与异动触发（PRD 4.6.1、US-5.2）。

每 POLL_SECONDS 秒执行一次 tick：
  1. 找出所有运行中的分钟级赛道，按市场分组
  2. 判断市场状态（开盘 / 午休 / 收盘）；开盘时拉取实时快照，合成 1 分钟 K 线
  3. 每个交易日首次开盘：处理除权除息、补齐截至昨天的日线（用于技术指标）
  4. 分钟 K 线完成时：撮合已生效的订单（同一轮所有选手的订单从同一分钟开始生效）
  5. 到达决策时点（开盘后 2 分钟起每 N 分钟）或持仓异动超过阈值时，发起决策轮次
  6. 收盘：输出最后的分钟 K 线并撮合，当日有效的订单失效；收盘净值由日终流程记录
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from functools import lru_cache

import pandas as pd
from sqlalchemy import func, select

from app.arena import cycle, orders
from app.arena.accounts import append_ledger, load_state, save_state
from app.arena.deps import market_service
from app.core.timeutils import ensure_utc, to_market_time, utcnow
from app.db.session import get_sessionmaker
from app.market.markets import get_market
from app.market.realtime import MinuteBar, MinuteBarBuilder, Quote, SinaQuoteClient
from app.models import AccountRow, EventRow, FillRow, MinuteBarRow, PositionRow, RoundRow, TrackRow
from app.trading.engine import apply_actions, settle_bar
from app.trading.params import RuleParams

logger = logging.getLogger(__name__)

POLL_SECONDS = 15
FIRST_ROUND_DELAY = timedelta(minutes=2)      # 开盘后等第一根分钟 K 线
NO_NEW_ROUND_BEFORE_CLOSE = timedelta(minutes=10)
MIN_EVENT_GAP = timedelta(minutes=3)          # 两轮之间至少间隔


@lru_cache
def _exchange_calendar(code: str):
    import exchange_calendars as xc

    return xc.get_calendar(code)


@dataclass(frozen=True)
class Phase:
    state: str                  # open / break / closed
    session: date | None
    open: datetime | None = None
    close: datetime | None = None


def market_phase(market: str, now: datetime) -> Phase:
    """市场当前状态。依据 exchange_calendars 的交易时段（含午休）。"""
    spec = get_market(market)
    cal = _exchange_calendar(spec.calendar)
    local_day = to_market_time(now, market).date()
    ts = pd.Timestamp(ensure_utc(now))
    try:
        if not cal.is_session(local_day):
            return Phase("closed", None)
        open_, close = cal.session_open(local_day), cal.session_close(local_day)
    except Exception:   # 超出日历范围
        return Phase("closed", None)
    o, c = open_.to_pydatetime(), close.to_pydatetime()
    if ts < open_ or ts >= close:
        return Phase("closed", local_day, o, c)
    return Phase("open" if cal.is_open_on_minute(ts) else "break", local_day, o, c)


@dataclass
class TrackInfo:
    id: int
    market: str
    status: str
    symbols: list[str]
    benchmark: str
    interval_min: int
    event_move_pct: Decimal | None
    event_max_per_day: int
    start_date: date
    intraday_day: date | None
    held: set[str] = field(default_factory=set)


def _load_tracks() -> list[TrackInfo]:
    with get_sessionmaker()() as s:
        out = []
        for t in s.scalars(select(TrackRow).where(TrackRow.frequency == "intraday",
                                                  TrackRow.status.in_(["running", "paused"]))):
            held = set(s.scalars(select(PositionRow.symbol).join(AccountRow, PositionRow.account_id == AccountRow.id)
                                 .where(AccountRow.track_id == t.id)))
            out.append(TrackInfo(t.id, t.market, t.status, list(t.symbols), t.benchmark_symbol, t.interval_min,
                                 t.event_move_pct, t.event_max_per_day, t.start_date, t.intraday_day, held))
        return out


def _store_bars(bars: list[MinuteBar]) -> None:
    with get_sessionmaker()() as s:
        for b in bars:
            s.merge(MinuteBarRow(symbol=b.symbol, ts=b.ts, open=b.open, high=b.high, low=b.low, close=b.close,
                                 volume=b.volume, cum_volume=b.cum_volume, prev_close=b.prev_close))
        s.commit()


def _begin_day(track_id: int, session: date) -> None:
    """每个交易日首次开盘：补齐截至昨天的日线、更新并处理除权除息。"""
    with get_sessionmaker()() as s:
        track = s.get(TrackRow, track_id)
        svc = market_service(s, track.market)
        cal = svc.calendar(session)
        insts = svc.get_instruments([*track.symbols, track.benchmark_symbol])
        svc.update_bars(list(insts.values()), until=cal.previous(session),
                        history_start=session - timedelta(days=cycle.HISTORY_LOOKBACK_DAYS))
        params = RuleParams(**track.rule_params)
        for acc in s.scalars(select(AccountRow).where(AccountRow.track_id == track.id, AccountRow.status == "active")):
            state = load_state(s, acc)
            if not state.positions:
                continue
            svc.update_corporate_actions([insts[sym] for sym in state.positions if sym in insts])
            start_cash = state.cash
            st = apply_actions(state, svc.actions_on(list(state.positions), session), params, session, insts)
            if st.ledger:
                append_ledger(s, acc.id, session, st.ledger, start_cash)
                save_state(s, acc, state)
                for e in st.events:
                    s.add(EventRow(track_id=track.id, account_id=acc.id, trade_date=session, type=e.type,
                                   message=f"{e.symbol} {e.message}"))
        track.intraday_day = session
        s.commit()


def _match(track_id: int, bars: dict[str, MinuteBar], session: date, end_of_session: bool) -> int:
    """用刚完成的分钟 K 线撮合各账户已生效的订单。返回成交笔数。"""
    n = 0
    with get_sessionmaker()() as s:
        track = s.get(TrackRow, track_id)
        svc = market_service(s, track.market)
        insts = svc.get_instruments(track.symbols)
        params = RuleParams(**track.rule_params)
        bar_ts = min((b.ts for b in bars.values()), default=None)
        label = f"分钟线 {to_market_time(bar_ts, track.market):%H:%M}" if bar_ts else "收盘"
        for acc in s.scalars(select(AccountRow).where(AccountRow.track_id == track.id, AccountRow.status == "active")):
            due = orders.due_orders(s, acc.id, session, now=bar_ts or utcnow())
            if not due:
                continue
            state = load_state(s, acc)
            start_cash = state.cash
            st = settle_bar(state, [orders.to_pending(o) for o in due.values()], bars, insts, params, session,
                            end_of_session)
            for f in st.fills:
                s.add(FillRow(order_id=f.order_id, account_id=acc.id, trade_date=session, symbol=f.symbol,
                              side=f.side, qty=f.qty, price=f.price, amount=f.amount,
                              fees={k: str(v) for k, v in f.fees.items()}, total_fee=f.total_fee,
                              realized_pnl=f.realized_pnl, price_source=label))
            orders.apply_settlement(s, due, st, session)
            if st.ledger:
                append_ledger(s, acc.id, session, st.ledger, start_cash)
            save_state(s, acc, state)
            n += len(st.fills)
        s.commit()
    return n


def _last_round_at(track_id: int, session: date) -> datetime | None:
    with get_sessionmaker()() as s:
        return s.scalar(select(func.max(RoundRow.snapshot_at)).where(RoundRow.track_id == track_id,
                                                                     RoundRow.data_date == session))


class IntradayEngine:
    def __init__(self, client=None):
        self.client = client or SinaQuoteClient()
        self.builders: dict[str, MinuteBarBuilder] = defaultdict(MinuteBarBuilder)
        self.state: dict[str, str] = {}
        self.round_tasks: dict[int, asyncio.Task] = {}
        self.round_prices: dict[int, dict[str, Decimal]] = {}
        self.last_round: dict[int, datetime] = {}
        self.events: dict[tuple[int, date], int] = defaultdict(int)
        self._stopped = asyncio.Event()

    async def run(self) -> None:
        logger.info("盘中引擎已启动（每 %d 秒轮询）", POLL_SECONDS)
        while not self._stopped.is_set():
            try:
                await self.tick(utcnow())
            except Exception:
                logger.exception("盘中引擎执行出错")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopped.wait(), timeout=POLL_SECONDS)

    def stop(self) -> None:
        self._stopped.set()
        for task in self.round_tasks.values():
            task.cancel()

    async def tick(self, now: datetime) -> None:
        tracks = await asyncio.to_thread(_load_tracks)
        groups: dict[str, list[TrackInfo]] = defaultdict(list)
        for t in tracks:
            groups[t.market].append(t)
        for market, trs in groups.items():
            await self._tick_market(market, trs, now)

    async def _tick_market(self, market: str, trs: list[TrackInfo], now: datetime) -> None:
        phase = market_phase(market, now)
        prev = self.state.get(market)
        self.state[market] = phase.state
        builder = self.builders[market]
        if phase.state != "open":
            if prev == "open":
                # 午休或收盘：输出最后一根分钟 K 线并撮合；收盘时当日有效订单失效
                await self._finish_bars(trs, builder.flush(), phase.session, end=phase.state == "closed")
            elif prev == "break" and phase.state == "closed":
                await self._finish_bars(trs, [], phase.session, end=True)
            if phase.state == "closed" and prev in ("open", "break"):
                builder.reset_day()
            return

        symbols = sorted({s for t in trs for s in t.symbols} | {t.benchmark for t in trs})
        try:
            quotes = await asyncio.to_thread(self.client.fetch, symbols)
        except Exception as e:
            logger.warning("%s 实时行情获取失败：%s", market, e)
            return
        # 当天还没有成交的标的（行情日期不是今天）不参与合成
        done = builder.update(quotes, now)
        for t in trs:
            if t.intraday_day != phase.session and t.start_date <= phase.session:
                await asyncio.to_thread(_begin_day, t.id, phase.session)
                t.intraday_day = phase.session
        await self._finish_bars(trs, done, phase.session, end=False)
        for t in trs:
            if t.status == "running" and t.start_date <= phase.session:
                await self._maybe_decide(t, now, phase, quotes)

    async def _finish_bars(self, trs: list[TrackInfo], bars: list[MinuteBar], session: date | None,
                           end: bool) -> None:
        if bars:
            await asyncio.to_thread(_store_bars, bars)
        if session is None or (not bars and not end):
            return
        by_symbol = {b.symbol: b for b in bars}
        for t in trs:
            if t.start_date <= session:
                await asyncio.to_thread(_match, t.id, by_symbol, session, end)

    async def _maybe_decide(self, t: TrackInfo, now: datetime, phase: Phase, quotes: dict[str, Quote]) -> None:
        task = self.round_tasks.get(t.id)
        if task is not None and not task.done():
            return
        first = phase.open + FIRST_ROUND_DELAY
        if now < first or now > phase.close - NO_NEW_ROUND_BEFORE_CLOSE:
            return
        slot = first + timedelta(minutes=t.interval_min) * ((now - first) // timedelta(minutes=t.interval_min))
        last = self.last_round.get(t.id)
        if last is None or last.date() != now.date():
            last = await asyncio.to_thread(_last_round_at, t.id, phase.session)
        trigger = None
        half = timedelta(minutes=t.interval_min) / 2
        if last is None or (last < slot and now - last >= half):
            trigger = "interval"
        elif (t.event_move_pct and now - last >= MIN_EVENT_GAP
              and self.events[(t.id, phase.session)] < t.event_max_per_day):
            base = self.round_prices.get(t.id, {})
            for sym in t.held | {t.benchmark}:
                q, p0 = quotes.get(sym), base.get(sym)
                if q is not None and p0 and abs(q.price / p0 - 1) >= t.event_move_pct:
                    trigger = "event"
                    self.events[(t.id, phase.session)] += 1
                    logger.info("赛道 %s：%s 较上一轮变动 %.2f%%，触发异动决策", t.id, sym,
                                float(q.price / p0 - 1) * 100)
                    break
        if trigger is None:
            return
        self.last_round[t.id] = now
        self.round_prices[t.id] = {s: q.price for s, q in quotes.items()}
        self.round_tasks[t.id] = asyncio.get_running_loop().create_task(
            self._run_round(t.id, dict(quotes), now, trigger))

    async def _run_round(self, track_id: int, quotes: dict[str, Quote], now: datetime, trigger: str) -> None:
        from app.arena import tasks

        started = utcnow().isoformat()
        tasks.last_result[track_id] = {"running": True, "started_at": started,
                                       "message": f"盘中决策中（{'异动触发' if trigger == 'event' else '定时'}）…"}
        msg = await cycle.run_live_round(track_id, quotes, now, trigger)
        tasks.last_result[track_id] = {"running": False, "started_at": started,
                                       "finished_at": utcnow().isoformat(), "message": msg}
        logger.info("赛道 %s（盘中 %s）：%s", track_id, trigger, msg)


_engine: IntradayEngine | None = None


def get_engine() -> IntradayEngine:
    global _engine
    if _engine is None:
        _engine = IntradayEngine()
    return _engine
