"""数据源解析逻辑（不访问网络）：前收盘价与复权因子推算。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pandas as pd

from app.market.providers.akshare_cn import AkshareCNProvider
from app.market.types import Instrument

CMB = Instrument("600036.SH", "CN", "招商银行", "stock", "main")


def test_build_bars_prev_close_on_ex_dividend_date():
    # 招商银行 2026-07-10 除息，每股派 1.003 元：除权参考价 = 37.55 - 1.003 ≈ 36.55
    raw = pd.DataFrame({
        "date": ["2026-07-08", "2026-07-09", "2026-07-10"],
        "open": [37.9, 37.6, 36.9], "high": [38.0, 38.0, 37.0], "low": [37.5, 37.4, 36.5],
        "close": [37.94, 37.55, 36.88], "volume": [1000.0, 2000.0, 3000.0], "amount": [1e6, 2e6, 3e6],
    })
    factors = pd.DataFrame({
        "date": pd.to_datetime(["2026-01-16", "2026-07-10"]),
        "factor": [Decimal("6.5621943831410000"), Decimal("6.7422879877129000")],
    })
    bars = AkshareCNProvider._build_bars(CMB, raw, factors, date(2026, 7, 9), date(2026, 7, 10))
    assert [b.date for b in bars] == [date(2026, 7, 9), date(2026, 7, 10)]
    assert bars[0].prev_close == Decimal("37.94")      # 非除权日：前收盘 = 昨收
    assert bars[1].prev_close == Decimal("36.55")      # 除权日：参考价
    assert bars[1].adj_factor == Decimal("6.7422879877129000")
    assert bars[1].volume == 3000
    assert isinstance(bars[1].close, Decimal) and bars[1].close == Decimal("36.88")


def test_build_bars_first_row_without_history_has_no_prev_close():
    raw = pd.DataFrame({"date": ["2026-07-08"], "open": [1.0], "high": [1.0], "low": [1.0],
                        "close": [1.0], "volume": [1.0], "amount": [1.0]})
    factors = pd.DataFrame({"date": pd.to_datetime(["1900-01-01"]), "factor": [Decimal("1")]})
    bars = AkshareCNProvider._build_bars(CMB, raw, factors, date(2026, 7, 8), date(2026, 7, 8))
    assert bars[0].prev_close is None
