"""程序调参与分段一致性（PRD 3.8）。"""

from __future__ import annotations

import copy

import numpy as np

from app.quant.service import LabSettings
from app.quant.spec import parse_spec
from app.quant.tune import WINDOWS, tune
from tests.quant.test_lab import GOOD, PANEL, SETTINGS


def test_tune_uses_only_train_window_and_common_values():
    cfg = LabSettings.from_dict(SETTINGS)
    w = cfg.windows(PANEL.dates[-1])
    spec = parse_spec({**GOOD, "factors": [{"name": "momentum", "params": {"window": 10}}],
                       "selection": {"top_n": 4}})
    t = tune(spec, PANEL, w, cfg.gates(), budget=20)
    assert t.evals <= 20 and t.score >= t.base_score
    assert t.spec.factors[0].params["window"] in WINDOWS
    if t.changes:
        assert all("→" in c for c in t.changes)

    # 改动验证区间之后的行情，调参结果不变（只使用训练区间）
    p2 = copy.copy(PANEL)
    p2.cache = {}
    i = PANEL.locate(cfg.valid_start)
    for name in ("open", "close", "high", "low"):
        arr = getattr(PANEL, name).copy()
        arr[i:] = arr[i:] * np.linspace(1, 3, len(arr) - i)[:, None]
        setattr(p2, name, arr)
    p2.__post_init__()
    t2 = tune(spec, p2, w, cfg.gates(), budget=20)
    assert t2.spec == t.spec and t2.changes == t.changes


def test_settings_tune_flags():
    cfg = LabSettings.from_dict({**SETTINGS, "tune": "false", "tune_budget": "12"})
    assert cfg.tune is False and cfg.tune_budget == 12 and cfg.to_dict()["tune"] is False
    assert LabSettings.from_dict({k: v for k, v in SETTINGS.items() if k != "tune"}).tune is False   # 默认关闭


def test_factor_scorecard_detects_predictive_factor():
    from datetime import date

    from app.quant import factor_research as fr

    rows = fr.scorecard(PANEL, date(2018, 6, 1), date(2021, 6, 30), [("momentum", {"window": 60}), ("ep", {})],
                        horizon=20, step=20, min_stocks=5)
    # 合成数据中涨势股持续上涨：动量 IC 为正且显著；没有财报的因子被跳过
    assert [r["name"] for r in rows] == ["momentum"]
    m = rows[0]
    assert m["ic"] > 0 and m["direction"] == "high" and m["periods"] >= 6 and len(m["quintiles"]) in (0, 5)
    assert "动量(60,0)" in fr.scorecard_text(rows)
