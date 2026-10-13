"""赛道日终流程（PRD 4.6.1）。

每个交易日 T 收盘后：
  1. 更新行情（含除权除息信息）
  2. 结算 T 日：处理除权除息 → 以 T 日开盘价撮合 T-1 日决策提交的订单 → 写入 T 日收盘净值
  3. 决策轮次：用 T 日收盘数据生成统一快照，并发请求所有选手 → 风控预检 → 订单在 T+1 日开盘撮合

系统停机错过的交易日会按顺序补结算；中间错过的决策轮次记为「错过」，只对最新交易日做决策。
所有步骤幂等：重复执行不会重复结算或重复决策（手动强制决策除外）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.context import (
    MarketSnapshot,
    account_context,
    benchmark_info,
    build_market_table,
    rules_summary,
    snapshot_hash,
)
from app.agent.parse import parse_decision
from app.agent.prompts import render
from app.agent.runner import DecisionOutcome, run_llm_decision, run_llm_tools
from app.arena import extras, orders, strategy, tools
from app.arena.accounts import append_ledger, load_state, save_state, write_nav
from app.arena.anonymize import Anonymizer
from app.arena.deps import market_service
from app.arena.lifecycle import latest_complete_date
from app.baselines import BaselineDecision, decide_baseline
from app.core.timeutils import to_market_time, utcnow
from app.db.session import get_sessionmaker
from app.llm import ProviderConfig, calc_cost, create_client
from app.market.markets import get_market
from app.market.types import DailyBar
from app.market.universe import BENCHMARK_NAMES
from app.models import (
    AccountRow,
    AgentRow,
    DecisionRow,
    EventRow,
    FillRow,
    NavSnapshotRow,
    OrderRow,
    PositionRow,
    PromptTemplateRow,
    RoundRow,
    TrackRow,
)
from app.trading.engine import settle_day
from app.trading.margin import check_margin
from app.trading.market_rules import get_rules
from app.trading.params import RuleParams
from app.trading.portfolio import AccountState
from app.trading.risk import precheck
from app.trading.targets import targets_to_intents
from app.trading.types import SIDE_LABELS, LedgerEntry, OrderIntent

logger = logging.getLogger(__name__)

HISTORY_LOOKBACK_DAYS = 150       # 首次拉取行情的历史长度（自然日），用于计算 20 日指标
INDICATOR_BARS = 30
MIN_BAR_COVERAGE = 0.8            # 当日有行情的标的比例低于该值时视为数据未就绪
LLM_CONCURRENCY = 3

_locks: dict[int, asyncio.Lock] = {}
_actions_refreshed: set[tuple[int, date]] = set()
_replaying: set[int] = set()     # 正在回放的赛道（定时任务跳过，避免重复回放）


def _lock(track_id: int) -> asyncio.Lock:
    return _locks.setdefault(track_id, asyncio.Lock())


def _event(s: Session, track_id: int | None, d: date | None, type_: str, message: str, level: str = "info",
           account_id: int | None = None, detail: dict | None = None) -> None:
    s.add(EventRow(track_id=track_id, account_id=account_id, trade_date=d, type=type_, level=level,
                   message=message, detail=detail or {}))


# ====================================================================== 计划


@dataclass
class CyclePlan:
    settle_days: list[date] = field(default_factory=list)
    latest: date | None = None
    decision_date: date | None = None
    fill_date: date | None = None
    message: str = ""
    intraday: bool = False
    reflect: bool = False           # 结算后检查是否有到期的复盘


def _plan(track_id: int, now: datetime) -> CyclePlan:
    with get_sessionmaker()() as s:
        track = s.get(TrackRow, track_id)
        if track is None or track.status not in ("running", "paused") or track.start_date is None:
            return CyclePlan(message="赛道未在运行")
        svc = market_service(s, track.market)
        now_local = to_market_time(now, track.market)
        cal = svc.calendar(now_local.date())
        latest = latest_complete_date(cal, now_local)
        if track.end_date:
            latest = min(latest, cal.latest_on_or_before(track.end_date))
        if latest < track.start_date:
            s.commit()
            return CyclePlan(message=f"等待 {track.start_date} 收盘后进行首轮决策")

        # 行情更新
        logger.info("赛道 %s：更新行情至 %s（%d 个标的）", track.id, latest, len(track.symbols))
        insts = svc.get_instruments([*track.symbols, track.benchmark_symbol])
        result = svc.update_bars(list(insts.values()), until=latest,
                                 history_start=track.start_date - timedelta(days=HISTORY_LOOKBACK_DAYS))
        failed = {k: v for k, v in result.items() if v.startswith("error")}
        if failed:
            _event(s, track.id, latest, "data_error", f"{len(failed)} 个标的行情更新失败", "warning",
                   detail={"failed": dict(list(failed.items())[:10])})
        key = (track.id, date.today())
        if key not in _actions_refreshed:
            svc.update_corporate_actions([i for i in insts.values() if i.type != "index"])
            _actions_refreshed.add(key)

        _ensure_initial_nav(s, track, svc, cal)
        # 日频：首日只决策不结算；分钟级：首日盘中就有交易，收盘后需要结算净值
        first = track.start_date if track.frequency != "intraday" else cal.previous(track.start_date)
        after = track.last_settled_date or first
        plan = CyclePlan(settle_days=cal.between(after + timedelta(days=1), latest), latest=latest,
                         intraday=track.frequency == "intraday")
        ended = track.end_date is not None and latest >= track.end_date
        if track.status == "running" and not ended and track.frequency != "intraday":
            plan.decision_date, plan.fill_date = latest, cal.next(latest)
        plan.reflect = track.status == "running" and not ended
        s.commit()
        return plan


def _ensure_initial_nav(s: Session, track: TrackRow, svc, cal) -> None:
    # 分钟级赛道在首日盘中交易，初始净值记在前一个交易日
    d = track.start_date if track.frequency != "intraday" else cal.previous(track.start_date)
    bench = svc.get_bars_on([track.benchmark_symbol], d).get(track.benchmark_symbol)
    for acc in s.scalars(select(AccountRow).where(AccountRow.track_id == track.id)):
        has_nav = s.scalars(select(NavSnapshotRow.id).where(NavSnapshotRow.account_id == acc.id).limit(1)).first()
        if has_nav is None:
            write_nav(s, acc.id, d, AccountState(cash=acc.cash), {}, bench.close if bench else None)


# ====================================================================== 结算


def _settle(track_id: int, d: date, is_latest: bool) -> bool:
    with get_sessionmaker()() as s:
        track = s.get(TrackRow, track_id)
        svc = market_service(s, track.market)
        bars = svc.get_bars_on([*track.symbols, track.benchmark_symbol], d)
        covered = sum(1 for sym in track.symbols if sym in bars)
        if track.benchmark_symbol not in bars or covered < MIN_BAR_COVERAGE * len(track.symbols):
            _event(s, track.id, d, "data_not_ready",
                   f"{d} 行情尚未就绪（{covered}/{len(track.symbols)}），稍后重试", "warning")
            s.commit()
            return False

        insts = svc.get_instruments(track.symbols)
        params = RuleParams(**track.rule_params)
        bench_close = bars[track.benchmark_symbol].close
        cal = svc.calendar(d)
        days = DayContext(d, cal.previous(d), cal.next(d))
        for acc in s.scalars(select(AccountRow).where(AccountRow.track_id == track.id,
                                                      AccountRow.status == "active")):
            _settle_account(s, svc, track, acc, days, bars, insts, params, bench_close)

        track.last_settled_date = d
        if not is_latest and s.scalars(select(RoundRow.id).where(RoundRow.track_id == track.id,
                                                                 RoundRow.data_date == d)).first() is None:
            s.add(RoundRow(track_id=track.id, data_date=d, trigger="schedule", status="missed",
                           note="系统未运行或数据延迟，跳过该日决策", finished_at=utcnow()))
        if track.end_date and d >= track.end_date:
            track.status = "finished"
            _event(s, track.id, d, "track_finished", "赛道已到结束日期")
        s.commit()
        return True


@dataclass
class DayContext:
    today: date
    previous: date          # 上一个交易日
    next: date              # 下一个交易日（计息天数、强平订单的撮合日）


def _settle_account(s: Session, svc, track: TrackRow, acc: AccountRow, days: DayContext, bars, insts,
                    params: RuleParams, bench_close: Decimal) -> None:
    d = days.today
    if track.frequency == "intraday":
        # 分钟级赛道：盘中已逐分钟撮合、开盘前已处理除权，收盘后计息、检查保证金并记录净值
        orders.due_orders(s, acc.id, d)
        state = load_state(s, acc)
        start_cash = state.cash
        entries = _financing_and_margin(s, svc, track, acc, state, days, params, insts)
        if entries:
            append_ledger(s, acc.id, d, entries, start_cash)
            save_state(s, acc, state)
        _write_close_nav(s, svc, acc, state, d, bench_close)
        return
    state = load_state(s, acc)
    start_cash = state.cash
    due = orders.due_orders(s, acc.id, d)
    actions = svc.actions_on(list(state.positions), d) if state.positions else []
    marks = {sym: b.close for sym, b in svc.latest_bars(list(state.positions), days.previous).items()} \
        if params.leveraged and state.positions else None

    st = settle_day(state, [orders.to_pending(o) for o in due.values()], bars, insts, actions, params, d, marks)

    for f in st.fills:
        s.add(FillRow(order_id=f.order_id, account_id=acc.id, trade_date=d, symbol=f.symbol, side=f.side,
                      qty=f.qty, price=f.price, amount=f.amount, fees={k: str(v) for k, v in f.fees.items()},
                      total_fee=f.total_fee, realized_pnl=f.realized_pnl, price_source=f"日线开盘价 {d}"))
    orders.apply_settlement(s, due, st, d)
    for e in st.events:
        if e.type == "corporate_action":
            _event(s, track.id, d, e.type, f"{e.symbol} {e.message}", account_id=acc.id)

    st.ledger += _financing_and_margin(s, svc, track, acc, state, days, params, insts)
    balance = append_ledger(s, acc.id, d, st.ledger, start_cash)
    if balance != state.cash:
        _event(s, track.id, d, "reconcile_error", f"流水与现金不一致：{balance} ≠ {state.cash}", "error",
               account_id=acc.id)
    save_state(s, acc, state)
    _write_close_nav(s, svc, acc, state, d, bench_close)


def _close_prices(svc, state: AccountState, d: date) -> dict[str, Decimal]:
    held = list(state.positions)
    prices = {sym: b.close for sym, b in (svc.latest_bars(held, d) if held else {}).items()}
    for sym, pos in state.positions.items():
        prices.setdefault(sym, pos.avg_cost)
    return prices


def _financing_and_margin(s: Session, svc, track: TrackRow, acc: AccountRow, state: AccountState, days: DayContext,
                          params: RuleParams, insts) -> list[LedgerEntry]:
    """收盘后：计提融资利息与融券费，检查保证金比率（追保 / 强平）。只做多的赛道直接跳过。"""
    if not params.leveraged:
        return []
    d = days.today
    prices = _close_prices(svc, state, d)
    # 收盘时持有的融资负债与空头计息到下一个交易日（周五、节前收盘计入周末和假期）
    entries = state.accrue_financing(prices, params, (days.next - d).days)
    in_call = acc.margin_call_since is not None and acc.margin_call_since < d
    res = check_margin(state, prices, params, in_call, insts)
    if res.level == "ok":
        if acc.margin_call_since is not None:
            acc.margin_call_since = None
            _event(s, track.id, d, "margin_restored", "保证金比率已恢复到维持线以上", account_id=acc.id,
                   detail={"ratio": str(res.ratio)})
        return entries
    if acc.margin_call_since is None:
        acc.margin_call_since = d
    if res.level == "call":
        _event(s, track.id, d, "margin_call", f"追保通知：{res.message}", "warning", account_id=acc.id,
               detail={"ratio": str(res.ratio)})
        return entries
    # 强制平仓：替换尚未成交的强平单，新订单在下一交易日开盘撮合
    for row in s.scalars(select(OrderRow).where(OrderRow.account_id == acc.id, OrderRow.status == "pending",
                                                OrderRow.forced.is_(True))):
        row.status, row.note = "cancelled", "被新的强制平仓计划替换"
    for it in res.orders:
        s.add(orders.new_order(acc.id, None, it, d, days.next))
    _event(s, track.id, d, "forced_liquidation", res.message, "error", account_id=acc.id,
           detail={"ratio": str(res.ratio), "orders": [f"{it.side} {it.symbol} {it.qty}" for it in res.orders]})
    return entries


def _write_close_nav(s: Session, svc, acc: AccountRow, state: AccountState, d: date, bench_close) -> None:
    # 持仓标的没有任何行情时按成本估值（理论上不会发生）
    write_nav(s, acc.id, d, state, _close_prices(svc, state, d), bench_close)


# ====================================================================== 决策


@dataclass
class LiveContext:
    """分钟级决策：用实时行情快照代替收盘日线。"""

    now: datetime                       # 快照时刻（UTC）
    quotes: dict                        # 统一代码 → realtime.Quote


@dataclass
class DecisionJob:
    account_id: int
    agent_name: str
    kind: str
    state: AccountState
    baseline_type: str | None = None
    provider: ProviderConfig | None = None
    model: str | None = None
    params: dict = field(default_factory=dict)
    json_mode: bool = True
    system: str | None = None
    user: str | None = None
    template_name: str | None = None
    template_version: int | None = None
    strategy_version: int | None = None
    tool_ctx: tools.ToolContext | None = None      # 工具模式
    quant_strategy_id: int | None = None           # 量化选手跟随的策略
    quant_portfolio_id: int | None = None          # 或跟随的多风格组合


@dataclass
class RoundPrep:
    round_id: int
    track_id: int
    snapshot: MarketSnapshot
    output_mode: str
    timeout_s: int
    benchmark_symbol: str
    jobs: list[DecisionJob]
    fill_after: datetime | None = None      # 分钟级：本轮订单最早撮合时刻（日频为 None）
    market: str = "CN"
    allow_short: bool = False
    anon: Anonymizer | None = None          # 回放匿名化


def _format_feedback(s: Session, account_id: int, round_id: int | None) -> list[str]:
    if round_id is None:
        return []
    side_label = SIDE_LABELS
    lines = []
    q = (select(OrderRow).join(DecisionRow, OrderRow.decision_id == DecisionRow.id)
         .where(DecisionRow.round_id == round_id, OrderRow.account_id == account_id))
    for o in s.scalars(q):
        head = f"{side_label.get(o.side, o.side)} {o.symbol} {o.qty} 股"
        if o.status in ("filled", "partially_filled"):
            fills = list(s.scalars(select(FillRow).where(FillRow.order_id == o.id)))
            qty = sum(f.qty for f in fills)
            avg = sum((f.amount for f in fills), Decimal("0")) / qty if qty else Decimal("0")
            tail = f"成交 {qty} 股，均价 {avg:.3f}" + (f"（{o.note}）" if o.note else "")
        elif o.status == "rejected":
            tail = f"被拒绝：{o.note}"
        elif o.status == "expired":
            tail = f"未成交：{o.note}"
        else:
            tail = o.status
        lines.append(f"{head} → {tail}")
    return lines


def _memory(s: Session, account_id: int, k: int, before_round_id: int) -> list[dict]:
    if k <= 0:
        return []
    q = (select(DecisionRow, RoundRow).join(RoundRow, DecisionRow.round_id == RoundRow.id)
         .where(DecisionRow.account_id == account_id, DecisionRow.status == "ok",
                DecisionRow.round_id < before_round_id)
         .order_by(DecisionRow.id.desc()).limit(k))
    out = []
    for dec, rnd in s.execute(q).all():
        nav = s.scalars(select(NavSnapshotRow.nav).where(NavSnapshotRow.account_id == account_id,
                                                         NavSnapshotRow.date == rnd.data_date)).first()
        text = (dec.analysis or "").replace("\n", " ")
        out.append({"date": str(rnd.data_date), "nav": f"{nav:,.0f}" if nav else "-",
                    "analysis": text[:150] + ("…" if len(text) > 150 else "")})
    return list(reversed(out))


def _extra_data(s: Session, track: TrackRow, instruments: dict, ref_bars: dict, data_date: date, trigger: str,
                live: LiveContext | None) -> tuple[str, str]:
    """快照模式按赛道设置附带财报指标与新闻（同一轮所有选手相同）。历史回放不附带新闻。"""
    opts = track.data_options or {}
    fund_text = (extras.safe(extras.fundamentals_table, s, track.market, instruments, data_date)
                 if opts.get("fundamentals") else "")
    news = ""
    if opts.get("news") and trigger != "replay":
        held = set(s.scalars(select(PositionRow.symbol).join(AccountRow, PositionRow.account_id == AccountRow.id)
                             .where(AccountRow.track_id == track.id)))
        moves = [(abs(float(b.close / b.prev_close - 1)), sym) for sym, b in ref_bars.items()
                 if b.prev_close and b.date == data_date]
        movers = [sym for _, sym in sorted(moves, reverse=True)[:8]]
        names = {sym: inst.name for sym, inst in instruments.items()}
        news = extras.safe(extras.news_text, s, extras.news_symbols(held, movers), names,
                           live.now if live else utcnow(), track.market)
    return fund_text, news


def make_anonymizer(svc, track: TrackRow, instruments: dict, anchor: date) -> Anonymizer:
    cal = svc.calendar(anchor)
    return Anonymizer(instruments, cal.between(anchor - timedelta(days=800), anchor + timedelta(days=40)), anchor)


def _universe_list(table: str) -> str:
    """工具模式下 prompt 中只放股票池的代码、名称、类型、最新价与涨跌幅，详细数据由 AI 查询。"""
    lines = table.splitlines()
    head = lines[0].split(",")
    cols = [head.index(c) for c in ("代码", "名称", "类型", "收盘", "涨跌%", "状态")]
    return "\n".join(",".join(row.split(",")[i] for i in cols) for row in lines)


def _account_text(account: dict, open_orders: list[str], alerts: list[str]) -> str:
    """get_positions 工具的返回内容。"""
    lines = [f"净值 {account['nav']}（初始 {account['initial']}，累计收益 {account['total_return']}）",
             f"现金 {account['cash']}（占 {account['cash_pct']}）"]
    if account.get("leverage_text"):
        lines.append(account["leverage_text"])
    lines += [f"⚠️ {a}" for a in alerts]
    if account["positions"]:
        lines.append("持仓：代码,名称,数量（负数为空头）,可卖 / 可平,成本价,最新价,浮动盈亏,占净值")
        lines += [f"{p['symbol']},{p['name']},{p['qty']},{p['sellable']},{p['avg_cost']},{p['close']},"
                  f"{p['pnl_pct']},{p['weight']}" for p in account["positions"]]
    else:
        lines.append("当前空仓。")
    if open_orders:
        lines.append("未成交订单：")
        lines += [f"- {o}" for o in open_orders]
    return "\n".join(lines)


def _margin_alerts(s: Session, acc: AccountRow) -> list[str]:
    """追保与强制平仓提示（给 AI 看）。"""
    alerts = []
    if acc.margin_call_since is not None:
        alerts.append(f"追保：自 {acc.margin_call_since} 收盘起保证金比率低于维持线。"
                      "下一次收盘检查时仍不足，将被强制平仓；"
                      "请降低仓位或杠杆。")
    for row in s.scalars(select(OrderRow).where(OrderRow.account_id == acc.id, OrderRow.status == "pending",
                                                OrderRow.forced.is_(True))):
        alerts.append(f"强制平仓：{orders.describe(row)}，将在 {row.target_date} 开盘执行。")
    return alerts


def _live_bar(symbol: str, d: date, q, factor) -> DailyBar:
    price = q.price
    return DailyBar(symbol=symbol, date=d, open=q.open or price, high=max(q.high, price) if q.high else price,
                    low=min(q.low, price) if q.low else price, close=price, volume=q.volume,
                    amount=q.amount or price * q.volume, prev_close=q.prev_close or None, adj_factor=factor)


def _with_today(history, price) -> object:
    """在截至昨天的前复权日线后面接上今天的最新价，用于计算 5 日 / 20 日涨跌与均线。"""
    import pandas as pd

    row = pd.DataFrame([{"date": None, "open": float(price), "high": float(price), "low": float(price),
                         "close": float(price), "volume": 0, "amount": 0.0}])
    return pd.concat([history, row], ignore_index=True) if not history.empty else row


def _prepare_round(track_id: int, data_date: date, fill_date: date, trigger: str, force: bool,
                   live: LiveContext | None = None) -> RoundPrep | None:
    with get_sessionmaker()() as s:
        track = s.get(TrackRow, track_id)
        existing = list(s.scalars(select(RoundRow).where(RoundRow.track_id == track_id,
                                                         RoundRow.data_date == data_date)))
        for r in existing:
            if r.status == "running":   # 进程中断遗留的轮次
                r.status, r.note, r.finished_at = "failed", "进程中断，轮次未完成", utcnow()
        if live is None and not force and any(r.status == "done" for r in existing):
            s.commit()
            return None

        svc = market_service(s, track.market)
        inst_map = svc.get_instruments(track.symbols)
        instruments = {sym: inst_map[sym] for sym in track.symbols if sym in inst_map}
        bench_name = BENCHMARK_NAMES.get(track.benchmark_symbol, track.benchmark_symbol)
        if live is None:
            ref_bars = svc.latest_bars(track.symbols, data_date)
            histories = {sym: svc.history_frame(sym, data_date, INDICATOR_BARS) for sym in track.symbols}
            bench_hist = svc.history_frame(track.benchmark_symbol, data_date, INDICATOR_BARS)
        else:
            # 盘中：今天的「日线」由实时行情构成；指标用截至昨天的日线接上最新价
            yesterday = data_date - timedelta(days=1)
            last = svc.latest_bars(track.symbols, yesterday)
            ref_bars, histories = {}, {}
            for sym in track.symbols:
                hist = svc.history_frame(sym, yesterday, INDICATOR_BARS)
                q = live.quotes.get(sym)
                if q is not None:
                    factor = last[sym].adj_factor if sym in last else Decimal("1")
                    ref_bars[sym] = _live_bar(sym, data_date, q, factor)
                    histories[sym] = _with_today(hist, q.price)
                else:
                    if sym in last:
                        ref_bars[sym] = last[sym]
                    histories[sym] = hist
            bench_hist = svc.history_frame(track.benchmark_symbol, yesterday, INDICATOR_BARS)
            bq = live.quotes.get(track.benchmark_symbol)
            if bq is not None:
                bench_hist = _with_today(bench_hist, bq.price)
        table = build_market_table(instruments, ref_bars, histories, data_date)
        bench = benchmark_info(bench_name, bench_hist)
        fund_text, news = _extra_data(s, track, instruments, ref_bars, data_date, trigger, live)
        full = "\n\n".join(x for x in (table, fund_text, news) if x)
        snap = MarketSnapshot(data_date=data_date, fill_date=fill_date, table=table,
                              hash=snapshot_hash(full, bench, data_date), benchmark=bench, ref_bars=ref_bars,
                              instruments=instruments, names={s_: i.name for s_, i in instruments.items()})
        previous = s.scalars(select(RoundRow).where(RoundRow.track_id == track_id, RoundRow.status == "done")
                             .order_by(RoundRow.id.desc()).limit(1)).first()
        rnd = RoundRow(track_id=track_id, data_date=data_date, fill_date=fill_date, trigger=trigger,
                       status="running", snapshot_hash=snap.hash, market_snapshot=full,
                       snapshot_at=live.now if live else None)
        s.add(rnd)
        s.flush()

        params = RuleParams(**track.rule_params)
        market_name = get_market(track.market).name
        if live is None:
            timing_text = (f"{data_date} 收盘后（{market_name}当地时间）决策；"
                           f"订单在下一个交易日（{fill_date}）开盘后生效：市价单以开盘价成交，限价、止损、止盈单在价格触及时成交，当日有效的订单收盘后失效。")
            now_text = f"{data_date} 收盘后（下一交易日 {fill_date}）"
            table_title = f"{data_date} 收盘"
        else:
            local = to_market_time(live.now, track.market)
            timing_text = (f"{local:%Y-%m-%d %H:%M} 盘中决策（{market_name}当地时间，"
                           f"每 {track.interval_min} 分钟一轮）；本轮所有选手决策完成后，订单从下一分钟开始生效："
                           "市价单按下一分钟开盘价成交，"
                           "限价、止损、止盈单在价格触及时成交，当日有效的订单收盘后失效。")
            now_text = f"{local:%Y-%m-%d %H:%M}（{market_name}盘中）"
            table_title = f"{local:%H:%M} 实时行情，涨跌% 为相对昨收"
        jobs: list[DecisionJob] = []
        tool_mode = track.decision_mode == "tools"
        anon = make_anonymizer(svc, track, instruments, data_date) if track.anonymize else None
        for acc in s.scalars(select(AccountRow).where(AccountRow.track_id == track_id, AccountRow.status == "active")):
            agent = s.get(AgentRow, acc.agent_id)
            state = load_state(s, acc)
            job = DecisionJob(account_id=acc.id, agent_name=agent.name, kind=agent.kind, state=state,
                              baseline_type=agent.baseline_type, quant_strategy_id=agent.quant_strategy_id,
                              quant_portfolio_id=agent.quant_portfolio_id)
            if agent.kind == "quant" and (agent.params or {}).get("ai_review"):
                # 组合 + AI 复核账户：复核模型来自选手设置
                job.params = dict(agent.params)
                if agent.provider_id:
                    job.provider, job.model, _, _ = strategy.llm_config(s, agent)
            if agent.kind == "quant" and (agent.params or {}).get("watch_tilt"):
                job.params = {"watch_tilt": True}
            if agent.kind == "llm":
                job.provider, job.model, job.params, job.json_mode = strategy.llm_config(s, agent)
                strategy_ctx, job.strategy_version = strategy.decision_context(s, acc.id)
                tpl = (s.get(PromptTemplateRow, agent.prompt_template_id) if agent.prompt_template_id else
                       s.scalars(select(PromptTemplateRow).where(PromptTemplateRow.name == "default")
                                 .order_by(PromptTemplateRow.version.desc())).first())
                ctx = {
                    "market_name": get_market(track.market).name, "currency": track.currency,
                    "data_date": str(data_date), "fill_date": str(fill_date), "benchmark": bench,
                    "timing_text": timing_text, "now_text": now_text, "table_title": table_title,
                    "open_orders": [orders.describe(o) for o in orders.open_orders(s, acc.id)],
                    "rules": {**rules_summary(params), "lines": get_rules(track.market).describe(params)},
                    "persona": agent.persona or "", "output_mode": track.output_mode, "market": track.market,
                    "example_symbol": track.symbols[0] if track.symbols else "",
                    "example_qty": instruments[track.symbols[0]].lot_size if track.symbols and track.symbols[0]
                    in instruments else 100,
                    "account": account_context(state, track.initial_cash, snap, params),
                    "margin_alerts": _margin_alerts(s, acc),
                    "feedback": _format_feedback(s, acc.id, previous.id if previous else None),
                    "memory": _memory(s, acc.id, agent.memory_rounds, rnd.id),
                    "market_table": table,
                    "fundamentals_text": fund_text,
                    "news_text": news,
                    "strategy": strategy_ctx,
                    "tool_mode": tool_mode,
                    "max_tool_calls": track.max_tool_calls,
                    "universe_list": _universe_list(table) if tool_mode else "",
                }
                job.system, job.user = render(tpl.system_tpl, tpl.user_tpl, ctx)
                if strategy_ctx and "strategy" not in tpl.system_tpl:
                    # 旧版模板不含策略笔记，追加在系统提示末尾
                    job.system += strategy.STRATEGY_SECTION.format(**strategy_ctx)
                if anon is not None:
                    job.system, job.user = anon.text(job.system), anon.text(job.user)
                job.template_name, job.template_version = tpl.name, tpl.version
                if tool_mode:
                    job.tool_ctx = tools.ToolContext(
                        track_id=track_id, account_id=acc.id, market=track.market, output_mode=track.output_mode,
                        data_date=data_date, until=live.now if live else utcnow(), live=live is not None,
                        replay=trigger == "replay", snapshot=snap, max_calls=track.max_tool_calls, anon=anon,
                        account_text=_account_text(ctx["account"], ctx["open_orders"], ctx["margin_alerts"]))
            jobs.append(job)
        s.commit()
        return RoundPrep(round_id=rnd.id, track_id=track_id, snapshot=snap, output_mode=track.output_mode,
                         timeout_s=track.decision_timeout_s, benchmark_symbol=track.benchmark_symbol, jobs=jobs,
                         market=track.market, allow_short=params.allow_short, anon=anon)


async def _run_job(job: DecisionJob, prep: RoundPrep, sem: asyncio.Semaphore) -> DecisionOutcome | BaselineDecision:
    if job.kind == "baseline":
        return decide_baseline(job.baseline_type, job.state, prep.snapshot, prep.benchmark_symbol,
                               seed=f"{prep.track_id}-{job.account_id}-{prep.snapshot.data_date}")
    if job.kind == "quant":
        from app.quant.live import decide_portfolio, decide_quant

        try:
            if job.quant_portfolio_id and job.params.get("watch_tilt"):
                from app.watch.tilt import decide_tilted

                return await decide_tilted(job.quant_portfolio_id, job.account_id, job.state, prep.snapshot)
            if job.quant_portfolio_id and job.params.get("ai_review"):
                from app.quant.review import decide_reviewed

                llm_params = {k: v for k, v in job.params.items() if k != "ai_review"}
                return await decide_reviewed(job.quant_portfolio_id, job.account_id, job.state, prep.snapshot,
                                             job.provider, job.model, llm_params)
            if job.quant_portfolio_id:
                return await asyncio.to_thread(decide_portfolio, job.quant_portfolio_id, job.account_id, job.state,
                                               prep.snapshot)
            return await asyncio.to_thread(decide_quant, job.quant_strategy_id, job.account_id, job.state,
                                           prep.snapshot)
        except Exception as e:
            logger.exception("量化选手 %s 计算失败", job.agent_name)
            return BaselineDecision(None, f"量化策略计算失败：{type(e).__name__}: {e}")
    async with sem:
        try:
            client = create_client(job.provider)
            if job.tool_ctx is not None:
                ctx = job.tool_ctx

                async def execute(name: str, args: dict) -> str:
                    return await asyncio.to_thread(tools.execute, ctx, name, args)

                def parse(text: str):
                    return parse_decision(prep.anon.restore(text) if prep.anon else text, prep.output_mode,
                                          prep.market)

                return await run_llm_tools(
                    client, model=job.model, system=job.system, user=job.user, params=job.params,
                    timeout_s=prep.timeout_s, tools=tools.tool_specs(prep.output_mode, prep.allow_short),
                    execute=execute, parse=parse, max_calls=ctx.max_calls)
            return await run_llm_decision(
                client, model=job.model, system=job.system, user=job.user, params=job.params,
                json_mode=job.json_mode, output_mode=prep.output_mode, timeout_s=prep.timeout_s,
                market=prep.market, restore=prep.anon.restore if prep.anon else None)
        except Exception as e:  # 单个选手异常不能影响整轮
            logger.exception("选手 %s 决策异常", job.agent_name)
            return DecisionOutcome(status="error", error=f"{type(e).__name__}: {e}")


def _persist_round(prep: RoundPrep, results: list[DecisionOutcome | BaselineDecision]) -> str:
    snap = prep.snapshot
    with get_sessionmaker()() as s:
        track = s.get(TrackRow, prep.track_id)
        params = RuleParams(**track.rule_params)
        valid = orders_total = 0
        for job, res in zip(prep.jobs, results, strict=True):
            acc = s.get(AccountRow, job.account_id)
            state = load_state(s, acc)
            used_params = params
            intents: list[OrderIntent] = []
            notes: list[str] = []
            if isinstance(res, BaselineDecision):
                dec = DecisionRow(round_id=prep.round_id, account_id=acc.id, status="baseline",
                                  analysis=res.analysis, parsed={"targets": {k: str(v) for k, v in
                                                                             (res.targets or {}).items()},
                                                                 **res.extra})
                if res.param_overrides:
                    used_params = params.model_copy(update=res.param_overrides)
                if res.targets is not None:
                    conv = targets_to_intents(res.targets, state, snap.instruments, snap.ref_bars, used_params)
                    intents, notes = conv.intents, conv.notes
                valid += 1
            else:
                provider_cost = calc_cost(job.provider, res.tokens_in, res.tokens_out) if job.provider else 0
                dec = DecisionRow(
                    round_id=prep.round_id, account_id=acc.id, status=res.status, model=res.model or job.model,
                    template_name=job.template_name, template_version=job.template_version,
                    prompt_system=job.system, prompt_user=job.user, raw_response=res.raw, reasoning=res.reasoning,
                    parsed=res.parsed.raw if res.parsed else None, analysis=res.parsed.analysis if res.parsed else None,
                    error=res.error, attempts=res.attempts, tokens_in=res.tokens_in, tokens_out=res.tokens_out,
                    cost=provider_cost, latency_ms=res.latency_ms, strategy_version=job.strategy_version,
                    tool_trace=res.trace,
                )
                if res.status == "ok" and res.parsed:
                    valid += 1
                    notes = list(res.parsed.notes)
                    if prep.output_mode == "targets":
                        conv = targets_to_intents(res.parsed.targets or {}, state, snap.instruments, snap.ref_bars,
                                                  params, res.parsed.reasons)
                        intents = conv.intents
                        notes += conv.notes
                    else:
                        intents = res.parsed.intents
            s.add(dec)
            s.flush()
            parsed = res.parsed if not isinstance(res, BaselineDecision) else None
            new_targets = parsed.targets if parsed else getattr(res, "targets", None)
            if prep.output_mode == "targets" and new_targets is not None:
                # 目标仓位以最新一轮为准：撤销上一轮还没成交的买卖单（止损止盈单保留），避免重复下单。
                # 本轮订单是按「已成交的持仓」计算的，撤单不影响其正确性
                n = orders.cancel_unfilled(s, acc.id, "被新一轮目标仓位替换")
                if n:
                    notes.append(f"已撤销上一轮未成交的 {n} 笔订单")
            if parsed and parsed.cancels:
                n = orders.cancel(s, acc.id, parsed.cancels, "AI 撤单")
                notes.append(f"已撤销 {n} 笔订单")
            check = precheck(intents, state, snap.instruments, snap.ref_bars, used_params, snap.fill_date)
            if parsed and parsed.risk_levels:
                _apply_risk_levels(s, acc.id, dec.id, state, parsed.risk_levels, check.accepted, snap, notes,
                                   parsed.targets or {})
            for it in check.accepted:
                s.add(orders.new_order(acc.id, dec.id, it, snap.data_date, snap.fill_date,
                                       active_from=prep.fill_after))
            for it, why in check.rejected:
                s.add(orders.new_order(acc.id, dec.id, it, snap.data_date, None, status="rejected", note=why))
            orders_total += len(check.accepted)
            dec.notes = notes
        rnd = s.get(RoundRow, prep.round_id)
        rnd.status, rnd.finished_at = "done", utcnow()
        when = (f"{to_market_time(rnd.snapshot_at, track.market):%m-%d %H:%M}" if rnd.snapshot_at
                else f"{snap.data_date}")
        msg = f"{when} 决策完成：{valid}/{len(prep.jobs)} 个选手有效，提交 {orders_total} 笔订单"
        rnd.note = msg
        _event(s, prep.track_id, snap.data_date, "round_done", msg)
        s.commit()
        return msg


def _apply_risk_levels(s: Session, account_id: int, decision_id: int, state: AccountState,
                       levels: dict, accepted: list[OrderIntent], snap: MarketSnapshot, notes: list[str],
                       targets: dict) -> None:
    """目标仓位模式下 AI 给出的止损 / 止盈：替换该标的原有条件单，覆盖现有持仓，并附加到本轮开仓单上。

    目标权重为负（空头）时方向相反：止损高于现价、止盈低于现价，触发后平空。
    """
    for symbol, (stop, tp) in levels.items():
        bar = snap.ref_bars.get(symbol)
        if symbol not in snap.instruments or bar is None:
            continue
        short = targets.get(symbol, Decimal("0")) < 0
        if short:
            bad = (stop is not None and stop <= bar.close) or (tp is not None and tp >= bar.close)
        else:
            bad = (stop is not None and stop >= bar.close) or (tp is not None and tp <= bar.close)
        if bad:
            notes.append(f"{symbol} 的止损/止盈价与当前价 {bar.close} 不匹配，已忽略")
            continue
        orders.cancel_conditionals(s, account_id, symbol, "被新的止损止盈设置替换")
        pos = state.position(symbol)
        held = pos.qty if pos else 0
        close_side, open_side = ("cover", "short") if short else ("sell", "buy")
        closing = sum(i.qty for i in accepted if i.symbol == symbol and i.side == close_side)
        keep = (-held if short else held) - closing
        if keep > 0:
            orders.protect(s, account_id, symbol, keep, stop, tp, snap.data_date, decision_id,
                           "AI 设置的止损止盈", active_from=None, side=close_side)
        for it in accepted:
            if it.symbol == symbol and it.side == open_side:
                it.attach_stop, it.attach_take_profit = stop, tp


async def _decision_round(track_id: int, data_date: date, fill_date: date, trigger: str, force: bool,
                          live: LiveContext | None = None) -> str:
    started = utcnow()
    prep = await asyncio.to_thread(_prepare_round, track_id, data_date, fill_date, trigger, force, live)
    if prep is None:
        return f"{data_date} 的决策已完成"
    logger.info("赛道 %s：%s 决策轮次开始，%d 个选手", track_id, data_date, len(prep.jobs))
    sem = asyncio.Semaphore(LLM_CONCURRENCY)
    results = await asyncio.gather(*(_run_job(job, prep, sem) for job in prep.jobs))
    if live is not None:
        # 全部选手完成后，订单从下一分钟开始生效：同一轮所有选手使用同一根分钟 K 线成交。
        # 以本轮时刻加上决策实际耗时计算（不直接取系统时钟），实时运行时与当前时间一致，测试中也不受真实日期影响
        elapsed = utcnow() - started
        prep.fill_after = (live.now + elapsed + timedelta(minutes=1)).replace(second=0, microsecond=0)
    return await asyncio.to_thread(_persist_round, prep, list(results))


async def run_live_round(track_id: int, quotes: dict, now: datetime, trigger: str) -> str:
    """分钟级决策轮次（由盘中引擎调用）。"""
    today = to_market_time(now, (await asyncio.to_thread(_track_market, track_id))).date()
    async with _lock(track_id):
        try:
            return await _decision_round(track_id, today, today, trigger, True, LiveContext(now, quotes))
        except Exception as e:
            logger.exception("赛道 %s 盘中决策失败", track_id)
            await asyncio.to_thread(_record_failure, track_id, f"盘中决策失败：{type(e).__name__}: {e}")
            return f"执行失败：{e}"


def _track_market(track_id: int) -> str:
    with get_sessionmaker()() as s:
        return s.get(TrackRow, track_id).market


# ====================================================================== 入口


async def run_track_cycle(track_id: int, trigger: str = "schedule", now: datetime | None = None,
                          force_round: bool = False) -> str:
    """执行一次赛道日终流程，返回结果说明。"""
    now = now or utcnow()
    async with _lock(track_id):
        try:
            plan = await asyncio.to_thread(_plan, track_id, now)
            if plan.latest is None:
                return plan.message
            for d in plan.settle_days:
                logger.info("赛道 %s：结算 %s", track_id, d)
                ok = await asyncio.to_thread(_settle, track_id, d, d == plan.latest)
                if not ok:
                    return f"{d} 行情尚未就绪，稍后重试"
            if plan.reflect:
                # 复盘只用截至最新结算日的数据，生成的新策略从本轮决策开始生效
                for line in await strategy.run_due_reflections(track_id, plan.latest):
                    logger.info("赛道 %s：%s", track_id, line)
            if plan.decision_date is None:
                if plan.intraday:
                    return "已记录收盘净值（分钟级赛道在盘中决策）"
                return "已完成结算（赛道暂停或已结束，不发起决策）"
            return await _decision_round(track_id, plan.decision_date, plan.fill_date, trigger, force_round)
        except Exception as e:
            logger.exception("赛道 %s 日终流程失败", track_id)
            await asyncio.to_thread(_record_failure, track_id, f"{type(e).__name__}: {e}")
            return f"执行失败：{e}"


def _record_failure(track_id: int, message: str) -> None:
    with get_sessionmaker()() as s:
        _event(s, track_id, None, "cycle_error", message, "error")
        s.commit()


# ====================================================================== 历史回放


@dataclass
class ReplayPlan:
    days: list[date] = field(default_factory=list)
    market: str = "CN"
    done: int = 0
    message: str = ""


def replay_pending(track: TrackRow) -> bool:
    """回放赛道是否还没追上 replay_until。"""
    if track.mode != "replay" or track.replay_until is None or track.start_date is None:
        return False
    return (track.last_settled_date or track.start_date) < track.replay_until


def _replay_plan(track_id: int) -> ReplayPlan:
    with get_sessionmaker()() as s:
        track = s.get(TrackRow, track_id)
        if track is None or track.mode != "replay" or track.status != "running":
            return ReplayPlan(message="赛道不是运行中的回放赛道")
        svc = market_service(s, track.market)
        cal = svc.calendar(track.replay_until)
        days = cal.between(track.start_date, track.replay_until)
        # 先一次性拉取整个回放区间的行情，之后逐日运行时不再请求数据源
        insts = svc.get_instruments([*track.symbols, track.benchmark_symbol])
        logger.info("赛道 %s：回放预取行情 %s ~ %s", track.id, track.start_date, track.replay_until)
        svc.update_bars(list(insts.values()), until=track.replay_until,
                        history_start=track.start_date - timedelta(days=HISTORY_LOOKBACK_DAYS))
        s.commit()
        done = sum(1 for d in days if track.last_settled_date and d <= track.last_settled_date)
        return ReplayPlan(days=days, done=done, market=track.market)


def _track_status(track_id: int) -> str | None:
    with get_sessionmaker()() as s:
        track = s.get(TrackRow, track_id)
        return track.status if track else None


async def run_replay(track_id: int, progress: Callable[[str], None] | None = None) -> str:
    """历史回放：从开始日期逐个交易日执行日终流程（结算 → 净值 → 决策），直到 replay_until。

    模拟时钟设为每个交易日收盘后数据就绪的时间（市场本地时间），因此所有数据只用到当日收盘为止，与实时运行的逻辑完全相同。
    可以中断：再次调用会从上次结算的位置继续。
    """
    if track_id in _replaying:
        return "回放已在进行中"
    _replaying.add(track_id)
    try:
        return await _run_replay(track_id, progress or (lambda _m: None))
    finally:
        _replaying.discard(track_id)


async def _run_replay(track_id: int, report: Callable[[str], None]) -> str:
    try:
        plan = await asyncio.to_thread(_replay_plan, track_id)
    except Exception as e:
        logger.exception("赛道 %s 回放准备失败", track_id)
        await asyncio.to_thread(_record_failure, track_id, f"回放准备失败：{e}")
        return f"回放准备失败：{e}"
    if not plan.days:
        return plan.message or "没有需要回放的交易日"
    spec = get_market(plan.market)
    # 模拟时钟设在该市场收盘后数据就绪之后，保证当天被结算和决策
    sim_time = (datetime.combine(date(2000, 1, 1), spec.data_ready) + timedelta(minutes=5)).time()
    total = len(plan.days)
    msg = ""
    for i, d in enumerate(plan.days, 1):
        if await asyncio.to_thread(_track_status, track_id) != "running":
            return f"回放已停止（进度 {i - 1}/{total}）"
        report(f"历史回放中：{d}（{i}/{total}）")
        sim_now = datetime.combine(d, sim_time, tzinfo=spec.zone).astimezone(UTC)
        msg = await run_track_cycle(track_id, trigger="replay", now=sim_now)
        if msg.startswith("执行失败") or "尚未就绪" in msg:
            return f"回放在 {d} 中断：{msg}"
    return f"历史回放完成（{total} 个交易日），之后按实时模式继续运行。最后一轮：{msg}"


async def run_market_tracks(market: str, trigger: str = "schedule") -> None:
    def _tracks() -> list[tuple[int, bool]]:
        with get_sessionmaker()() as s:
            rows = s.scalars(select(TrackRow).where(TrackRow.market == market,
                                                    TrackRow.status.in_(["running", "paused"])))
            return [(t.id, t.status == "running" and replay_pending(t)) for t in rows]

    for track_id, replaying in await asyncio.to_thread(_tracks):
        if track_id in _replaying:
            continue
        # 回放未完成的赛道继续回放（例如回放中途重启），不能按当前日期直接跳到最新一天
        msg = await (run_replay(track_id) if replaying else run_track_cycle(track_id, trigger))
        logger.info("赛道 %s：%s", track_id, msg)
