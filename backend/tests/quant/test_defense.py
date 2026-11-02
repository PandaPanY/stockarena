"""三风格稳健加入国债和黄金（预注册 2026-11-02）：方案构成、开发期挑选与检验、留出期与前瞻期、加入策略库。"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from app.quant import defense
from app.quant.spec import parse_spec
from tests.quant.test_backtest import make_panel

STOCKS = ["600000.SH", "600001.SH", "600002.SH", "600003.SH", "600004.SH"]


@pytest.fixture(scope="module")
def panel():
    n = len(pd.bdate_range("2016-06-30", "2027-12-31"))
    rng = np.random.default_rng(11)
    closes = {}
    for k, s in enumerate(STOCKS):
        r = 0.0002 + rng.normal(0, 0.30 / np.sqrt(252), n)
        closes[s] = list(np.round(10 * (k + 1) * np.exp(np.cumsum(r)), 3))
    for s, v in ((defense.BOND, 0.03), (defense.GOLD, 0.15)):
        r = 0.0002 + rng.normal(0, v / np.sqrt(252), n)
        closes[s] = list(np.round(100 * np.exp(np.cumsum(r)), 3))
    types = {s: "stock" for s in STOCKS} | {defense.BOND: "etf", defense.GOLD: "etf"}
    return make_panel(closes, start="2016-06-30", types=types, bench=closes[STOCKS[0]])


@pytest.fixture(scope="module")
def styles():
    base = {"universe": {"types": ["stock"], "min_listed_days": 0, "min_amount": 0},
            "selection": {"top_n": 2}, "weighting": {"max_weight": 0.5}, "rebalance": {"every_days": 20}}
    return [parse_spec(base | {"factors": [{"name": "momentum", "params": {"window": w}, "direction": d}]})
            for w, d in ((60, "high"), (120, "low"), (20, "high"))]


def test_mix_and_plan(styles):
    assert defense.mix("base") == (1.0, 0.0, 0.0)
    assert defense.mix("C") == (0.50, 0.25, 0.25)
    s, b, g = defense.mix("B", 0.10)                                    # 避险 30% → 40%，国债与黄金各半
    assert (round(s, 4), round(b, 4), round(g, 4)) == (0.6, 0.2, 0.2)
    s, b, g = defense.mix("A", -0.10)
    assert (round(s, 4), b, round(g, 4)) == (0.9, 0.0, 0.1)
    members = defense.plan(styles, "A")
    assert len(members) == 4 and members[-1][0].universe.symbols == [defense.GOLD]   # A 没有国债成员
    assert abs(sum(w for _, w in members) - 1) < 1e-9 and abs(members[0][1] - 0.8 / 3) < 1e-9
    assert len(defense.plan(styles, "base")) == 3


def test_dev_holdout_forward(panel, styles):
    dev = defense.evaluate_dev(panel, styles)
    assert dev["window"] == ["2018-01-01", "2024-12-31"] and set(dev["results"]) == {"A", "B", "C", "base"}
    sharpes = {k: dev["results"][k]["sharpe"] for k in defense.CANDIDATES}
    assert dev["chosen"] == max(sharpes, key=sharpes.get)
    assert set(dev["checks"]) == {"sharpe_above_base", "significant", "drawdown_below_base"}
    assert dev["passed"] == all(dev["checks"].values()) and dev["p_max"] == pytest.approx(0.05 / 3)
    assert [n["shift"] for n in dev["neighbors"]] == [-0.1, 0.1]
    vols = [dev["results"][k]["volatility"] for k in ("base", "A", "C")]
    assert vols[0] > vols[2]                                            # 避险越多波动越低

    hold = defense.evaluate_holdout(panel, styles, dev)
    assert hold["window"][0] == "2025-01-01" and set(hold["results"]) == {dev["chosen"], "base"}
    assert set(hold["checks"]) == {"sharpe_kept", "drawdown_bounded"}

    fwd = defense.evaluate_forward(panel, styles, dev)
    assert fwd["final"] and fwd["days"] >= 250 and fwd["window"][0] == "2026-11-02"
    assert set(fwd["checks"]) == {"volatility_lower", "drawdown_not_worse"}


def test_forward_before_evaluation_date(panel, styles):
    cut = panel.locate(date(2027, 3, 1), "before")
    types = {s: ("etf" if s in (defense.BOND, defense.GOLD) else "stock") for s in panel.symbols}
    early = make_panel({s: list(panel.close[:cut + 1, j]) for j, s in enumerate(panel.symbols)},
                       start="2016-06-30", types=types)
    fwd = defense.evaluate_forward(early, styles, {"chosen": "A"})
    assert not fwd["final"] and 0 < fwd["days"] < 250 and "checks" not in fwd


def test_cli_runs_each_stage_once(panel, styles, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(defense, "_inputs", lambda: (panel, styles))
    monkeypatch.setattr(defense, "_path", lambda: tmp_path / "defense.json")
    monkeypatch.setattr(defense, "evaluate_dev", lambda p, st: {"chosen": "A", "passed": False})
    assert defense.main(["holdout"]) == 1                           # 开发期还没运行
    assert defense.main(["dev"]) == 0
    assert defense.main(["dev"]) == 1                               # 不重复运行
    assert defense.main(["holdout"]) == 1                           # 开发期没通过：不看留出期
    assert "没有通过" in capsys.readouterr().out
    monkeypatch.setattr(defense, "_load", lambda: {"dev": {"chosen": "A", "passed": True}})
    monkeypatch.setattr(defense, "evaluate_holdout", lambda p, st, d: {"passed": True})
    assert defense.main(["holdout"]) == 0
    monkeypatch.setattr(defense, "_load", lambda: {"dev": {"chosen": "A", "passed": True}, "holdout": {"x": 1}})
    assert defense.main(["holdout"]) == 1                           # 留出期只看一次
    monkeypatch.setattr(defense, "_load", lambda: {"dev": {"chosen": "A", "passed": True},
                                                   "holdout": {"passed": False}})
    assert defense.main(["library"]) == 1                           # 留出期没通过：不加入策略库
    made = []
    monkeypatch.setattr(defense, "_load", lambda: {"dev": {"chosen": "B", "passed": True},
                                                   "holdout": {"passed": True}})
    monkeypatch.setattr(defense, "add_to_library", lambda s, uid, chosen: made.append((uid, chosen)) or type(
        "P", (), {"name": "x", "id": 1, "follow_account_id": 2})())
    assert defense.main(["library"]) == 0 and made == [(1, "B")]


def test_add_to_library_creates_frozen_copies_once(lab, monkeypatch):  # noqa: F811
    from app.db.session import get_sessionmaker
    from app.models.quant import QuantStrategyRow
    from tests.quant.test_lab import BAD, GOOD, _create

    _, pid = lab
    ids = [_create(GOOD, pid), _create(BAD, pid), _create(GOOD)]
    with get_sessionmaker()() as s:
        champs = [s.get(QuantStrategyRow, i).champion_id for i in ids]
    monkeypatch.setattr(defense, "STYLE_VERSIONS", dict(zip(("价值投资", "ETF 轮动", "防御稳健"), champs, strict=True)))
    with get_sessionmaker()() as s:
        pf = defense.add_to_library(s, 1, "C", follow_cash=None)
        s.commit()
        assert defense.add_to_library(s, 1, "C", follow_cash=None).id == pf.id      # 不重复创建
        assert pf.settings == {"frozen": {"doc": defense.PREREG_DOC, "at": pf.settings["frozen"]["at"]}}
        assert [m["weight"] for m in pf.members] == [0.1667, 0.1667, 0.1667, 0.25, 0.25]
        sts = [s.get(QuantStrategyRow, m["strategy_id"]) for m in pf.members]
        assert [st.name for st in sts] == ["避险组合·价值投资", "避险组合·ETF 轮动", "避险组合·防御稳健",
                                           "避险组合·国债", "避险组合·黄金"]
        assert all(st.origin["kind"] == "prereg" and st.is_public and st.provider_id is None
                   and not st.auto_optimize and st.id not in ids for st in sts)
        from app.models.quant import QuantVersionRow

        copy, src = s.get(QuantVersionRow, sts[1].champion_id), s.get(QuantVersionRow, champs[1])
        assert parse_spec(copy.spec) == parse_spec(src.spec)                         # 冻结副本的规则不变


from tests.quant.test_lab import lab  # noqa: E402, F401
