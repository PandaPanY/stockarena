"""盘中引擎端到端：模拟 2026-10-05（港股交易日）的时钟与实时行情，不访问网络。"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.arena import cycle, deps
from app.arena.intraday import IntradayEngine, market_phase
from app.arena.lifecycle import ArenaError, create_track, enroll, start_track
from app.db.session import get_sessionmaker
from app.market.realtime import Quote
from app.market.universe import load_universe_specs
from app.models import (
    AccountRow,
    AgentRow,
    DecisionRow,
    FillRow,
    MinuteBarRow,
    NavSnapshotRow,
    OrderRow,
    RoundRow,
    RuleSetRow,
    TrackRow,
)
from tests.arena.conftest import FakeLLM
from tests.arena.test_markets import FakeIntlProvider


def hkt(h: int, m: int, s: int = 0, day: int = 5) -> datetime:
    return datetime(2026, 10, day, h, m, s, tzinfo=UTC) - timedelta(hours=8)


class FakeQuotes:
    def __init__(self, symbols: list[str]):
        self.prices = {s: Decimal("25") if s.startswith(("028", "030", "031")) else Decimal("400") for s in symbols}
        self.volume = 0

    def fetch(self, symbols):
        self.volume += 1_000_000
        return {s: Quote(s, self.prices[s], self.prices[s], self.prices[s], self.prices[s], self.prices[s],
                         self.volume, Decimal("0"), "2026-10-05") for s in symbols if s in self.prices}


def test_market_phase_hk_and_us():
    assert market_phase("HK", hkt(9, 29)).state == "closed"
    assert market_phase("HK", hkt(10, 0)).state == "open"
    assert market_phase("HK", hkt(12, 30)).state == "break"
    assert market_phase("HK", hkt(16, 0)).state == "closed"
    assert market_phase("HK", hkt(10, 0, day=1)).session is None            # 国庆假期
    us_open = datetime(2026, 10, 5, 13, 35, tzinfo=UTC)                     # 美东 09:35（夏令时）
    assert market_phase("US", us_open).state == "open"


@pytest.fixture
def intraday_track(arena, settings, monkeypatch):
    specs = load_universe_specs(settings.config_dir)
    symbols = [m["symbol"] for n in ("hk_core", "hk_etf_core") for m in specs[n].members]
    lots = {s.split(".")[0]: (500 if s == "02800.HK" else 100) for s in symbols}
    provider = FakeIntlProvider("HK", {date(2026, 10, 1)}, lots)
    monkeypatch.setattr(deps, "hk_provider", lambda: provider)
    llm = FakeLLM(json.dumps({"analysis": "盘中配置", "targets": [{"symbol": "700", "weight": 0.2},
                                                               {"symbol": "2800", "weight": 0.3}]}))
    monkeypatch.setattr(cycle, "create_client", lambda cfg: llm)
    with get_sessionmaker()() as s:
        rs = s.scalars(select(RuleSetRow).where(RuleSetRow.name == "hk_long_only")).one()
        t = create_track(s, 1, name="港股盘中", universe_name="hk_core_etf", rule_set_id=rs.id,
                         initial_cash=Decimal("1000000"), frequency="intraday", interval_min=15,
                         event_move_pct=Decimal("0.03"))
        for a in s.scalars(select(AgentRow)):
            if a.kind == "llm" or a.baseline_type == "buy_hold_index":
                enroll(s, t, a.id)
        start_track(s, t, hkt(9, 0))
        s.commit()
        assert t.start_date == date(2026, 10, 5)
        return t.id, FakeQuotes([*symbols, "HSI.HK"]), llm


async def test_intraday_day(intraday_track):
    track_id, quotes, llm = intraday_track
    engine = IntradayEngine(client=quotes)

    await engine.tick(hkt(9, 31))                       # 开盘：开盘前处理，尚未到首轮决策时间
    with get_sessionmaker()() as s:
        assert s.get(TrackRow, track_id).intraday_day == date(2026, 10, 5)
    await engine.tick(hkt(9, 32, 10))                   # 首轮定时决策（开盘后 2 分钟）
    await engine.round_tasks[track_id]
    with get_sessionmaker()() as s:
        [rnd] = s.scalars(select(RoundRow).where(RoundRow.track_id == track_id)).all()
        assert rnd.trigger == "interval" and rnd.status == "done"
        pending = list(s.scalars(select(OrderRow).where(OrderRow.status == "pending")))
        assert pending and all(o.active_from == hkt(9, 33) for o in pending)   # 下一分钟开始生效
    with get_sessionmaker()() as s:
        dec = s.scalars(select(DecisionRow).where(DecisionRow.status == "ok")).first()
        assert "盘中决策" in dec.prompt_system and "09:32 实时行情" in dec.prompt_user

    # 订单还没成交时又来一轮（如异动触发）：目标仓位模式撤销上一轮未成交的订单，避免重复买入
    with get_sessionmaker()() as s:
        first_ids = {o.id for o in s.scalars(select(OrderRow).where(OrderRow.status == "pending"))}
    await cycle.run_live_round(track_id, quotes.fetch(list(quotes.prices)), hkt(9, 32, 40), "event")
    with get_sessionmaker()() as s:
        old = [s.get(OrderRow, i) for i in first_ids]
        assert all(o.status == "cancelled" and "新一轮目标仓位" in o.note for o in old if o.side == "buy"
                   and o.order_type == "market")
        assert s.scalars(select(OrderRow).where(OrderRow.status == "pending")).first() is not None

    await engine.tick(hkt(9, 33, 5))                    # 完成 09:32 的 K 线：订单尚未生效
    with get_sessionmaker()() as s:
        assert s.scalars(select(FillRow)).first() is None
    await engine.tick(hkt(9, 34, 5))                    # 完成 09:33 的 K 线：撮合
    with get_sessionmaker()() as s:
        fills = list(s.scalars(select(FillRow)))
        assert fills and all(f.price_source == "分钟线 09:33" for f in fills)
        assert s.scalars(select(MinuteBarRow).where(MinuteBarRow.symbol == "00700.HK")).first() is not None

    # 腾讯上涨 5%（超过 3% 阈值）且距上一轮已超过 3 分钟：触发异动决策
    quotes.prices["00700.HK"] = Decimal("420")
    await engine.tick(hkt(9, 37))
    await engine.round_tasks[track_id]
    with get_sessionmaker()() as s:
        triggers = [r.trigger for r in s.scalars(select(RoundRow).where(RoundRow.track_id == track_id))]
        assert triggers == ["interval", "event", "event"]

    # 收盘：当日有效的未成交订单失效
    await engine.tick(hkt(15, 59, 30))
    await engine.tick(hkt(16, 0, 30))
    with get_sessionmaker()() as s:
        assert not [o for o in s.scalars(select(OrderRow).where(OrderRow.status == "pending")) if o.tif == "day"]

    # 收盘后的日终流程：只记录净值，不做日频决策
    msg = await cycle.run_track_cycle(track_id, now=hkt(17, 0))
    assert msg == "已记录收盘净值（分钟级赛道在盘中决策）"
    with get_sessionmaker()() as s:
        acc_ids = [a.id for a in s.scalars(select(AccountRow).where(AccountRow.track_id == track_id))]
        for aid in acc_ids:
            dates = [n.date for n in s.scalars(select(NavSnapshotRow).where(NavSnapshotRow.account_id == aid))]
            assert dates == [date(2026, 10, 2), date(2026, 10, 5)]     # 初始净值记在前一交易日
        assert len(s.scalars(select(RoundRow).where(RoundRow.track_id == track_id)).all()) == 3


def test_intraday_validation(arena):
    with get_sessionmaker()() as s:
        rs = s.scalars(select(RuleSetRow).where(RuleSetRow.name == "cn_long_only")).one()
        with pytest.raises(ArenaError, match="只支持实时"):
            create_track(s, 1, name="x", universe_name="cn_sse50_etf", rule_set_id=rs.id, initial_cash=Decimal("1"),
                         frequency="intraday", mode="replay", replay_start=date(2026, 9, 1))
        with pytest.raises(ArenaError, match="间隔"):
            create_track(s, 1, name="x", universe_name="cn_sse50_etf", rule_set_id=rs.id, initial_cash=Decimal("1"),
                         frequency="intraday", interval_min=7)
