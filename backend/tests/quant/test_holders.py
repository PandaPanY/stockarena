"""股东户数因子（预注册（预注册文档已归档，未包含在本仓库））：按公告日可用、过期、同日多条、判定规则。"""

from __future__ import annotations

import numpy as np
import pytest

from app.quant import holders
from tests.quant.test_backtest import make_panel


def test_build_factor_point_in_time():
    panel = make_panel({"A.SH": [10.0] * 300, "B.SH": [10.0] * 300}, start="2024-01-01")
    d = panel.dates
    recs = {
        "A.SH": [
            # 截止日 1/31，公告日是第 30 个交易日（周三）：当天收盘后可用，之前没有值
            {"period": "2024-01-31", "announced": d[30].isoformat(), "now": 900, "prev": 1000},
            # 同一天补发两条：取统计截止日较新的一条
            {"period": "2024-03-31", "announced": d[80].isoformat(), "now": 1100, "prev": 1000},
            {"period": "2024-04-15", "announced": d[80].isoformat(), "now": 800, "prev": 1000},
            {"period": "2024-05-31", "announced": d[100].isoformat(), "now": 0, "prev": 1000},     # 无效，跳过
        ],
        "B.SH": [{"period": "2024-01-31", "announced": "2024-02-03", "now": 1200, "prev": 1000}],  # 周六公告
        "C.SH": [{"period": "2024-01-31", "announced": "2024-02-02", "now": 1, "prev": 1}],       # 不在股票池
    }
    f, info = holders.build_factor(panel, recs)
    a, b = panel.col["A.SH"], panel.col["B.SH"]
    assert np.isnan(f[29, a]) and f[30, a] == pytest.approx(0.1)            # 户数减少 10% → +0.1
    assert f[79, a] == pytest.approx(0.1) and f[80, a] == pytest.approx(0.2)
    # 截止日 4/15 + 200 天之后过期
    last = max(i for i in range(len(d)) if (d[i] - d[80]).days >= 0 and (d[i] - holders.date(2024, 4, 15)).days <= 200)
    assert f[last, a] == pytest.approx(0.2) and np.isnan(f[last + 1, a])
    # 周六公告：记在周五（公告日之前最后一个交易日）
    fri = panel.locate(holders.date(2024, 2, 2), "before")
    assert np.isnan(f[fri - 1, b]) and f[fri, b] == pytest.approx(-0.2)
    assert info["announce_lag_days"] > 0


def test_dev_verdict():
    years = {y: 0.01 for y in range(2018, 2025)}
    ok = holders.dev_verdict({"ic": 0.02, "t": 2.5, "years": years}, {"ic": 0.015, "t": 2.1})
    assert ok == {"P1": True, "P2": True, "P4": True, "pass": True}
    years_bad = {**years, 2018: -0.01, 2019: -0.01, 2020: -0.01}
    v = holders.dev_verdict({"ic": 0.02, "t": 2.5, "years": years_bad}, {"ic": 0.015, "t": 2.1})
    assert not v["P2"] and not v["pass"]
    v = holders.dev_verdict({"ic": 0.02, "t": 2.5, "years": years}, {"ic": 0.01, "t": 1.5})
    assert not v["P4"] and not v["pass"]
    assert not holders.dev_verdict({"ic": 0.02, "t": 1.9, "years": years}, {"ic": 0.02, "t": 3})["P1"]


def test_residual_removes_controls():
    rng = np.random.default_rng(0)
    size = rng.normal(size=200)
    v = 2 * size + rng.normal(scale=0.01, size=200)          # 因子几乎就是市值
    res = holders._residual_rank(v, [size, rng.normal(size=200)])
    assert abs(np.corrcoef(res, holders.pd.Series(size).rank())[0, 1]) < 0.2
