"""量化选手（PRD 3.8 每日信号与实时跟踪）：策略冠军版本在赛道中由交易引擎执行，按调仓周期调仓，冠军更新后切换。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select

from app.agent.context import MarketSnapshot
from app.arena import cycle, deps
from app.arena.accounts import load_state, reconcile
from app.arena.lifecycle import create_track, enroll, start_track
from app.db.session import get_sessionmaker
from app.market.universe import index_instrument
from app.models import AccountRow, AgentRow, DecisionRow, FillRow, RuleSetRow
from app.models.quant import QuantStrategyRow, QuantVersionRow
from app.quant import live
from tests.arena.conftest import cst

SPEC = {"universe": {"types": ["stock", "etf"], "min_listed_days": 0, "min_amount": 0},
        "factors": [{"name": "momentum", "params": {"window": 5}}],
        "selection": {"top_n": 2}, "weighting": {"max_weight": 0.45}, "rebalance": {"every_days": 3}}


def _strategy(s) -> QuantStrategyRow:
    st = QuantStrategyRow(name="测试量化", style="momentum", universe_name="cn_sse50_etf", benchmark="000016.SH",
                          settings={})
    s.add(st)
    s.flush()
    v = QuantVersionRow(strategy_id=st.id, number=1, source="initial", title="初始", spec=SPEC, status="champion")
    s.add(v)
    s.flush()
    st.champion_id = v.id
    return st


async def test_quant_player_rebalances_on_schedule(arena):
    with get_sessionmaker()() as s:
        st = _strategy(s)
        agent = AgentRow(name="量化·测试", kind="quant", quant_strategy_id=st.id)
        s.add(agent)
        rs = s.scalars(select(RuleSetRow).where(RuleSetRow.name == "cn_long_only")).one()
        track = create_track(s, 1, name="跟踪", universe_name="cn_sse50_etf", rule_set_id=rs.id,
                             initial_cash=Decimal("1000000"), mode="replay", replay_start=date(2026, 9, 1))
        s.flush()
        acc = enroll(s, track, agent.id)
        start_track(s, track, cst(2026, 10, 2, 10))
        s.commit()
        track_id, acc_id, sid = track.id, acc.id, st.id

    msg = await cycle.run_replay(track_id, lambda m: None)
    assert "回放完成" in msg
    with get_sessionmaker()() as s:
        decs = list(s.scalars(select(DecisionRow).where(DecisionRow.account_id == acc_id).order_by(DecisionRow.id)))
        flags = [d.parsed["rebalance"] for d in decs]
        assert flags[:7] == [True, False, False, True, False, False, True]          # 每 3 个交易日调仓
        assert all(d.status == "baseline" and d.parsed["quant_version"] == 1 for d in decs)
        assert "量化策略「测试量化」v1" in decs[0].analysis and "选中 2 个" in decs[0].analysis
        assert "选股理由" in decs[0].analysis and "名，动量(5,0)" in decs[0].analysis
        assert s.scalars(select(FillRow).where(FillRow.account_id == acc_id)).first() is not None
        acc = s.get(AccountRow, acc_id)
        assert reconcile(s, acc) == 0
        state = load_state(s, acc)
        assert 1 <= len(state.positions) <= 2

        # 冠军更新后的下一轮立即按新版本调仓
        st = s.get(QuantStrategyRow, sid)
        v2 = QuantVersionRow(strategy_id=sid, number=2, source="ai", title="新版", status="champion",
                             spec={**SPEC, "selection": {"top_n": 1}})
        s.add(v2)
        s.flush()
        st.champion_id = v2.id
        s.commit()
    snap = MarketSnapshot(date(2026, 9, 30), date(2026, 10, 8), "", "", {}, {}, {})
    with get_sessionmaker()() as s:
        state = load_state(s, s.get(AccountRow, acc_id))
    dec = live.decide_quant(sid, acc_id, state, snap)
    assert dec.extra["quant_version"] == 2 and dec.extra["rebalance"] and "冠军版本更新" in dec.analysis
    assert len(dec.targets) == 1


def test_signal_with_capital(arena):
    with get_sessionmaker()() as s:
        st = _strategy(s)
        s.commit()
        svc = deps.market_service(s, "CN")
        insts = deps.universe_resolver().resolve("cn_sse50_etf")
        bench = index_instrument("000016.SH", "上证50")
        svc.upsert_instruments([*insts, bench])
        svc.update_bars([*insts, bench], until=date(2026, 9, 30), history_start=date(2026, 3, 1))
        s.commit()
        sig = live.signal(s, st, capital=1_000_000)
    assert sig["date"] == "2026-09-30" and sig["version"] == 1 and len(sig["holdings"]) == 2
    for h in sig["holdings"]:
        assert h["shares"] % 100 == 0 and h["weight"] <= 0.45
    assert sig["cash_weight"] >= 0.1


def test_follow_creates_live_track_and_tracking_api(arena, client):
    with get_sessionmaker()() as s:
        sid = _strategy(s).id
        s.commit()
    r = client.post(f"/api/quant/strategies/{sid}/follow", json={"initial_cash": 500000})
    assert r.status_code == 200
    acc_id = r.json()["account_id"]
    assert client.post(f"/api/quant/strategies/{sid}/follow", json={}).status_code == 400   # 不能重复跟踪
    t = client.get(f"/api/quant/strategies/{sid}/tracking").json()
    assert t["account_id"] == acc_id and t["track_status"] == "running" and t["initial_cash"] == 500000
    with get_sessionmaker()() as s:
        acc = s.get(AccountRow, acc_id)
        agents = [s.get(AgentRow, a.agent_id) for a in s.scalars(select(AccountRow).where(
            AccountRow.track_id == acc.track_id))]
        assert {a.kind for a in agents} == {"quant", "baseline"}
        assert s.get(QuantStrategyRow, sid).follow_account_id == acc_id
