from __future__ import annotations

from app.trading.matching import fill_price, match_market_order
from tests.trading.conftest import D, bar, etf, stock


def test_market_buy_at_open_plus_slippage_rounded_up(params):
    # G-02/G-03：10.00 × 1.0005 = 10.005 → 向上取整 10.01
    r = match_market_order("buy", 1000, bar(o="10", h="10.5", low="9.8"), stock(), params)
    assert (r.qty, r.price) == (1000, D("10.01"))


def test_market_sell_at_open_minus_slippage_rounded_down(params):
    r = match_market_order("sell", 1000, bar(o="10", h="10.5", low="9.8"), stock(), params)
    assert r.price == D("9.99")


def test_fill_price_capped_by_high_and_limit(params):
    # 开盘即最高价时，买入价不超过最高价
    assert fill_price("buy", bar(o="10", h="10", low="9.5"), stock(), params) == D("10")
    # 涨停价开盘（非一字板），买入价不超过涨停价
    assert fill_price("buy", bar(o="11", h="11", low="10.5", prev="10"), stock(), params) == D("11.00")


def test_etf_tick(params):
    r = match_market_order("buy", 1000, bar("510300.SH", o="4.432", h="4.5", low="4.4", prev="4.4"), etf(), params)
    assert r.price == D("4.435")   # 4.432 × 1.0005 = 4.434216 → 4.435


def test_cn11_one_price_limit_up_cannot_buy(params):
    b = bar(o="11", h="11", low="11", prev="10")
    assert match_market_order("buy", 100, b, stock(), params).qty == 0
    assert match_market_order("sell", 100, b, stock(), params).qty == 100   # 一字涨停可以卖


def test_cn11_one_price_limit_down_cannot_sell(params):
    b = bar(o="9", h="9", low="9", prev="10")
    r = match_market_order("sell", 100, b, stock(), params)
    assert r.qty == 0 and "跌停" in r.reason
    assert match_market_order("buy", 100, b, stock(), params).qty == 100


def test_cn12_suspended(params):
    assert match_market_order("buy", 100, None, stock(), params).qty == 0
    assert match_market_order("buy", 100, bar(volume=0), stock(), params).reason == "停牌或无行情"


def test_g04_volume_participation_cap(params):
    # 成交量 10,000 × 10% = 1,000 股
    r = match_market_order("buy", 5000, bar(volume=10_000), stock(), params)
    assert r.qty == 1000 and "部分成交" in r.reason


def test_g04_odd_lot_sell_all_within_cap(params):
    r = match_market_order("sell", 150, bar(volume=10_000), stock(), params, sell_all=True)
    assert r.qty == 150


def test_star_buy_cap_below_minimum(params):
    star = stock("688981.SH", board="star")
    r = match_market_order("buy", 1000, bar("688981.SH", volume=1500), star, params)
    assert r.qty == 0   # 上限 100 股 < 科创板最低 200 股
