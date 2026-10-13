from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from app.analytics.metrics import (
    FillLike,
    TradeLot,
    ai_metrics,
    concentration,
    holding_days,
    leverage_stats,
    nav_metrics,
    radar_scores,
    trade_metrics,
)


def test_nav_metrics_basic():
    idx = pd.date_range("2026-01-01", periods=5)
    nav = pd.Series([100, 110, 99, 105, 120], index=idx)
    m = nav_metrics(nav, None)
    assert m["cumulative_return"] == pytest.approx(0.2)
    assert m["max_drawdown"] == pytest.approx(0.1)          # 110 → 99
    assert m["max_drawdown_days"] == 2
    assert m["annualized_return"] is None                    # 样本不足 60 天不年化
    assert m["reliable"] is False


def test_nav_metrics_benchmark_alpha_beta():
    idx = pd.date_range("2026-01-01", periods=80)
    bench = pd.Series([100 * (1.001 ** i) * (1 + 0.01 * ((-1) ** i)) for i in range(80)], index=idx)
    nav = bench * 2   # 与基准完全同步：beta=1，超额为 0
    m = nav_metrics(nav, bench)
    assert m["beta"] == pytest.approx(1.0)
    assert m["excess_return"] == pytest.approx(0.0, abs=1e-12)
    assert m["annualized_return"] is not None


def test_nav_metrics_single_point():
    assert nav_metrics(pd.Series([100.0]), None)["cumulative_return"] == 0.0


def test_trade_metrics():
    fills = [FillLike("buy", 10000, 5, None), FillLike("sell", 11000, 10, 980),
             FillLike("buy", 5000, 5, None), FillLike("sell", 4500, 5, -510)]
    m = trade_metrics(fills, initial_cash=100000, avg_nav=100000, days=10)
    assert m["win_rate"] == 0.5
    assert m["profit_factor"] == pytest.approx(980 / 510)
    assert m["total_fees"] == 25
    assert m["turnover"] == pytest.approx(30500 / 100000 * 252 / 10)


def test_ai_metrics():
    decs = [{"status": "ok", "latency_ms": 1000, "tokens_in": 10, "tokens_out": 5, "cost": "0.1"},
            {"status": "invalid", "latency_ms": 3000, "tokens_in": 10, "tokens_out": 5, "cost": "0.1"}]
    m = ai_metrics(decs, orders_total=4, orders_rejected=1)
    assert m["valid_rate"] == 0.5 and m["cost"] == pytest.approx(0.2) and m["reject_rate"] == 0.25
    assert ai_metrics([{"status": "baseline", "latency_ms": 0, "tokens_in": 0, "tokens_out": 0, "cost": 0}],
                      0, 0)["llm"] is False


def test_holding_days_fifo_with_shorts():
    trades = [TradeLot(date(2026, 9, 1), "A", "buy", 100), TradeLot(date(2026, 9, 3), "A", "buy", 100),
              TradeLot(date(2026, 9, 5), "A", "sell", 150),             # 100 股持有 4 天 + 50 股持有 2 天
              TradeLot(date(2026, 9, 1), "B", "short", 100), TradeLot(date(2026, 9, 11), "B", "cover", 100)]
    assert holding_days(trades) == pytest.approx((100 * 4 + 50 * 2 + 100 * 10) / 250)
    assert holding_days([TradeLot(date(2026, 9, 1), "A", "buy", 100)]) is None


def test_leverage_concentration_and_radar():
    assert leverage_stats([(100, 150, 50), (100, 80, 0), (100, None, None)]) == {"avg_leverage": 1.4,
                                                                              "max_leverage": 2.0}
    c = concentration([0.5, -0.5])
    assert c["max_weight"] == 0.5 and c["hhi"] == pytest.approx(0.5)
    r = radar_scores({"excess_return": 0.05, "max_drawdown": 0.1, "sharpe": 1.0, "win_rate": 0.6,
                      "profit_factor": 1.0, "fee_ratio": 0.001})
    assert r == {"return": 75.0, "risk": 50.0, "stability": 75.0, "efficiency": 55.0, "cost": 95.0}
    assert radar_scores({})["return"] is None
