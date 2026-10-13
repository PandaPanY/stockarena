"""赛道生命周期：创建、报名、启动（冻结股票池与规则）、暂停、恢复、结束。"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.arena import strategy
from app.arena.deps import market_service, universe_resolver
from app.core.timeutils import to_market_time
from app.market.calendar import TradingCalendar
from app.market.markets import get_market
from app.market.universe import BENCHMARK_NAMES, index_instrument
from app.models import AccountRow, AgentRow, CashLedgerRow, RuleSetRow, TrackRow
from app.trading.params import RuleParams

DEFAULT_BENCHMARKS = {"CN": "000300.SH", "HK": "HSI.HK", "US": "NDX.US"}


class ArenaError(ValueError):
    """业务规则错误，message 直接展示给用户。"""


def latest_complete_date(cal: TradingCalendar, now_local: datetime) -> date:
    """当前时刻已经收盘、数据可用的最近交易日（now_local 为该市场本地时间）。"""
    today = now_local.date()
    if cal.is_trading_day(today) and now_local.time() >= get_market(cal.market).data_ready:
        return today
    return cal.previous(today) if cal.is_trading_day(today) else cal.latest_on_or_before(today)


def first_decision_date(cal: TradingCalendar, now_local: datetime) -> date:
    """启动赛道时的首轮决策数据日期：开盘前启动用上一交易日数据（当天开盘成交）；盘中启动则等到当天收盘后。"""
    today = now_local.date()
    if not cal.is_trading_day(today):
        return cal.latest_on_or_before(today)
    if now_local.time() < get_market(cal.market).pre_open:
        return cal.previous(today)
    return today


def create_track(session: Session, user_id: int, *, name: str, universe_name: str, rule_set_id: int,
                 initial_cash: Decimal, output_mode: str = "targets", decision_timeout_s: int = 900,
                 end_date: date | None = None, description: str = "", mode: str = "live",
                 replay_start: date | None = None, frequency: str = "daily", interval_min: int = 15,
                 event_move_pct: Decimal | None = None, event_max_per_day: int = 5, decision_mode: str = "snapshot",
                 max_tool_calls: int = 10, data_options: dict | None = None, anonymize: bool = False) -> TrackRow:
    rule_set = session.get(RuleSetRow, rule_set_id)
    if rule_set is None:
        raise ArenaError("规则集不存在")
    resolver = universe_resolver()
    spec = resolver.specs.get(universe_name)
    if spec is None:
        raise ArenaError(f"股票池不存在：{universe_name}")
    if spec.market != rule_set.market:
        raise ArenaError("股票池与规则集的市场不一致")
    if output_mode not in ("targets", "orders"):
        raise ArenaError("决策模式只能是 targets 或 orders")
    if initial_cash <= 0:
        raise ArenaError("初始本金必须大于 0")
    if mode not in ("live", "replay"):
        raise ArenaError("运行模式只能是 live 或 replay")
    if mode == "replay" and replay_start is None:
        raise ArenaError("历史回放需要指定开始日期")
    if decision_mode not in ("snapshot", "tools"):
        raise ArenaError("决策方式只能是 snapshot（快照）或 tools（工具调用）")
    if anonymize and mode != "replay":
        raise ArenaError("代码匿名化只用于历史回放（实时模拟不存在前视偏差）")
    if not 1 <= max_tool_calls <= 30:
        raise ArenaError("单轮工具调用次数上限应在 1～30 之间")
    options = {k: bool(v) for k, v in (data_options or {}).items() if k in ("news", "fundamentals")}
    if frequency not in ("daily", "intraday"):
        raise ArenaError("决策频率只能是 daily 或 intraday")
    if frequency == "intraday":
        if mode == "replay":
            raise ArenaError("分钟级赛道只支持实时模式（港股、美股没有可用的分钟级历史数据）")
        if interval_min not in (5, 15, 30, 60):
            raise ArenaError("分钟级决策间隔只能是 5、15、30 或 60 分钟")
        if event_move_pct is not None and not (Decimal("0.005") <= event_move_pct <= Decimal("0.2")):
            raise ArenaError("异动触发阈值应在 0.5% 到 20% 之间")
    track = TrackRow(
        user_id=user_id, name=name, market=spec.market, currency=get_market(spec.market).currency,
        universe_name=universe_name, symbols=[], benchmark_symbol=spec.benchmark or DEFAULT_BENCHMARKS[spec.market],
        rule_set_id=rule_set_id, rule_params={}, initial_cash=initial_cash, output_mode=output_mode,
        decision_timeout_s=decision_timeout_s, end_date=end_date, status="draft", description=description,
        mode=mode, start_date=replay_start if mode == "replay" else None,
        frequency=frequency, interval_min=interval_min, event_move_pct=event_move_pct,
        event_max_per_day=event_max_per_day, decision_mode=decision_mode, max_tool_calls=max_tool_calls,
        data_options=options, anonymize=anonymize,
    )
    session.add(track)
    session.flush()
    return track


def clone_track(session: Session, track: TrackRow) -> TrackRow:
    """复制赛道设置与选手为新的草稿；回放赛道沿用原来的回放开始日期。"""
    n = 1 + sum(1 for _ in session.scalars(select(TrackRow.id).where(TrackRow.name.like(f"{track.name}（副本%"))))
    new = create_track(
        session, track.user_id, name=f"{track.name}（副本 {n}）", universe_name=track.universe_name,
        rule_set_id=track.rule_set_id, initial_cash=track.initial_cash, output_mode=track.output_mode,
        decision_timeout_s=track.decision_timeout_s, end_date=track.end_date, description=track.description,
        mode=track.mode, replay_start=track.start_date if track.mode == "replay" else None,
        frequency=track.frequency, interval_min=track.interval_min, event_move_pct=track.event_move_pct,
        event_max_per_day=track.event_max_per_day, decision_mode=track.decision_mode,
        max_tool_calls=track.max_tool_calls, data_options=dict(track.data_options or {}), anonymize=track.anonymize,
    )
    for acc in session.scalars(select(AccountRow).where(AccountRow.track_id == track.id).order_by(AccountRow.id)):
        agent = session.get(AgentRow, acc.agent_id)
        if agent is not None and not agent.archived:
            enroll(session, new, agent.id)
    return new


def enroll(session: Session, track: TrackRow, agent_id: int) -> AccountRow:
    if track.status not in ("draft",):
        raise ArenaError("赛道启动后不能再添加选手（保证公平）。如需加入新选手，请复制为新赛道")
    agent = session.get(AgentRow, agent_id)
    # 只能加入自己的选手；基线选手（买入持有指数等，不调用 AI）所有用户共用
    if agent is None or agent.archived or (agent.user_id != track.user_id and agent.kind != "baseline"):
        raise ArenaError("选手不存在")
    if session.scalars(select(AccountRow).where(AccountRow.track_id == track.id,
                                                AccountRow.agent_id == agent_id)).first():
        raise ArenaError("该选手已在赛道中")
    account = AccountRow(track_id=track.id, agent_id=agent_id, cash=track.initial_cash, status="active")
    session.add(account)
    session.flush()
    return account


def withdraw(session: Session, track: TrackRow, account_id: int) -> None:
    if track.status != "draft":
        raise ArenaError("赛道启动后不能移除选手")
    account = session.get(AccountRow, account_id)
    if account is None or account.track_id != track.id:
        raise ArenaError("账户不存在")
    session.delete(account)


def start_track(session: Session, track: TrackRow, now: datetime) -> None:
    if track.status != "draft":
        raise ArenaError("只有草稿状态的赛道可以启动")
    accounts = list(session.scalars(select(AccountRow).where(AccountRow.track_id == track.id)))
    if not accounts:
        raise ArenaError("请先添加至少一个选手")

    rule_set = session.get(RuleSetRow, track.rule_set_id)
    track.rule_params = RuleParams(**rule_set.params).model_dump(mode="json")   # 冻结规则参数

    svc = market_service(session, track.market)
    instruments = universe_resolver().resolve(track.universe_name)
    if not instruments:
        raise ArenaError("股票池为空")
    svc.upsert_instruments(instruments)
    svc.upsert_instruments([index_instrument(track.benchmark_symbol,
                                             BENCHMARK_NAMES.get(track.benchmark_symbol, track.benchmark_symbol))])
    track.symbols = [i.symbol for i in instruments]                              # 冻结股票池

    now_local = to_market_time(now, track.market)
    cal = svc.calendar(now_local.date())
    if track.mode == "replay":
        latest = latest_complete_date(cal, now_local)
        start = track.start_date
        if start is None or not cal.covers(start):
            raise ArenaError("回放开始日期超出交易日历范围")
        if not cal.is_trading_day(start):
            start = cal.next(start)
        if start >= latest:
            raise ArenaError(f"回放开始日期必须早于最近一个已收盘的交易日（{latest}）")
        track.start_date = start
        # 回放到启动时最近的已收盘交易日；之后（未设结束日期时）转为实时继续运行
        track.replay_until = min(latest, track.end_date) if track.end_date else latest
    elif track.frequency == "intraday":
        # 分钟级：今天还没收盘就从今天开始，否则从下一个交易日开始
        today = now_local.date()
        before_close = (cal.covers(today) and cal.is_trading_day(today)
                        and now_local.time() < get_market(track.market).data_ready)
        track.start_date = today if before_close else cal.next(today)
    else:
        track.start_date = first_decision_date(cal, now_local)
    track.status = "running"
    for acc in accounts:
        acc.cash = track.initial_cash
        session.add(CashLedgerRow(account_id=acc.id, trade_date=track.start_date, type="deposit",
                                  amount=track.initial_cash, balance_after=track.initial_cash, note="初始本金"))
    strategy.init_versions(session, track)


def set_status(track: TrackRow, action: str) -> None:
    transitions = {
        "pause": ({"running"}, "paused"),
        "resume": ({"paused"}, "running"),
        "stop": ({"running", "paused"}, "finished"),
    }
    allowed, target = transitions[action]
    if track.status not in allowed:
        raise ArenaError(f"当前状态（{track.status}）不能执行该操作")
    track.status = target
