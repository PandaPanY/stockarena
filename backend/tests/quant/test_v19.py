"""v1.9：新因子、行业中性化与行业限制、股息率与行业数据加载（PRD 3.9）。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import numpy as np
import pytest

from app.db.migrate import upgrade_to_head
from app.db.session import get_sessionmaker
from app.models.market import CorporateActionRow, IndustryRow
from app.models.quant import QuantStrategyRow
from app.quant.factors import compute
from app.quant.panel import load_dividend_yield, load_industries
from app.quant.signal import target_weights
from app.quant.spec import parse_spec
from tests.quant.test_lab import lab, synthetic_panel  # noqa: F401

SPEC = {"universe": {"types": ["stock"], "min_listed_days": 0, "min_amount": 0},
        "factors": [{"name": "momentum", "params": {"window": 60}, "direction": "high"}],
        "selection": {"top_n": 3}, "weighting": {"max_weight": 0.5}, "rebalance": {"every_days": 20}}


def _panel_with_industries():
    """UP1、UP2 同属电子（动量最强的两只），FLAT 属银行，DOWN 属煤炭。"""
    p = synthetic_panel()
    p.industry_names = ["电子", "银行", "煤炭"]
    code = {"UP1.SH": 0, "UP2.SH": 0, "FLAT1.SH": 1, "FLAT2.SH": 1, "DOWN1.SH": 2, "DOWN2.SH": 2}
    p.industry = np.full(p.shape, -1, dtype=np.int16)
    for sym, c in code.items():
        p.industry[:, p.col[sym]] = c
    return p


def _chosen(panel, spec, i=800):
    w, _ = target_weights(parse_spec(spec), panel, i)
    return {panel.symbols[j] for j in np.where(w > 0)[0]}


def test_max_per_industry_and_industry_filters():
    p = _panel_with_industries()
    assert {"UP1.SH", "UP2.SH"} <= _chosen(p, SPEC)
    capped = _chosen(p, {**SPEC, "selection": {"top_n": 3, "max_per_industry": 1}})
    assert len(capped & {"UP1.SH", "UP2.SH"}) == 1 and len(capped) == 3            # 同行业只留一只
    excl = _chosen(p, {**SPEC, "universe": {**SPEC["universe"], "exclude_industries": ["电子"]}})
    assert not excl & {"UP1.SH", "UP2.SH"}
    only = _chosen(p, {**SPEC, "universe": {**SPEC["universe"], "industries": ["煤炭"]}})
    assert only == {"DOWN1.SH", "DOWN2.SH"}


def test_industry_neutral_ranking_picks_best_of_each_industry():
    p = _panel_with_industries()
    spec = {**SPEC, "factors": [{**SPEC["factors"][0], "neutralize": "industry"}]}
    # 行业内排名：每个行业的第一名得分都是 1，三个行业各选一只
    picked = _chosen(p, spec)
    assert len(picked) == 3
    assert {p.industry[800, p.col[x]] for x in picked} == {0, 1, 2}


def test_unknown_industry_rejected():
    with pytest.raises(ValueError, match="未知行业"):
        parse_spec({**SPEC, "universe": {**SPEC["universe"], "industries": ["人工智能"]}})


def test_new_factors_on_synthetic_panel():
    p = synthetic_panel()
    T, N = p.shape
    p.fundamentals = {"shares": np.full((T, N), 10.0), "div_yield": np.full((T, N), 2.5),
                      "net_margin": np.full((T, N), 12.0), "growth_accel": np.full((T, N), -3.0)}
    p.cache.clear()
    j = p.col["UP1.SH"]
    assert np.isclose(compute(p, "size")[500, j], 10.0 * p.close_ff[500, j])
    assert compute(p, "dividend_yield")[500, j] == 2.5 and compute(p, "net_margin")[500, j] == 12.0
    assert compute(p, "growth_accel")[500, j] == -3.0
    for name in ("idio_vol", "skewness", "illiquidity", "turnover"):
        v = compute(p, name)[500]
        assert np.isfinite(v[:6]).all(), name
    assert (compute(p, "idio_vol")[500, :6] >= 0).all()
    # 没有财报数据的行情矩阵（如只有价格的测试数据）：财报类因子为 NaN，不报错
    q = synthetic_panel()
    assert np.isnan(compute(q, "size")[500]).all()


def test_load_dividend_yield_and_industries(settings):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    upgrade_to_head(settings.resolved_database_url)
    p = synthetic_panel()
    j = p.col["UP1.SH"]
    ex = p.dates[300]
    with get_sessionmaker()() as s:
        s.add(CorporateActionRow(symbol="UP1.SH", ex_date=ex, cash_per_share=Decimal("0.5"),
                                 shares_per_share=Decimal("0")))
        s.add_all([IndustryRow(symbol="UP1.SH", start_date=p.dates[400], level1="电子"),
                   IndustryRow(symbol="UP1.SH", start_date=p.dates[600], level1="计算机"),
                   IndustryRow(symbol="FLAT1.SH", start_date=date(2021, 7, 30), level1="银行")])
        s.commit()
        y = load_dividend_yield(s, p)
        codes, names = load_industries(s, p)
    expect = 0.5 / p.close_ff[299, j] * 100
    assert y[299, j] == 0 and np.isclose(y[300, j], expect) and np.isclose(y[540, j], expect)
    assert y[300 + 262, j] == 0                                   # 一年（365 天）后不再计入
    assert names == ["电子", "计算机", "银行"]
    assert codes[0, j] == 0 and codes[599, j] == 0 and codes[600, j] == 1          # 早于最早记录沿用最早一条
    assert codes[10, p.col["DOWN1.SH"]] == -1


def test_pbo_separates_noise_from_real_differences():
    from app.quant.evaluate import pbo_cscv

    rng = np.random.default_rng(0)
    noise = rng.normal(0, 0.05, (84, 20))
    assert 0.25 <= pbo_cscv(noise) <= 0.75                       # 纯噪音：挑样本内最好的和随便挑差不多
    real = rng.normal(0, 0.05, (84, 20)) + np.linspace(-0.03, 0.03, 20)
    assert pbo_cscv(real) < 0.1                                   # 版本之间有真实差异：样本内最好的样本外也好
    assert pbo_cscv(noise[:, :3]) is None                         # 版本太少不计算


def test_significance_threshold_counts_all_candidates_since_champion(lab):  # noqa: F811
    """显著性门槛按当前冠军以来累计检验过的候选数收紧。"""
    from app.models.quant import QuantVersionRow
    from app.quant import research, service
    from app.quant.research import ParsedResearch
    from tests.quant.test_lab import BAD, GOOD, _create

    sid = _create(GOOD)
    with get_sessionmaker()() as s:
        champ = s.get(QuantStrategyRow, sid).champion_id
        for k in range(4):                                         # 此前已有 4 个候选挑战过这个冠军
            s.add(QuantVersionRow(strategy_id=sid, number=10 + k, parent_id=champ, spec=BAD, status="rejected"))
        s.commit()
    cand = {**BAD, "selection": {"top_n": 3}}
    out = research._round_evaluate(sid, 0, ParsedResearch("", [{"title": "t", "rationale": "r",
                                                                 "spec": parse_spec(cand)}]))
    [entry] = out["candidates"]
    with get_sessionmaker()() as s:
        v = s.get(QuantVersionRow, entry["version_id"])
        assert v.dev["vs_champion"]["n_tests"] == 5
        q = service.research_quality(s, s.get(QuantStrategyRow, sid))
        assert q["tests_since_champion"] == 5
