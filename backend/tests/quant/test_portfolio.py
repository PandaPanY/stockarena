"""多风格组合（PRD 3.9）：分仓回测相加、组合接口、实时跟踪中按成员调仓周期更新。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.arena import cycle
from app.arena.accounts import reconcile
from app.arena.lifecycle import create_track, enroll, start_track
from app.db.session import get_sessionmaker
from app.models import AccountRow, AgentRow, DecisionRow, RuleSetRow
from app.models.quant import QuantPortfolioRow, QuantStrategyRow, QuantVersionRow
from app.quant.backtest import PORTFOLIO_MIN_TRADE, PortfolioTargeter, SingleTargeter, run_backtest
from app.quant.portfolio import combine
from app.quant.spec import parse_spec
from tests.arena.conftest import cst
from tests.quant.test_lab import BAD, GOOD, PANEL, _create, lab  # noqa: F401


def test_combine_equals_sum_of_member_backtests():
    a = run_backtest(parse_spec(GOOD), PANEL, PANEL.dates[60], PANEL.dates[-1], 600_000)
    b = run_backtest(parse_spec(BAD), PANEL, PANEL.dates[60], PANEL.dates[-1], 400_000)
    c = combine([a, b])
    assert c.initial_cash == 1_000_000 and c.nav[-1] == pytest.approx(a.nav[-1] + b.nav[-1])
    assert len(c.trades) == len(a.trades) + len(b.trades)
    assert sum(e.pnl for e in c.episodes) == pytest.approx(c.nav[-1] - c.initial_cash, rel=1e-9)


def test_portfolio_api(lab, client):  # noqa: F811
    _, pid = lab
    s1, s2 = _create(GOOD, pid), _create(BAD, pid)
    assert client.post("/api/quant/portfolios", json={"name": "x", "members": [{"strategy_id": s1, "weight": 1}]}
                       ).status_code == 400                                  # 至少 2 个
    r = client.post("/api/quant/portfolios", json={"name": "均衡", "members": [
        {"strategy_id": s1, "weight": 3}, {"strategy_id": s2, "weight": 1}]})
    assert r.status_code == 200 and [m["weight"] for m in r.json()["members"]] == [0.75, 0.25]
    pf = client.get(f"/api/quant/portfolios/{r.json()['id']}").json()
    rep = pf["report"]
    assert rep["dev"]["days"] > 100 and len(rep["members"]) == 2
    assert rep["correlation"]["matrix"][0][0] == 1.0 and rep["avg_correlation"] is not None
    assert client.get("/api/quant/portfolios").json()[0]["name"] == "均衡"


SPEC_A = {"universe": {"types": ["stock", "etf"], "min_listed_days": 0, "min_amount": 0},
          "factors": [{"name": "momentum", "params": {"window": 5}}],
          "selection": {"top_n": 2}, "weighting": {"max_weight": 0.45}, "rebalance": {"every_days": 3}}
SPEC_B = {**SPEC_A, "factors": [{"name": "momentum", "params": {"window": 5}, "direction": "low"}],
          "rebalance": {"every_days": 5}}


def _strategy(s, name, spec) -> int:
    st = QuantStrategyRow(name=name, style="x", universe_name="cn_sse50_etf", benchmark="000016.SH", settings={})
    s.add(st)
    s.flush()
    v = QuantVersionRow(strategy_id=st.id, number=1, source="initial", title="初始", spec=spec, status="champion")
    s.add(v)
    s.flush()
    st.champion_id = v.id
    return st.id


async def test_portfolio_player_updates_members_on_their_own_schedule(arena):
    with get_sessionmaker()() as s:
        a, b = _strategy(s, "甲", SPEC_A), _strategy(s, "乙", SPEC_B)
        pf = QuantPortfolioRow(name="组合", members=[{"strategy_id": a, "weight": 0.5},
                                                   {"strategy_id": b, "weight": 0.5}])
        s.add(pf)
        s.flush()
        agent = AgentRow(name="组合·测试", kind="quant", quant_portfolio_id=pf.id)
        s.add(agent)
        rs = s.scalars(select(RuleSetRow).where(RuleSetRow.name == "cn_long_only")).one()
        track = create_track(s, 1, name="组合跟踪", universe_name="cn_sse50_etf", rule_set_id=rs.id,
                             initial_cash=Decimal("1000000"), mode="replay", replay_start=date(2026, 9, 1))
        s.flush()
        acc = enroll(s, track, agent.id)
        start_track(s, track, cst(2026, 10, 2, 10))
        s.commit()
        track_id, acc_id = track.id, acc.id
    assert "回放完成" in await cycle.run_replay(track_id, lambda m: None)
    with get_sessionmaker()() as s:
        decs = list(s.scalars(select(DecisionRow).where(DecisionRow.account_id == acc_id).order_by(DecisionRow.id)))
        since_a = [d.parsed["members"][str(a)]["since"] for d in decs[:7]]
        since_b = [d.parsed["members"][str(b)]["since"] for d in decs[:7]]
        assert since_a == [0, 1, 2, 0, 1, 2, 0] and since_b == [0, 1, 2, 3, 4, 0, 1]
        assert decs[0].parsed["rebalance"] and "多风格组合「组合」" in decs[0].analysis
        assert len(decs[0].parsed["targets"]) >= 2
        assert reconcile(s, s.get(AccountRow, acc_id)) == 0
        # 组合回测与组合选手使用同一规则（单账户、成员间定期再平衡），对账应一致
        from app.quant.reconcile import reconcile as recon

        r = recon(s, acc_id)
        assert r["status"] in ("一致", "有偏差") and abs(r["gap"]) < 0.02 and r["holdings_overlap"] > 0.8, r


def test_portfolio_min_trade_floor_and_new_positions_free():
    spec = parse_spec(GOOD)
    t = PortfolioTargeter([(spec, 0.2)] * 5)
    assert t.min_trade == max(spec.rebalance.min_trade_pct * 0.2, PORTFOLIO_MIN_TRADE)
    assert t.new_free and SingleTargeter(spec).new_free
