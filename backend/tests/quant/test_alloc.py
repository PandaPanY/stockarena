"""组合层资金配置（PRD 3.9 方向 B）：波动率倒数、风险平价、目标波动，以及回测与实时跟踪的一致性。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import numpy as np
import pytest
from sqlalchemy import select

from app.arena import cycle
from app.arena.lifecycle import create_track, enroll, start_track
from app.db.session import get_sessionmaker
from app.models import AgentRow, DecisionRow, RuleSetRow
from app.models.quant import QuantPortfolioRow
from app.quant import allocation
from app.quant.allocation import AllocError, AllocSettings, allocate
from app.quant.backtest import PortfolioTargeter, run_backtest
from app.quant.spec import parse_spec
from tests.arena.conftest import cst
from tests.quant.test_lab import GOOD, PANEL
from tests.quant.test_portfolio import SPEC_A, SPEC_B, _strategy


def _returns(vols, n=200, corr=0.0, seed=1):
    rng = np.random.default_rng(seed)
    common = rng.normal(0, 1, n)
    out = []
    for v in vols:
        e = rng.normal(0, 1, n)
        out.append((np.sqrt(corr) * common + np.sqrt(1 - corr) * e) * v / np.sqrt(252))
    return np.array(out).T


def test_inverse_vol_and_erc_weights():
    r = _returns([0.10, 0.20, 0.40], n=500)
    b = np.ones(3) / 3
    w, scale, _ = allocate(r, 499, b, AllocSettings(method="inverse_vol", lookback=500))
    assert w[0] > w[1] > w[2] and np.isclose(w.sum(), 1) and scale == 1.0
    assert np.isclose(w[0] / w[2], 4, rtol=0.2)                     # 波动之比约 1:4
    w, _, _ = allocate(r, 499, b, AllocSettings(method="erc", lookback=500))
    cov = np.cov(r.T)
    rc = w * (cov @ w)
    assert np.allclose(rc / rc.sum(), b, atol=0.02)                # 各成员风险贡献相等
    w, _, _ = allocate(r, 499, b, AllocSettings(method="fixed"))
    assert np.allclose(w, b)


def test_vol_target_scales_down_but_never_levers():
    r = _returns([0.30, 0.30], corr=0.9)
    b = np.array([0.5, 0.5])
    _, scale, note = allocate(r, 199, b, AllocSettings(vol_target=0.12))
    assert 0.3 <= scale < 0.6 and "预估波动" in note
    _, scale, _ = allocate(r, 199, b, AllocSettings(vol_target=0.50))
    assert scale == 1.0                                             # 不加杠杆
    _, scale, _ = allocate(r, 199, b, AllocSettings(vol_target=0.02))
    assert scale == 0.3                                             # 最低保留 30% 仓位


def test_falls_back_to_budget_when_data_insufficient():
    r = _returns([0.2, 0.2])
    r[:150] = np.nan
    w, scale, note = allocate(r, 199, np.array([0.7, 0.3]), AllocSettings(method="erc", lookback=120))
    assert np.allclose(w, [0.7, 0.3]) and scale == 1.0 and "不足" in note
    flat = _returns([0.2, 0.2])
    flat[:, 1] = 0.0                                                # 尚未建仓的成员
    w, _, note = allocate(flat, 199, np.array([0.5, 0.5]), AllocSettings(method="inverse_vol"))
    assert np.allclose(w, [0.5, 0.5]) and "波动过低" in note


def test_settings_validation():
    assert AllocSettings.from_dict(None).method == "fixed" and not AllocSettings().dynamic
    assert AllocSettings.from_dict({"method": "erc", "vol_target": 0.12}).dynamic
    with pytest.raises(AllocError):
        AllocSettings.from_dict({"method": "magic"})
    with pytest.raises(AllocError):
        AllocSettings.from_dict({"vol_target": 0.9})


def test_portfolio_targeter_reallocates_on_schedule(monkeypatch):
    monkeypatch.setattr(allocation, "MEMBER_START", 30)
    good, bad = parse_spec(GOOD), parse_spec({**GOOD, "factors": [{"name": "momentum", "params": {"window": 60},
                                                                     "direction": "low"}]})
    t = PortfolioTargeter([(good, 0.5), (bad, 0.5)], AllocSettings(method="inverse_vol", lookback=60, every=20,
                                                                   vol_target=0.08))
    res = run_backtest(None, PANEL, PANEL.dates[200], PANEL.dates[400], 1_000_000, targeter=t)
    dates = [x["date"] for x in t.alloc_log]
    # 第 200～399 个交易日收盘（最后一天不再调仓）每 20 个交易日更新一次
    assert len(dates) == 10 and dates[0] == PANEL.dates[200].isoformat() and dates[1] == PANEL.dates[220].isoformat()
    assert all(abs(sum(x["weights"]) - 1) < 1e-6 and 0.3 <= x["scale"] <= 1 for x in t.alloc_log)
    assert res.exposure.max() <= max(x["scale"] for x in t.alloc_log) + 0.02  # 总仓位按目标波动缩放


async def test_live_portfolio_alloc_matches_backtest(arena, monkeypatch):
    """实时组合选手与组合回测使用同一套资金配置规则：配置状态逐日推进，对账一致。"""
    monkeypatch.setattr(allocation, "MEMBER_START", 20)
    with get_sessionmaker()() as s:
        a, b = _strategy(s, "甲", SPEC_A), _strategy(s, "乙", SPEC_B)
        pf = QuantPortfolioRow(name="动态组合", members=[{"strategy_id": a, "weight": 0.5},
                                                     {"strategy_id": b, "weight": 0.5}],
                               settings={"alloc": {"method": "inverse_vol", "lookback": 40, "every": 5,
                                                   "vol_target": 0.05}})
        s.add(pf)
        s.flush()
        agent = AgentRow(name="组合·动态", kind="quant", quant_portfolio_id=pf.id)
        s.add(agent)
        rs = s.scalars(select(RuleSetRow).where(RuleSetRow.name == "cn_long_only")).one()
        track = create_track(s, 1, name="动态组合跟踪", universe_name="cn_sse50_etf", rule_set_id=rs.id,
                             initial_cash=Decimal("1000000"), mode="replay", replay_start=date(2026, 9, 1))
        s.flush()
        acc = enroll(s, track, agent.id)
        start_track(s, track, cst(2026, 10, 2, 10))
        s.commit()
        track_id, acc_id = track.id, acc.id
    assert "回放完成" in await cycle.run_replay(track_id, lambda m: None)
    with get_sessionmaker()() as s:
        decs = list(s.scalars(select(DecisionRow).where(DecisionRow.account_id == acc_id).order_by(DecisionRow.id)))
        sinces = [d.parsed["alloc"]["since"] for d in decs[:7]]
        assert sinces == [0, 1, 2, 3, 4, 0, 1]
        assert "资金配置（波动率倒数" in decs[0].analysis
        assert 0.3 <= decs[0].parsed["alloc"]["scale"] <= 1
        from app.quant.reconcile import reconcile as recon

        r = recon(s, acc_id)
        assert r["status"] in ("一致", "有偏差") and abs(r["gap"]) < 0.02, r
