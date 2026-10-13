"""港股 / 美股数据解析（不访问网络）：复权表 → 除权事件 → 复权因子与前收盘价。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from app.market.providers.adjust import actions_from_factor_table, build_bars
from app.market.symbols import normalize, normalize_hk, normalize_us
from app.market.types import CorporateAction, Instrument
from app.market.universe import UniverseResolver, index_instrument, load_universe_specs
from app.trading.market_rules import hk_tick

D = Decimal
TENCENT = Instrument("00700.HK", "HK", "腾讯控股", "stock", "main", lot_size=100, tick=D("0.001"))


def test_hk_factor_table_cash_and_split():
    # 新浪港股 hfq 表：拆合股因子 + 累计派息（2014 年 1 拆 5）
    table = pd.DataFrame({
        "date": ["1900-01-01", "2014-05-15", "2025-05-16", "2026-05-15"],
        "hfq_factor": ["1", "5", "5", "5"],
        "cash": ["0", "10", "252.43", "278.93"],
    })
    actions = actions_from_factor_table("00700.HK", table, "hfq_factor", "cash", "t")
    by_date = {a.ex_date: a for a in actions}
    assert by_date[date(2014, 5, 15)].shares_per_share == D("4")      # 1 股变 5 股
    assert by_date[date(2026, 5, 15)].cash_per_share == D("5.3")      # (278.93 - 252.43) / 5


def test_us_factor_table_dividends():
    # 新浪美股 qfq 表：累计调整值越近越接近 0
    table = pd.DataFrame({"date": ["2026-05-11", "2026-02-09", "2025-11-10"], "qfq_factor": ["1", "1", "1"],
                          "adjust": ["0", "-0.27", "-0.53"]})
    actions = actions_from_factor_table("AAPL.US", table, "qfq_factor", "adjust", "t")
    assert [(a.ex_date, a.cash_per_share) for a in actions] == [
        (date(2026, 2, 9), D("0.26")), (date(2026, 5, 11), D("0.27"))]


def test_reverse_split_is_negative():
    table = pd.DataFrame({"date": ["2026-01-01", "2026-06-01"], "f": ["1", "0.1"], "c": ["0", "0"]})
    [a] = actions_from_factor_table("X.US", table, "f", "c", "t")
    assert a.shares_per_share == D("-0.9")


def test_build_bars_ex_dividend_reference_price_and_factor():
    raw = pd.DataFrame({"date": ["2026-05-13", "2026-05-14", "2026-05-15"], "open": [462.6, 460.0, 455.0],
                        "high": [465.0, 462.0, 458.0], "low": [460.0, 458.0, 450.0],
                        "close": [462.60001, 460.20001, 456.39999], "volume": [1e7, 1e7, 1e7]})
    action = CorporateAction("00700.HK", date(2026, 5, 15), cash_per_share=D("5.3"))
    bars = build_bars(TENCENT, raw, [action], date(2026, 5, 14), date(2026, 5, 15), tick_for=hk_tick,
                      places=D("0.001"))
    assert [b.date for b in bars] == [date(2026, 5, 14), date(2026, 5, 15)]
    assert bars[0].close == D("460.200")                        # 去掉浮点误差
    assert bars[1].prev_close == D("455.0")                     # 460.2 - 5.3 = 454.9 → 价位 0.2
    assert bars[1].adj_factor == pytest.approx(D("460.2") / D("454.9"))
    assert bars[0].amount == bars[0].close * bars[0].volume    # 无成交额列时用 收盘 × 成交量


@pytest.mark.parametrize(
    ("raw", "market", "expected"),
    [("700", "HK", "00700.HK"), ("0700.HK", "HK", "00700.HK"), ("hk02800", "HK", "02800.HK"),
     ("aapl", "US", "AAPL.US"), ("QQQ.US", "US", "QQQ.US"), ("600519", "CN", "600519.SH")],
)
def test_normalize(raw, market, expected):
    assert normalize(raw, market) == expected


def test_normalize_invalid():
    with pytest.raises(ValueError):
        normalize_hk("ABC")
    with pytest.raises(ValueError):
        normalize_us("$$")


def test_index_instrument_market():
    assert index_instrument("HSI.HK", "恒生指数").market == "HK"
    assert index_instrument("NDX.US", "纳斯达克100").market == "US"
    assert index_instrument("000300.SH", "沪深300").market == "CN"


class FakeHKSource:
    def securities(self):
        return {"00700": {"lot": 100, "stamp_duty": True, "category": "Equity", "name_en": "TENCENT"},
                "02800": {"lot": 500, "stamp_duty": False, "category": "Exchange Traded Products",
                          "name_en": "TRACKER FUND"}}


def test_resolve_hk_and_us_pools(settings, tmp_path):
    specs = load_universe_specs(settings.config_dir)
    resolver = UniverseResolver(specs, {"HK": FakeHKSource()}, tmp_path / "cache")
    with pytest.raises(ValueError, match="每手股数"):
        resolver.resolve("hk_core_etf", today=date(2026, 10, 2))   # 假数据源只有两只，其余取不到每手股数
    specs["hk_mini"] = specs["hk_etf_core"]
    specs["hk_mini"].members = [{"symbol": "00700.HK", "name": "腾讯控股"}, {"symbol": "2800", "name": "盈富基金"}]
    by = {i.symbol: i for i in resolver.resolve("hk_mini", today=date(2026, 10, 2))}
    assert by["00700.HK"].lot_size == 100 and by["00700.HK"].stamp_duty and by["00700.HK"].type == "stock"
    assert by["02800.HK"].lot_size == 500 and not by["02800.HK"].stamp_duty and by["02800.HK"].type == "etf"

    us = {i.symbol: i for i in resolver.resolve("us_core_etf", today=date(2026, 10, 2))}
    assert len(us) == 36 and us["AAPL.US"].lot_size == 1 and us["QQQ.US"].type == "etf"
    assert all(i.market == "US" and i.t0 and i.price_limit_pct is None for i in us.values())
