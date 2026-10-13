"""港股 / 美股赛道端到端：伪造数据源，验证交易日历、每手股数、提示词规则、结算货币与调度。"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from app.arena import cycle, deps
from app.arena.accounts import reconcile
from app.arena.lifecycle import create_track, enroll, start_track
from app.db.session import get_sessionmaker
from app.market.types import DailyBar
from app.market.universe import load_universe_specs
from app.models import AccountRow, AgentRow, DecisionRow, FillRow, PositionRow, RuleSetRow, TrackRow
from app.scheduler import get_scheduler, register_jobs, shutdown_scheduler
from tests.arena.conftest import FakeLLM


class FakeIntlProvider:
    def __init__(self, market: str, holidays: set[date], lots: dict[str, int]):
        self.market = market
        self.holidays = holidays
        self.lots = lots

    def trading_days(self):
        start = date(2026, 1, 1)
        return [start + timedelta(i) for i in range(400)
                if (start + timedelta(i)).weekday() < 5 and start + timedelta(i) not in self.holidays]

    def daily_bars(self, inst, start, end):
        base = Decimal("400") if inst.type == "stock" else Decimal("25")
        out, prev = [], None
        for i, d in enumerate(self.trading_days()):
            px = base + Decimal(i % 5)
            if start <= d <= end:
                out.append(DailyBar(inst.symbol, d, px, px + 2, px - 2, px, 10**8, px * 10**8, prev))
            prev = px
        return out

    def corporate_actions(self, inst):
        return []

    def securities(self):
        return {code: {"lot": lot, "stamp_duty": not code.startswith(("02800", "02828", "03033", "03188", "02840")),
                       "category": "Equity", "name_en": code} for code, lot in self.lots.items()}


def hk_time(y, m, d, hh):
    return datetime(y, m, d, hh, tzinfo=UTC) - timedelta(hours=8)


def _start(universe: str, ruleset: str, cash: str, now: datetime) -> int:
    with get_sessionmaker()() as s:
        rs = s.scalars(select(RuleSetRow).where(RuleSetRow.name == ruleset)).one()
        t = create_track(s, 1, name=universe, universe_name=universe, rule_set_id=rs.id, initial_cash=Decimal(cash))
        for a in s.scalars(select(AgentRow)):
            if a.kind == "llm" or a.baseline_type == "buy_hold_index":
                enroll(s, t, a.id)
        start_track(s, t, now)
        s.commit()
        return t.id


async def test_hk_track_end_to_end(arena, settings, monkeypatch):
    specs = load_universe_specs(settings.config_dir)
    codes = [m["symbol"].split(".")[0] for n in ("hk_core", "hk_etf_core") for m in specs[n].members]
    lots = {c: (500 if c == "02800" else 100) for c in codes}
    provider = FakeIntlProvider("HK", {date(2026, 10, 1)}, lots)
    monkeypatch.setattr(deps, "hk_provider", lambda: provider)
    llm = FakeLLM(json.dumps({"analysis": "港股配置", "targets": [{"symbol": "700", "weight": 0.2},
                                                               {"symbol": "2800", "weight": 0.3}]}))
    monkeypatch.setattr(cycle, "create_client", lambda cfg: llm)

    # 10/2 港股正常交易（A股休市），16:45 港股收盘后启动
    track_id = _start("hk_core_etf", "hk_long_only", "1000000", hk_time(2026, 10, 2, 17))
    with get_sessionmaker()() as s:
        t = s.get(TrackRow, track_id)
        assert (t.market, t.currency, t.start_date, t.benchmark_symbol) == ("HK", "HKD", date(2026, 10, 2), "HSI.HK")
        assert len(t.symbols) == 35
    assert "决策完成" in await cycle.run_track_cycle(track_id, now=hk_time(2026, 10, 2, 17))
    with get_sessionmaker()() as s:
        dec = s.scalars(select(DecisionRow).where(DecisionRow.status == "ok")).one()
        assert "每手" in dec.prompt_user and "00700.HK" in dec.prompt_user
        assert "港股没有涨跌停" in dec.prompt_system and "HKD" in dec.prompt_system

    # 下一个港股交易日 10/5 结算：腾讯整手买入、盈富基金按 500 股一手
    assert "决策完成" in await cycle.run_track_cycle(track_id, now=hk_time(2026, 10, 5, 17))
    with get_sessionmaker()() as s:
        acc = s.scalars(select(AccountRow).join(AgentRow).where(AgentRow.kind == "llm")).one()
        pos = {p.symbol: p.qty for p in s.scalars(select(PositionRow).where(PositionRow.account_id == acc.id))}
        assert pos["00700.HK"] % 100 == 0 and pos["02800.HK"] % 500 == 0
        fills = {f.symbol: f for f in s.scalars(select(FillRow).where(FillRow.account_id == acc.id))}
        assert "stamp_duty" in fills["00700.HK"].fees and "stamp_duty" not in fills["02800.HK"].fees
        for a in s.scalars(select(AccountRow)):
            assert reconcile(s, a) == 0


async def test_us_track_replay(arena, monkeypatch):
    provider = FakeIntlProvider("US", set(), {})
    monkeypatch.setattr(deps, "us_provider", lambda: provider)
    llm = FakeLLM(json.dumps({"analysis": "美股", "targets": [{"symbol": "aapl", "weight": 0.2}]}))
    monkeypatch.setattr(cycle, "create_client", lambda cfg: llm)
    with get_sessionmaker()() as s:
        rs = s.scalars(select(RuleSetRow).where(RuleSetRow.name == "us_long_only")).one()
        t = create_track(s, 1, name="美股回放", universe_name="us_core_etf", rule_set_id=rs.id,
                         initial_cash=Decimal("100000"), mode="replay", replay_start=date(2026, 9, 21))
        for a in s.scalars(select(AgentRow).where(AgentRow.kind == "llm")):
            enroll(s, t, a.id)
        # 美东时间 10/2 10:00（UTC 14:00）启动：最近收盘日为 10/1
        start_track(s, t, datetime(2026, 10, 2, 14, tzinfo=UTC))
        s.commit()
        track_id = t.id
        assert (t.currency, t.replay_until) == ("USD", date(2026, 10, 1))
    msg = await cycle.run_replay(track_id)
    assert "回放完成（9 个交易日）" in msg and "2026-10-01 决策完成" in msg
    with get_sessionmaker()() as s:
        assert s.get(TrackRow, track_id).last_settled_date == date(2026, 10, 1)
        acc = s.scalars(select(AccountRow).where(AccountRow.track_id == track_id)).one()
        pos = {p.symbol: p.qty for p in s.scalars(select(PositionRow).where(PositionRow.account_id == acc.id))}
        assert pos["AAPL.US"] > 0
        assert reconcile(s, acc) == 0


def test_scheduler_registers_jobs_per_market_timezone(settings):
    try:
        register_jobs()
        jobs = {j.id: j for j in get_scheduler().get_jobs()}
        assert {"cn_daily_1545", "hk_daily_1640", "us_daily_1650", "startup_catch_up"} <= set(jobs)
        assert str(jobs["us_daily_1650"].trigger.timezone) == "America/New_York"
        assert tuple(jobs["hk_daily_1640"].args) == ("HK",)
    finally:
        shutdown_scheduler()


async def test_hk_replay_reaches_last_day(arena, settings, monkeypatch):
    """港股数据就绪时间（16:30）晚于 A股，回放的模拟时钟必须在其之后，否则会少处理最后一天。"""
    specs = load_universe_specs(settings.config_dir)
    codes = [m["symbol"].split(".")[0] for n in ("hk_core", "hk_etf_core") for m in specs[n].members]
    provider = FakeIntlProvider("HK", {date(2026, 10, 1)}, dict.fromkeys(codes, 100))
    monkeypatch.setattr(deps, "hk_provider", lambda: provider)
    with get_sessionmaker()() as s:
        rs = s.scalars(select(RuleSetRow).where(RuleSetRow.name == "hk_long_only")).one()
        t = create_track(s, 1, name="港股回放", universe_name="hk_core_etf", rule_set_id=rs.id,
                         initial_cash=Decimal("1000000"), mode="replay", replay_start=date(2026, 9, 24))
        enroll(s, t, s.scalars(select(AgentRow).where(AgentRow.baseline_type == "buy_hold_index")).one().id)
        start_track(s, t, hk_time(2026, 10, 2, 10))
        s.commit()
        track_id = t.id
    msg = await cycle.run_replay(track_id)
    assert "2026-09-30 决策完成" in msg
    with get_sessionmaker()() as s:
        t = s.get(TrackRow, track_id)
        assert t.last_settled_date == t.replay_until == date(2026, 9, 30)
        # 买入持有基准已买入盈富基金
        acc = s.scalars(select(AccountRow).where(AccountRow.track_id == track_id)).one()
        assert s.scalars(select(PositionRow).where(PositionRow.symbol == "02800.HK",
                                                   PositionRow.account_id == acc.id)).first() is not None


def test_intl_provider_without_factor_table_still_returns_bars(monkeypatch):
    import pandas as pd

    from app.market.providers import sina_intl
    from app.market.types import Instrument

    class FakeAk:
        @staticmethod
        def stock_hk_daily(symbol, adjust=""):
            if adjust:
                raise SyntaxError("invalid syntax")      # 新浪没有 ETF 复权表时 AKShare 的报错
            return pd.DataFrame({"date": ["2026-09-29", "2026-09-30"], "open": [25.1, 25.0], "high": [25.3, 25.3],
                                 "low": [25.0, 24.9], "close": [25.14, 25.26], "volume": [1e8, 1e8]})

    monkeypatch.setattr(sina_intl, "_ak", lambda: FakeAk)
    monkeypatch.setattr("app.market.retry.time.sleep", lambda _s: None)
    etf = Instrument("02800.HK", "HK", "盈富基金", "etf", "fund", lot_size=500, tick=Decimal("0.001"))
    bars = sina_intl.SinaHKProvider().daily_bars(etf, date(2026, 9, 30), date(2026, 9, 30))
    assert len(bars) == 1 and bars[0].close == Decimal("25.260") and bars[0].prev_close == Decimal("25.14")
