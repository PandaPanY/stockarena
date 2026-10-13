"""交易成本设置：红利税按持有期（回测与交易引擎一致）、佣金设置、同步到跟踪赛道。"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal as D

import numpy as np
import pytest

from app.market.types import CorporateAction, Instrument
from app.models import TrackRow
from app.quant import costs
from app.quant.backtest import CostModel, dividend_tax_rate, run_backtest
from app.quant.spec import parse_spec
from app.trading.engine import apply_actions
from app.trading.fees import dividend_tax_rate as engine_rate
from app.trading.params import RuleParams
from app.trading.portfolio import AccountState
from app.trading.types import Fill, Position
from tests.quant.test_backtest import make_panel, spec

STOCK = Instrument("600036.SH", "CN", "招商银行", "stock", "main", price_limit_pct=D("0.10"))
ETF = Instrument("510300.SH", "CN", "沪深300ETF", "etf", "main", price_limit_pct=D("0.10"))


@pytest.fixture(autouse=True)
def _reset_costs() -> Iterator[None]:
    yield
    costs._by_user.clear()


def test_holding_period_rates_match():
    params = RuleParams(cn_dividend_tax_by_holding=True)
    ex = date(2026, 6, 1)
    for opened, rate in ((date(2026, 5, 20), 0.2), (date(2026, 1, 1), 0.1), (date(2025, 1, 1), 0.0)):
        assert float(engine_rate(STOCK, opened, ex, params)) == rate
        assert dividend_tax_rate((ex - opened).days) == rate
    assert engine_rate(ETF, date(2026, 5, 20), ex, params) == 0          # 基金分红免征
    assert engine_rate(STOCK, None, ex, params) == D("0.1")              # 缺少建仓日时按 10%
    assert engine_rate(STOCK, date(2026, 5, 20), ex, RuleParams()) == 0  # 未开启时用统一税率（默认 0）


def test_engine_deducts_tax_by_holding():
    params = RuleParams(cn_dividend_tax_by_holding=True)
    ex = date(2026, 6, 1)
    acc = AccountState(cash=D("0"), positions={
        STOCK.symbol: Position(STOCK.symbol, 1000, D("10"), opened_date=date(2026, 5, 25)),
        ETF.symbol: Position(ETF.symbol, 1000, D("4"), opened_date=date(2026, 5, 25)),
    })
    acts = [CorporateAction(STOCK.symbol, ex, cash_per_share=D("1")), CorporateAction(ETF.symbol, ex,
                                                                                      cash_per_share=D("0.1"))]
    apply_actions(acc, acts, params, ex, {STOCK.symbol: STOCK, ETF.symbol: ETF})
    assert acc.cash == D("800") + D("100")       # 股票 1000 元扣 20%，ETF 100 元免税


def test_fill_records_opened_date():
    acc = AccountState(cash=D("100000"))
    for d in (date(2026, 1, 5), date(2026, 3, 2)):
        acc.apply_fill(Fill(order_id=1, symbol=STOCK.symbol, side="buy", qty=100, price=D("10"), trade_date=d,
                            fees={}))
    assert acc.positions[STOCK.symbol].opened_date == date(2026, 1, 5)   # 加仓不改变建仓日


def _div_panel():
    closes = [10.0] * 8 + [9.0] * 6                 # 第 9 天每股派现 1 元（除息价 9，复权因子 ×10/9）
    fac = [1.0] * 8 + [10 / 9] * 6
    p = make_panel({"A": closes}, factor={"A": fac})
    p.cash_div = np.zeros(p.shape)
    p.cash_div[8, 0] = 1.0
    return p


def test_backtest_dividend_tax():
    p = _div_panel()
    free = dict(slippage_bps=0, commission_min=0, commission_rate=0, transfer_rate=0, impact_coef=0)
    s = parse_spec(spec())
    no_tax = run_backtest(s, p, p.dates[5], p.dates[-1], cost=CostModel(**free))
    taxed = run_backtest(s, p, p.dates[5], p.dates[-1], cost=CostModel(**free, dividend_tax=True))
    shares = no_tax.trades[0].qty
    assert no_tax.nav[-1] == pytest.approx(no_tax.nav[1], rel=1e-9)             # 不扣税：分红再投资，净值不变
    assert taxed.dividend_tax == pytest.approx(0.2 * 1.0 * shares)              # 持有 2 天，按 20%
    assert taxed.nav[-1] == pytest.approx(no_tax.nav[-1] - taxed.dividend_tax, rel=1e-9)


def test_backtest_uses_current_costs():
    p = _div_panel()
    s = parse_spec(spec())
    costs._by_user[1] = costs.TradeCosts(commission_rate=0.0001, commission_min=0, dividend_tax=False)
    res = run_backtest(s, p, p.dates[5], p.dates[-1])
    assert res.dividend_tax == 0
    buy = res.trades[0]
    assert buy.fee == pytest.approx(buy.amount * (0.0001 + 0.00001))           # 佣金万 1 免五 + 过户费
    costs._by_user[1] = costs.TradeCosts()
    assert run_backtest(s, p, p.dates[5], p.dates[-1]).dividend_tax > 0


def test_costs_validate_and_overrides():
    with pytest.raises(costs.CostError):
        costs.TradeCosts(commission_rate=0.01).validate()
    c = costs.TradeCosts(commission_rate=0.0001, commission_min=0, dividend_tax=True)
    assert "万 1" in c.describe() and "免五" in c.describe()
    track = TrackRow(rule_params={"commission_rate": "0.00025", "slippage_bps": "5"})
    costs.apply_to_track(track, c)
    params = RuleParams(**track.rule_params)
    assert params.commission_rate == D("0.0001") and params.commission_min == 0
    assert params.cn_dividend_tax_by_holding and params.slippage_bps == D("5")
    assert costs.TradeCosts.from_dict(c.to_dict()) == c
    assert c.signature() != costs.TradeCosts().signature()


def test_costs_api(client):
    r = client.get("/api/quant/costs").json()
    assert r["costs"] == costs.TradeCosts().to_dict()
    assert r["up_to_date"] is True               # 新库启动时没有需要重新回测的版本
    assert client.put("/api/quant/costs", json={"commission_rate": 0.01, "commission_min": 5}).status_code == 422
    r = client.put("/api/quant/costs", json={"commission_rate": 0.0001, "commission_min": 0, "dividend_tax": True})
    assert r.status_code == 200
    assert r.json()["costs"]["commission_rate"] == 0.0001 and r.json()["synced_tracks"] == 0
    assert costs.current().commission_min == 0


def test_dividend_tax_with_full_position_keeps_buying_safely():
    """满仓时扣红利税使现金为负，之后的调仓买入不能出错（预算按 0 计）。"""
    closes = [10.0] * 8 + [9.0] * 12
    p = make_panel({"A": closes, "B": [10.0 + 0.1 * i for i in range(20)]}, factor={"A": [1.0] * 8 + [10 / 9] * 12})
    p.cash_div = np.zeros(p.shape)
    p.cash_div[8, 0] = 1.0
    s = parse_spec(spec(selection={"top_n": 2}, weighting={"max_weight": 0.5}, rebalance={"every_days": 5}))
    res = run_backtest(s, p, p.dates[5], p.dates[-1], cost=CostModel(dividend_tax=True))
    assert res.dividend_tax > 0 and np.isfinite(res.nav).all()
