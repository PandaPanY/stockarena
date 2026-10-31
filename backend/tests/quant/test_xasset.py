"""跨资产 ETF 配置研究（预注册 2026-10-31）：方案构成、开发期挑选与检验、留出期只看一次、前瞻期评估时点。"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from app.quant import xasset
from tests.quant.test_backtest import make_panel

VOLS = {"510300.SH": 0.25, "511010.SH": 0.03, "518880.SH": 0.15, "513100.SH": 0.22}


@pytest.fixture(scope="module")
def panel():
    n = len(pd.bdate_range("2016-06-30", "2027-12-31"))
    rng = np.random.default_rng(7)
    closes = {}
    for k, (s, v) in enumerate(VOLS.items()):
        drift = -0.0004 if s == "513100.SH" else 0.0002          # 美股一路走弱：趋势过滤时大部分时间不持有
        r = drift + rng.normal(0, v / np.sqrt(252), n)
        closes[s] = list(np.round(10 * (k + 1) * np.exp(np.cumsum(r)), 3))
    return make_panel(closes, start="2016-06-30", types={s: "etf" for s in VOLS}, bench=closes["510300.SH"])


def test_plans():
    members, alloc = xasset.plan("C")
    assert [m.universe.symbols for m, _ in members] == [[s] for s in xasset.ASSETS]
    assert alloc.method == "erc" and alloc.lookback == 120 and alloc.every == 20
    members, alloc = xasset.plan("D")
    assert alloc.method == "fixed" and all(m.filters[0].params["window"] == 200 for m, _ in members)
    members, _ = xasset.plan("60_40")
    assert [w for _, w in members] == [0.6, 0.4]
    members, alloc = xasset.plan("B", lookback=60, every=10)
    assert alloc.lookback == 60 and alloc.every == 10 and members[0][0].rebalance.every_days == 10


def test_dev_holdout_forward(panel):
    dev = xasset.evaluate_dev(panel)
    assert dev["window"] == ["2018-01-01", "2024-12-31"]
    assert set(dev["results"]) == {"A", "B", "C", "D", "csi300", "60_40"}
    sharpes = {k: dev["results"][k]["sharpe"] for k in xasset.CANDIDATES}
    assert dev["chosen"] == max(sharpes, key=sharpes.get)
    assert set(dev["checks"]) == {"sharpe_above_60_40", "significant", "drawdown_below_60_40"}
    assert dev["passed"] == all(dev["checks"].values())
    for key in ("B", "C"):                                          # 波动最低的国债权重最大
        w = dev["results"][key]["avg_weights"]
        assert max(w, key=w.get) == "国债"
    assert dev["results"]["D"]["avg_exposure"] < dev["results"]["A"]["avg_exposure"] - 0.1
    assert len(dev["neighbors"]) == (4 if dev["chosen"] in ("B", "C") else 2)

    hold = xasset.evaluate_holdout(panel, dev)
    assert hold["window"][0] == "2025-01-01" and set(hold["results"]) == {dev["chosen"], "csi300", "60_40"}
    assert set(hold["checks"]) == {"sharpe_kept", "drawdown_bounded"}

    fwd = xasset.evaluate_forward(panel, dev)
    assert fwd["final"] and fwd["days"] >= 250 and fwd["window"][0] == "2026-10-30"
    assert set(fwd["checks"]) == {"volatility_half", "drawdown_half"}


def test_forward_before_evaluation_date(panel):
    cut = panel.locate(date(2027, 3, 1), "before")
    early = make_panel({s: list(panel.close[:cut + 1, j]) for j, s in enumerate(panel.symbols)},
                       start="2016-06-30", types={s: "etf" for s in panel.symbols})
    fwd = xasset.evaluate_forward(early, {"chosen": "A"})
    assert not fwd["final"] and 0 < fwd["days"] < 250 and "checks" not in fwd


def test_cli_runs_each_stage_once(panel, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(xasset, "_panel", lambda: panel)
    monkeypatch.setattr(xasset, "_path", lambda: tmp_path / "xasset.json")
    monkeypatch.setattr(xasset, "evaluate_dev", lambda p: {"chosen": "A", "passed": False})
    assert xasset.main(["holdout"]) == 1                            # 开发期还没运行
    assert xasset.main(["dev"]) == 0
    assert xasset.main(["dev"]) == 1                                # 不重复运行
    assert xasset.main(["holdout"]) == 1                            # 开发期没通过：不看留出期
    assert "没有通过" in capsys.readouterr().out
    monkeypatch.setattr(xasset, "_load", lambda: {"dev": {"chosen": "A", "passed": True}})
    monkeypatch.setattr(xasset, "evaluate_holdout", lambda p, d: {"passed": True})
    assert xasset.main(["holdout"]) == 0
    monkeypatch.setattr(xasset, "_load", lambda: {"dev": {"chosen": "A", "passed": True}, "holdout": {"x": 1}})
    assert xasset.main(["holdout"]) == 1                            # 留出期只看一次


def test_costs_fixed_ignores_user_settings():
    from app.quant import costs

    mine = costs.TradeCosts(commission_rate=0.001, commission_min=0)
    with costs.fixed(mine):
        assert costs.current() is mine
        with costs.use(5):
            assert costs.current() is mine                          # 固定的成本优先于用户设置
