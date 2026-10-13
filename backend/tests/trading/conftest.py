from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.market.types import DailyBar, Instrument
from app.trading.params import RuleParams

D = Decimal
T1 = date(2026, 10, 8)
T2 = date(2026, 10, 9)


def stock(symbol="600036.SH", board="main", st=False) -> Instrument:
    limit = D("0.20") if board in ("star", "gem") else (D("0.05") if st else D("0.10"))
    return Instrument(symbol, "CN", "测试股", "stock", board, lot_size=100, tick=D("0.01"),
                      is_st=st, price_limit_pct=limit)


def etf(symbol="510300.SH", t0=False, limit="0.10") -> Instrument:
    return Instrument(symbol, "CN", "测试ETF", "etf", "fund", lot_size=100, tick=D("0.001"),
                      t0=t0, price_limit_pct=D(limit))


def bar(symbol="600036.SH", d=T1, o="10", h=None, low=None, c=None, prev="10", volume=1_000_000) -> DailyBar:
    o = D(o)
    return DailyBar(symbol, d, o, D(h) if h else o, D(low) if low else o, D(c) if c else o,
                    volume, o * volume, D(prev) if prev else None)


@pytest.fixture
def params() -> RuleParams:
    return RuleParams()
