"""A股规则测试：编号对应 PRD 附录 6.3。"""

from __future__ import annotations

import pytest

from app.trading.fees import calc_fees_cn
from app.trading.rules_cn import price_limits, sellable_qty, validate_qty
from app.trading.types import Position
from tests.trading.conftest import T1, T2, D, etf, stock


# ---------- CN-01 股票买入为 100 股整数倍 ----------
@pytest.mark.parametrize(("qty", "ok"), [(100, True), (1000, True), (150, False), (50, False), (0, False)])
def test_cn01_buy_lot(qty, ok):
    assert (validate_qty(stock(), "buy", qty, 0) is None) is ok


# ---------- CN-02 科创板买入 ≥200 股，1 股递增 ----------
@pytest.mark.parametrize(("qty", "ok"), [(200, True), (201, True), (199, False), (100, False)])
def test_cn02_star_buy(qty, ok):
    assert (validate_qty(stock("688981.SH", board="star"), "buy", qty, 0) is None) is ok


def test_cn02_star_sell_remainder_must_be_sold_at_once():
    star = stock("688981.SH", board="star")
    assert validate_qty(star, "sell", 150, 150) is None          # 余额不足 200 股一次性卖出
    assert validate_qty(star, "sell", 150, 350) is not None      # 不能只卖 150
    assert validate_qty(star, "sell", 201, 350) is None


# ---------- CN-03 零股须一次性卖出 ----------
def test_cn03_odd_lot_sell():
    s = stock()
    assert validate_qty(s, "sell", 110, 110) is None             # 全部卖出，含零股
    assert validate_qty(s, "sell", 100, 110) is None             # 整手卖出
    assert validate_qty(s, "sell", 50, 110) is not None          # 只卖零股的一部分
    assert validate_qty(s, "sell", 200, 110) is not None         # 超过可卖


# ---------- CN-04 最小价位：股票 0.01，ETF 0.001 ----------
def test_cn04_tick():
    assert stock().tick == D("0.01")
    assert etf().tick == D("0.001")


# ---------- CN-05 涨跌停价计算 ----------
@pytest.mark.parametrize(
    ("inst", "prev", "down", "up"),
    [
        (stock(), "10.00", "9.00", "11.00"),
        (stock(), "10.05", "9.05", "11.06"),                     # 11.055 四舍五入
        (stock(st=True), "10.00", "9.50", "10.50"),             # ST ±5%
        (stock("300750.SZ", board="gem"), "10.00", "8.00", "12.00"),
        (stock("688981.SH", board="star"), "10.00", "8.00", "12.00"),
    ],
)
def test_cn05_price_limits(inst, prev, down, up):
    assert price_limits(inst, D(prev)) == (D(down), D(up))


# ---------- CN-06 ETF 涨跌停与价位 ----------
def test_cn06_etf_price_limits():
    assert price_limits(etf(), D("4.432")) == (D("3.989"), D("4.875"))
    assert price_limits(etf(limit="0.20"), D("1.000")) == (D("0.800"), D("1.200"))
    assert price_limits(stock(), None) == (None, None)


# ---------- CN-07 T+1 / T+0 ----------
def test_cn07_t1_today_bought_not_sellable():
    pos = Position("600036.SH", 300, D("10"), last_buy_date=T1, last_buy_qty=200)
    assert sellable_qty(stock(), pos, T1) == 100
    assert sellable_qty(stock(), pos, T2) == 300


def test_cn07_t0_etf_sellable_same_day():
    pos = Position("513100.SH", 300, D("1"), last_buy_date=T1, last_buy_qty=300)
    assert sellable_qty(etf("513100.SH", t0=True), pos, T1) == 300
    assert sellable_qty(etf("510300.SH", t0=False), pos, T1) == 0


# ---------- CN-08/09/10 费用 ----------
def test_cn08_commission_minimum(params):
    fees = calc_fees_cn(stock(), "buy", 100, D("10"), params)     # 成交额 1000，佣金 0.25 → 最低 5 元
    assert fees["commission"] == D("5")


def test_cn09_cn10_stock_sell_fees(params):
    fees = calc_fees_cn(stock(), "sell", 10000, D("10"), params)  # 成交额 100,000
    assert fees == {"commission": D("25.00"), "transfer_fee": D("1.00"), "stamp_duty": D("50.00")}


def test_cn10_stock_buy_no_stamp_duty(params):
    fees = calc_fees_cn(stock(), "buy", 10000, D("10"), params)
    assert "stamp_duty" not in fees and fees["transfer_fee"] == D("1.00")


def test_cn09_cn10_etf_exempt(params):
    fees = calc_fees_cn(etf(), "sell", 100000, D("4"), params)    # ETF 免印花税和过户费
    assert fees == {"commission": D("100.00")}


# ---------- CN-11/12 见 test_matching.py；CN-13 见股票池；CN-14 交易时段在分钟级版本实现 ----------
def test_cn13_bse_excluded_from_default_pool(settings):
    import yaml

    raw = yaml.safe_load((settings.config_dir / "universes" / "cn_sse50_etf.yaml").read_text(encoding="utf-8"))
    assert raw["include"] == ["cn_sse50", "cn_etf_core"]   # 默认池只含上证50与 ETF，不含北交所


def test_fee_rounding_g01(params):
    # G-01：费用四舍五入到分
    fees = calc_fees_cn(stock(), "sell", 12345, D("12.34"), params)   # 成交额 152,337.30
    assert fees["commission"] == D("38.08")
    assert fees["stamp_duty"] == D("76.17")
    assert fees["transfer_fee"] == D("1.52")

