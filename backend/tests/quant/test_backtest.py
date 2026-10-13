"""策略工坊回测内核（PRD 3.8）：因子无前视、A股成交规则、费用、止损、择时、规格校验与升级规则。"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from app.quant import factors
from app.quant.backtest import CostModel, run_backtest
from app.quant.evaluate import Gates, Windows, check_gates, evaluate, window_report
from app.quant.panel import Panel, _ttm
from app.quant.spec import describe, parse_spec


def make_panel(closes: dict[str, list[float]], opens: dict[str, list[float]] | None = None,
               start: str = "2023-01-02", types: dict[str, str] | None = None,
               factor: dict[str, list[float]] | None = None, bench: list[float] | None = None) -> Panel:
    syms = list(closes)
    T = len(next(iter(closes.values())))
    dates = [d.date() for d in pd.bdate_range(start, periods=T)]
    close = np.array([closes[s] for s in syms], dtype=float).T
    op = np.array([(opens or {}).get(s, closes[s]) for s in syms], dtype=float).T
    prev = np.vstack([close[:1], close[:-1]])
    prev[0] = np.nan
    fac = np.array([(factor or {}).get(s, [1.0] * T) for s in syms], dtype=float).T
    types = types or {}
    return Panel(
        dates=dates, symbols=syms, names=syms, types=np.array([types.get(s, "stock") for s in syms]),
        is_st=np.zeros(len(syms), bool), limit_pct=np.full(len(syms), 0.10), lot=np.full(len(syms), 100.0),
        tick=np.full(len(syms), 0.01), open=op, high=np.maximum(op, close), low=np.minimum(op, close), close=close,
        prev_close=prev, amount=np.full((T, len(syms)), 1e10), volume=np.full((T, len(syms)), 1e6),
        factor=fac, has_bar=~np.isnan(close), bench=np.array(bench or [100.0] * T),
    )


def spec(**kw) -> dict:
    base = {"universe": {"types": ["stock"], "min_listed_days": 0, "min_amount": 0},
            "factors": [{"name": "momentum", "params": {"window": 5}}],
            "selection": {"top_n": 1}, "weighting": {"max_weight": 1.0},
            "rebalance": {"every_days": 60, "min_trade_pct": 0}}
    for k, v in kw.items():
        base[k] = {**base.get(k, {}), **v} if isinstance(v, dict) else v
    return base


def rising(n: int, start: float = 10.0, step: float = 0.1) -> list[float]:
    return [round(start + step * i, 2) for i in range(n)]


# ---------------------------------------------------------------- 因子


def test_factors_use_only_past_data():
    p1 = make_panel({"A": rising(40), "B": rising(40, 20, -0.1)})
    closes = rising(40)
    closes[30:] = [99.0] * 10                   # 只改未来
    p2 = make_panel({"A": closes, "B": rising(40, 20, -0.1)})
    for name, params in [("momentum", {"window": 5}), ("volatility", {"window": 5}), ("rsi", {"window": 5}),
                         ("ma_trend", {"fast": 3, "slow": 10}), ("drawdown", {"window": 10})]:
        a, b = factors.compute(p1, name, params), factors.compute(p2, name, params)
        np.testing.assert_array_equal(a[:30], b[:30])


def test_momentum_value():
    p = make_panel({"A": rising(10, 10, 1)})
    m = factors.compute(p, "momentum", {"window": 5})
    assert np.isnan(m[4, 0]) and m[5, 0] == pytest.approx(15 / 10 - 1)


def test_ttm_eps_from_cumulative_reports():
    recs = [(date(2022, 9, 30), {"eps": 0.9}), (date(2022, 12, 31), {"eps": 1.2}), (date(2023, 9, 30), {"eps": 1.0})]
    assert _ttm(recs, "eps", 2) == pytest.approx(1.0 + 1.2 - 0.9)
    assert _ttm(recs, "eps", 0) == pytest.approx(0.9 * 12 / 9)       # 缺上年数据时按月份年化
    assert _ttm(recs, "eps", 1) == pytest.approx(1.2)


# ---------------------------------------------------------------- 规格


def test_spec_validation_messages():
    with pytest.raises(ValueError, match="未知因子 foo"):
        parse_spec(spec(factors=[{"name": "foo"}]))
    with pytest.raises(ValueError, match="超出范围"):
        parse_spec(spec(factors=[{"name": "momentum", "params": {"window": 1000}}]))
    with pytest.raises(ValueError, match="ETF 没有财报"):
        parse_spec(spec(universe={"types": ["etf"]}, factors=[{"name": "ep"}]))
    with pytest.raises(ValueError, match="top_n"):
        parse_spec(spec(selection={"top_n": 0}))
    s = parse_spec(spec())
    assert s.factors[0].params == {"window": 5, "skip": 0}           # 补全默认参数
    text = describe(s)
    assert any("动量" in t for t in text) and any("调仓" in t for t in text)


# ---------------------------------------------------------------- 成交规则与费用


def test_buys_next_open_in_lots_with_fees():
    p = make_panel({"A": rising(10), "B": rising(10, 20, -0.1)}, opens={"A": [x - 0.05 for x in rising(10)]})
    s = parse_spec(spec())
    res = run_backtest(s, p, p.dates[5], p.dates[-1], initial_cash=100_000,
                       cost=CostModel(slippage_bps=0, impact_coef=0))
    t = res.trades[0]
    # 第 6 天收盘出信号，第 7 天开盘买入 A（上涨的那只）
    assert (t.symbol, t.side, t.date) == ("A", "buy", p.dates[6])
    assert t.price == pytest.approx(p.open[6, 0]) and t.qty % 100 == 0
    assert t.fee == pytest.approx(max(t.amount * 0.00025, 5) + t.amount * 0.00001)
    assert res.nav[0] == 100_000 and res.exposure[-1] > 0.9


def test_limit_up_open_blocks_buy():
    closes = rising(12)
    opens = list(closes)
    opens[6] = round(closes[5] * 1.1, 2)        # 第 7 天开盘即涨停
    p = make_panel({"A": closes}, opens={"A": opens})
    s = parse_spec(spec(rebalance={"every_days": 1}))
    res = run_backtest(s, p, p.dates[5], p.dates[-1], cost=CostModel(slippage_bps=0))
    assert res.trades[0].date == p.dates[7] and res.blocked == {"A": 1}


@pytest.mark.parametrize(("start", "rate"), [("2023-08-07", 0.001), ("2023-09-04", 0.0005)])
def test_stamp_duty_rate_changes_on_2023_08_28(start, rate):
    p = make_panel({"A": rising(8) + [5.0] * 4}, start=start)
    s = parse_spec(spec(rebalance={"every_days": 1},
                        filters=[{"name": "momentum", "params": {"window": 5}, "op": ">", "value": 0.0}]))
    res = run_backtest(s, p, p.dates[5], p.dates[-1])
    assert res.trades[0].side == "buy"
    sell = next(t for t in res.trades if t.side == "sell")      # 下跌后过滤条件不满足 → 清仓
    assert sell.fee == pytest.approx(max(sell.amount * 0.00025, 5) + sell.amount * 0.00001 + sell.amount * rate)


def test_stop_loss_sells_and_blocks_rebuy():
    closes = rising(30)
    closes[10:] = [c * 0.85 for c in closes[10:]]   # 第 11 天下跌 15%
    p = make_panel({"A": closes, "B": [5.0] * 30})
    s = parse_spec(spec(risk={"stop_loss": 0.1}, rebalance={"every_days": 20}))
    res = run_backtest(s, p, p.dates[5], p.dates[-1], cost=CostModel(slippage_bps=0))
    assert res.trades[0].symbol == "A" and res.trades[0].date == p.dates[6]
    sells = [t for t in res.trades if t.side == "sell"]
    assert sells[0].symbol == "A" and sells[0].reason == "stop" and sells[0].date == p.dates[11]
    # 下次调仓（第 26 天信号）前不会买回 A
    rebuys = [t for t in res.trades if t.side == "buy" and t.symbol == "A" and t.date > p.dates[11]]
    assert all(t.date >= p.dates[26] for t in rebuys)
    assert res.closed and res.closed[0][1] < 0


def test_dividend_factor_keeps_nav_continuous():
    closes = [10.0] * 8 + [5.0] * 6               # 第 9 天 10 送 10（价格减半、复权因子翻倍）
    fac = [1.0] * 8 + [2.0] * 6
    p = make_panel({"A": closes}, factor={"A": fac})
    s = parse_spec(spec())
    res = run_backtest(s, p, p.dates[5], p.dates[-1], cost=CostModel(slippage_bps=0, commission_min=0,
                                                                         commission_rate=0, transfer_rate=0))
    assert res.trades[0].date == p.dates[6]
    assert res.nav[-1] == pytest.approx(res.nav[1], rel=1e-9)
    assert factors.compute(p, "momentum", {"window": 5})[10, 0] == pytest.approx(0.0)


def test_timing_moves_to_defensive_asset():
    T = 30
    bench = [100 - i for i in range(T)]           # 基准持续下跌 → 择时关闭
    p = make_panel({"A": rising(T), "BOND": [100.0] * T}, types={"BOND": "etf"}, bench=bench)
    s = parse_spec(spec(timing={"enabled": True, "name": "ma_bias", "params": {"window": 5}, "op": ">", "value": 0,
                                "exposure_off": 0.0, "defensive": "BOND"}, rebalance={"every_days": 5}))
    res = run_backtest(s, p, p.dates[6], p.dates[-1])
    assert {t.symbol for t in res.trades if t.side == "buy"} == {"BOND"}


def test_evaluate_windows_and_gates():
    T = 260
    rng = np.random.default_rng(1)
    a = list(np.cumprod(1 + rng.normal(0.001, 0.01, T)) * 10)
    b = list(np.cumprod(1 + rng.normal(0.0, 0.01, T)) * 10)
    p = make_panel({"A": a, "B": b}, start="2021-01-04")
    w = Windows(train=(p.dates[20], p.dates[150]), valid=(p.dates[151], p.dates[220]),
                holdout=(p.dates[221], p.dates[-1]))
    s = parse_spec(spec(selection={"top_n": 1}, rebalance={"every_days": 5}))
    res, rep = evaluate(s, p, w)
    assert rep["train"]["days"] == 130 and rep["valid"]["start"] == p.dates[151].isoformat()
    assert rep["train"]["yearly"][0]["year"] == 2021 and "turnover" in rep["valid"]
    assert window_report(res, p.dates[0], p.dates[1])["days"] == 0     # 区间早于回测起点

    assert rep["dev"]["start"] == rep["train"]["start"] and rep["dev"]["end"] == rep["valid"]["end"]
    assert rep["dev"]["monthly"][0][0] == "2021-02" and "sr_daily" in rep["dev"]


def _months(rets: list[float]) -> list[list]:
    return [[f"{2018 + i // 12}-{i % 12 + 1:02d}", r] for i, r in enumerate(rets)]


def test_gates_paired_yearly_and_significance():
    rng = np.random.default_rng(3)
    base = rng.normal(0.005, 0.04, 84)
    champ = {"dev": {"sharpe": 0.5, "max_drawdown": 0.2, "monthly": _months(list(base))}}
    better = base + 0.01 + rng.normal(0, 0.005, 84)        # 每月稳定多 1%
    good = {"dev": {"sharpe": 0.9, "max_drawdown": 0.18, "turnover": 5, "monthly": _months(list(better))},
            "robustness": {"samples": 6, "ratio": 0.9}}
    ok, why, test = check_gates(good, champ, Gates())
    assert ok and "开发期夏普 0.90" in why[0] and test["years"] == 7 and test["year_wins"] == 7 and test["p"] < 0.01

    noise = base + rng.normal(0, 0.02, 84)                   # 只是噪声
    ok, why, test = check_gates({**good, "dev": {**good["dev"], "monthly": _months(list(noise))}}, champ, Gates())
    assert not ok and (any("不显著" in r for r in why) or any("年跑赢冠军" in r for r in why))

    # 同轮候选越多，显著性门槛越严（p_max / n）
    ok, why, _ = check_gates({**good, "dev": {**good["dev"], "monthly": _months(list(noise))}}, champ,
                             Gates(p_max=0.2), n_tests=4)
    assert any("需 ≤ 0.05" in r for r in why) or not any("不显著" in r for r in why)

    ok, why, _ = check_gates({**good, "dev": {**good["dev"], "sharpe": 0.4}}, champ, Gates())
    assert not ok and any("不高于冠军" in r for r in why)
    ok, why, _ = check_gates({**good, "dev": {**good["dev"], "max_drawdown": 0.3}}, champ, Gates())
    assert not ok and "超过上限" in why[0]
    ok, why, _ = check_gates({**good, "robustness": {"samples": 6, "ratio": 0.4}}, champ, Gates())
    assert not ok and any("不够稳健" in r for r in why)
    ok, why, _ = check_gates(good, champ, Gates(min_holdings=20), holdings=10)
    assert not ok and any("过于集中" in r for r in why)
    # 冠军本身超过回撤上限：不比冠军差即可
    deep = {"dev": {**champ["dev"], "max_drawdown": 0.35}}
    assert check_gates({**good, "dev": {**good["dev"], "max_drawdown": 0.33}}, deep, Gates())[0]
    ok, why, _ = check_gates({**good, "dev": {**good["dev"], "max_drawdown": 0.36}}, deep, Gates())
    assert not ok and "冠军为 35.0%" in why[0]


def test_deflated_sharpe_penalizes_many_trials():
    from app.quant.evaluate import deflated_sharpe

    rep = {"sr_daily": 0.05, "days": 1700, "skew": 0.0, "kurt": 3.0}       # 年化夏普约 0.8
    one = deflated_sharpe(rep, 1, None)
    many = deflated_sharpe(rep, 50, 0.25)
    assert one > 0.95 and many < one


def test_etf_share_split_keeps_adjusted_price_continuous():
    from app.quant.panel import _fix_fund_splits

    close = np.array([[4.0, 10.0], [4.1, 10.2], [0.82, 10.4], [0.83, 3.0]])     # ETF 1 拆 5；股票暴跌不处理
    fac = np.ones_like(close)
    _fix_fund_splits(close, fac, ["etf", "stock"])
    adj = close * fac
    assert adj[2, 0] == pytest.approx(adj[1, 0]) and adj[3, 0] / adj[2, 0] == pytest.approx(0.83 / 0.82)
    assert (fac[:, 1] == 1).all()


def test_point_in_time_member_mask():
    from app.quant.panel import member_mask
    from app.quant.signal import eligible

    p = make_panel({"A": rising(20), "B": rising(20), "ETF": rising(20)}, types={"ETF": "etf"})
    p.member = member_mask(p, {"A": [(p.dates[0], None)], "B": [(p.dates[10], p.dates[15])]})
    s = parse_spec(spec(universe={"types": ["stock", "etf"], "min_listed_days": 0, "min_amount": 0}))
    assert list(eligible(s, p, 5)) == [True, False, True]       # B 尚未纳入；ETF 不受成分限制
    assert list(eligible(s, p, 12)) == [True, True, True]
    assert list(eligible(s, p, 16)) == [True, False, True]      # B 已被剔除
    from app.quant.explain import drop_reason

    assert "不是指数成分" in drop_reason(s, p, 16, 1)


def test_impact_cost_grows_with_trade_size():
    p = make_panel({"A": rising(30, 10, 0.05), "B": rising(30, 20, -0.05)})
    p.amount[:] = 5e6                                         # 每天成交 500 万
    s = parse_spec(spec())
    small = run_backtest(s, p, p.dates[5], p.dates[-1], 100_000, CostModel(slippage_bps=0, participation=1.0))
    big = run_backtest(s, p, p.dates[5], p.dates[-1], 5_000_000, CostModel(slippage_bps=0, participation=1.0))
    assert 0 < small.impact_cost / 100_000 < big.impact_cost / 5_000_000          # 冲击成本占比随资金增大
    assert small.trades[0].price < big.trades[0].price                            # 大单成交价更差
    capped = run_backtest(s, p, p.dates[5], p.dates[-1], 5_000_000)                # 成交额上限 10%
    assert capped.buy_filled < capped.buy_want * 0.2
    tiny = run_backtest(parse_spec(spec(selection={"top_n": 2})), make_panel({"A": [500.0] * 20, "B": [600.0] * 20}),
                        date(2023, 1, 9), date(2023, 1, 27), 50_000)
    assert tiny.lot_blocked > 0 and tiny.buy_filled == 0                         # 一手 5 万以上，资金不够
