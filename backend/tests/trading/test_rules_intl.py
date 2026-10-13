"""港股、美股规则（PRD 附录 6.3 HK-01～HK-08、US-01～US-08）。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.market.types import DailyBar, Instrument
from app.trading.market_rules import get_rules, hk_tick
from app.trading.matching import match_market_order
from app.trading.params import RuleParams
from app.trading.portfolio import AccountState
from app.trading.targets import targets_to_intents
from app.trading.types import Position

D = Decimal
T = date(2026, 10, 2)
HK = get_rules("HK")
US = get_rules("US")
TENCENT = Instrument("00700.HK", "HK", "腾讯控股", "stock", "main", lot_size=100, tick=D("0.001"), t0=True)
TRACKER = Instrument("02800.HK", "HK", "盈富基金", "etf", "fund", lot_size=500, tick=D("0.001"), t0=True,
                     stamp_duty=False)
AAPL = Instrument("AAPL.US", "US", "苹果", "stock", "main", lot_size=1, tick=D("0.01"), t0=True, stamp_duty=False)
P_HK = RuleParams(commission_rate=D("0.0003"), commission_min=D("3"))
P_US = RuleParams(commission_rate=D("0"), commission_min=D("0"))


# ---------------- 港股 ----------------
@pytest.mark.parametrize(("qty", "ok"), [(100, True), (300, True), (150, False), (0, False)])
def test_hk01_board_lot(qty, ok):
    assert (HK.validate_qty(TENCENT, "buy", qty, 0) is None) is ok


def test_hk01_lot_differs_per_stock():
    assert HK.validate_qty(TRACKER, "buy", 100, 0) is not None
    assert HK.validate_qty(TRACKER, "buy", 500, 0) is None


@pytest.mark.parametrize(
    ("price", "tick"),
    [("0.2", "0.001"), ("0.3", "0.005"), ("5", "0.01"), ("15", "0.02"), ("50", "0.05"), ("150", "0.1"),
     ("431", "0.2"), ("600", "0.5"), ("1500", "1"), ("3000", "2"), ("6000", "5")],
)
def test_hk02_spread_table(price, tick):
    assert hk_tick(D(price)) == D(tick)


def test_hk03_no_price_limit_hk04_t0():
    assert HK.price_limits(TENCENT, D("400")) == (None, None)
    pos = Position("00700.HK", 200, D("400"), last_buy_date=T, last_buy_qty=200)
    assert HK.sellable_qty(TENCENT, pos, T) == 200


def test_hk05_hk06_fees_stock():
    # 成交额 431 × 1000 = 431,000 港元
    fees = HK.calc_fees(TENCENT, "sell", 1000, D("431"), P_HK)
    assert fees["commission"] == D("129.30")             # 0.03%
    assert fees["stamp_duty"] == D("431")               # 0.1% = 431，整数
    assert fees["trading_levy"] == D("11.64")           # 0.0027% = 11.637
    assert fees["afrc_levy"] == D("0.65")               # 0.00015% = 0.6465
    assert fees["trading_fee"] == D("24.35")            # 0.00565% = 24.3515
    assert fees["settlement_fee"] == D("8.62")          # 0.002%


def test_hk05_stamp_duty_rounds_up_and_settlement_bounds():
    fees = HK.calc_fees(TENCENT, "buy", 100, D("430.5"), P_HK)   # 43,050 → 印花税 43.05 向上取整 44
    assert fees["stamp_duty"] == D("44")
    assert fees["settlement_fee"] == D("2")                      # 最低 2 港元
    assert fees["commission"] == D("12.92")
    big = HK.calc_fees(TENCENT, "buy", 100_000, D("431"), P_HK)
    assert big["settlement_fee"] == D("100")                     # 最高 100 港元


def test_hk05_etf_exempt_from_stamp_duty():
    assert "stamp_duty" not in HK.calc_fees(TRACKER, "buy", 5000, D("25"), P_HK)


def test_hk_platform_fee():
    fees = HK.calc_fees(TENCENT, "buy", 100, D("431"), P_HK.model_copy(update={"platform_fee": D("15")}))
    assert fees["platform_fee"] == D("15")


def test_hk_fill_price_uses_spread_table():
    bar = DailyBar("00700.HK", T, D("431"), D("440"), D("425"), D("432"), 10_000_000, D("1"), D("430"))
    r = match_market_order("buy", 100, bar, TENCENT, P_HK)
    assert r.price == D("431.4")      # 431 × 1.0005 = 431.2155 → 价位 0.2 向上取整


def test_hk_targets_round_to_board_lot():
    bars = {TRACKER.symbol: DailyBar(TRACKER.symbol, T, *(D("25"),) * 4, 10**9, D("1"), D("25"))}
    conv = targets_to_intents({TRACKER.symbol: D("0.3")}, AccountState(cash=D("1000000")),
                              {TRACKER.symbol: TRACKER}, bars, P_HK)
    assert conv.intents[0].qty % 500 == 0 and conv.intents[0].qty == 11500


# ---------------- 美股 ----------------
def test_us01_single_share_and_t0():
    assert US.validate_qty(AAPL, "buy", 1, 0) is None
    assert US.validate_qty(AAPL, "buy", 0, 0) is not None
    assert US.validate_qty(AAPL, "sell", 3, 2) is not None
    pos = Position("AAPL.US", 5, D("300"), last_buy_date=T, last_buy_qty=5)
    assert US.sellable_qty(AAPL, pos, T) == 5


def test_us02_tick_us03_no_limits():
    assert US.tick(AAPL, D("330")) == D("0.01")
    assert US.tick(AAPL, D("0.5")) == D("0.0001")
    assert US.price_limits(AAPL, D("330")) == (None, None)


def test_us05_us06_us07_fees():
    assert US.calc_fees(AAPL, "buy", 100, D("330"), P_US) == {}           # 免佣金，买入无费用
    fees = US.calc_fees(AAPL, "sell", 100, D("330"), P_US)                # 成交额 33,000
    assert fees["sec_fee"] == D("0.92")                                   # 0.9174 向上取整到美分
    assert fees["finra_taf"] == D("0.02")                                 # 0.0166 → 0.02
    assert US.calc_fees(AAPL, "sell", 100_000, D("1"), P_US)["finra_taf"] == D("8.30")   # 上限


def test_us_commission_when_configured():
    p = RuleParams(commission_rate=D("0.001"), commission_min=D("1"))
    assert US.calc_fees(AAPL, "buy", 1, D("100"), p)["commission"] == D("1")


def test_us_targets_single_share():
    bars = {AAPL.symbol: DailyBar(AAPL.symbol, T, *(D("330"),) * 4, 10**8, D("1"), D("330"))}
    conv = targets_to_intents({AAPL.symbol: D("0.2")}, AccountState(cash=D("100000")), {AAPL.symbol: AAPL}, bars, P_US)
    assert conv.intents[0].qty == 60      # 19,900 / (330 × 1.0005) = 60.27 → 60 股


def test_unknown_market():
    with pytest.raises(ValueError):
        get_rules("JP")
