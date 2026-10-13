from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.agent.context import MarketSnapshot
from app.baselines import decide_baseline
from app.market.types import DailyBar, Instrument
from app.trading.portfolio import AccountState
from app.trading.types import Position

D = date(2026, 9, 30)


def _snap(n: int) -> MarketSnapshot:
    insts, bars = {}, {}
    for i in range(n):
        sym = f"6000{i:02d}.SH"
        insts[sym] = Instrument(sym, "CN", f"股{i}", "stock", "main")
        bars[sym] = DailyBar(sym, D, Decimal(10), Decimal(10), Decimal(10), Decimal(10), 1000, Decimal(1), Decimal(10))
    insts["510050.SH"] = Instrument("510050.SH", "CN", "上证50ETF", "etf", "fund", tick=Decimal("0.001"))
    bars["510050.SH"] = DailyBar("510050.SH", D, *(Decimal(3),) * 4, 1000, Decimal(1), Decimal(3))
    return MarketSnapshot(D, date(2026, 10, 8), "", "", {}, bars, insts)


def test_equal_weight_exempt_from_order_cap():
    dec = decide_baseline("equal_weight", AccountState(cash=Decimal("1000000")), _snap(59), "000016.SH", "s")
    assert len(dec.targets) == 60
    assert dec.param_overrides["max_orders_per_round"] >= 60


def test_buy_hold_index_full_position_once():
    snap = _snap(3)
    dec = decide_baseline("buy_hold_index", AccountState(cash=Decimal("100000")), snap, "000016.SH", "s")
    assert dec.targets == {"510050.SH": Decimal("1")} and dec.param_overrides["max_position_pct"] == 1
    held = AccountState(cash=Decimal("0"), positions={"510050.SH": Position("510050.SH", 100, Decimal(3))})
    assert decide_baseline("buy_hold_index", held, snap, "000016.SH", "s").targets is None


def test_random_is_deterministic_per_seed():
    snap = _snap(20)
    a = decide_baseline("random", AccountState(cash=Decimal("1")), snap, "000016.SH", "seed-1")
    b = decide_baseline("random", AccountState(cash=Decimal("1")), snap, "000016.SH", "seed-1")
    assert a.targets == b.targets and len(a.targets) == 5
