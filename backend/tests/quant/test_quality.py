"""数据质量扫描：跳变、价格不一致、复权因子下降、长期无行情、数据未更新；忽略与新增错误。"""

from __future__ import annotations

import numpy as np

from app.quant import quality
from app.quant.panel import _fix_fund_splits
from tests.quant.test_backtest import make_panel


def _kinds(issues):
    return {(x["symbol"], x["kind"], x["level"]) for x in issues}


def test_scan_finds_jumps_and_bad_prices():
    n = 30
    a = [10.0] * n
    a[20] = 13.0                                   # 单日 +30%：超过两倍涨跌停 → 错误
    b = [10.0] * n
    b[15] = 11.5                                   # +15%：超过限制 → 提示
    c = [10.0] * n
    c[3] = 11.5                                    # 上市前几天不检查
    p = make_panel({"A": a, "B": b, "C": c})
    p.high[25, 1] = 9.0                            # 最高价低于收盘价
    issues = quality.scan_panel(p)
    kinds = _kinds(issues)
    assert ("A", "jump", "error") in kinds and ("B", "jump", "warn") in kinds and ("B", "ohlc", "error") in kinds
    assert not any(x["symbol"] == "C" for x in issues)
    assert issues[0]["level"] == "error"
    assert issues[0]["date"] >= issues[1]["date"] or issues[1]["level"] != "error"


def test_scan_gap_stale_and_resume():
    a = [10.0] * 10 + [np.nan] * 70 + [30.0] * 40   # 长期停牌后复牌首日 +200%：提示为真实涨跌
    b = [10.0] * 110 + [np.nan] * 10                # 最近 10 天没有日线
    p = make_panel({"A": a, "B": b})
    kinds = _kinds(quality.scan_panel(p))
    assert ("A", "gap", "info") in kinds and ("A", "jump", "info") in kinds
    assert ("B", "stale", "warn") in kinds


def test_scan_factor_drop():
    p = make_panel({"A": [10.0] * 20}, factor={"A": [1.0] * 10 + [0.5] * 10})
    assert ("A", "factor", "warn") in _kinds(quality.scan_panel(p))
    # 份额合并：价格翻倍、因子减半，复权价连续 → 参考
    p = make_panel({"A": [10.0] * 10 + [20.0] * 10}, factor={"A": [1.0] * 10 + [0.5] * 10})
    assert _kinds(quality.scan_panel(p)) == {("A", "factor", "info")}


def test_panel_fixes_small_fund_split():
    close = np.array([[7.236], [7.22], [6.30], [6.291]])
    factor = np.ones_like(close)
    _fix_fund_splits(close, factor, ["etf"], [0.10])
    adj = close[:, 0] * factor[:, 0]
    assert abs(adj[2] / adj[1] - 1) < 1e-9          # 折算当日复权价连续
    # 20% 涨跌停的 ETF（创业板 / 科创板类）真实上涨 18% 不能当成折算
    close2, factor2 = np.array([[1.0], [1.18]]), np.ones((2, 1))
    _fix_fund_splits(close2, factor2, ["etf"], [0.20])
    assert factor2[1, 0] == 1.0


def test_save_reports_new_errors_and_ignore(settings, client):
    from app.db.session import get_sessionmaker

    err = {"symbol": "A", "name": "A", "date": "2026-01-05", "kind": "jump", "level": "error", "detail": "x"}
    warn = {**err, "kind": "stale", "level": "warn"}
    res = {"checked_at": "t", "universes": {"u": {"issues": [err, warn]}}}
    with get_sessionmaker()() as s:
        assert quality.save(s, res) == [err]       # 新出现的错误
        s.commit()
        assert quality.save(s, res) == []          # 已经报告过
        quality.set_ignored(s, quality.issue_key(err), True)
        s.commit()
        rep = quality.report(s)
    assert rep["universes"]["u"]["issues"][0]["ignored"] is True
    r = client.get("/api/quant/data-quality").json()
    assert r["universes"]["u"]["issues"][0]["key"] == "A:2026-01-05:jump"


def test_walk_forward_selection():
    from app.quant.evaluate import walk_forward

    months = [f"{y}-{m:02d}" for y in range(2018, 2023) for m in range(1, 13)]
    rng = np.random.default_rng(0)
    # 版本 2 每年都稳定更好（排名能延续）；版本 3、4 是噪音
    series = {1: {k: 0.005 + rng.normal(0, 0.02) for k in months},
              2: {k: 0.02 + rng.normal(0, 0.01) for k in months},
              3: {k: rng.normal(0, 0.03) for k in months},
              4: {k: rng.normal(0, 0.03) for k in months}}
    wf = walk_forward(series, seed=1, holdout_year=2022)
    assert [r["year"] for r in wf["years"]] == [2020, 2021, 2022]      # 至少 24 个月历史后开始
    assert all(r["chosen"] == 2 for r in wf["years"])
    assert wf["beat_seed"] == 3 and wf["chosen_total"] > wf["seed_total"]
    assert wf["years"][-1]["holdout"] is True and wf["mean_percentile"] == 1.0
    assert walk_forward({1: series[1], 2: series[2]}, seed=1) is None   # 版本太少
