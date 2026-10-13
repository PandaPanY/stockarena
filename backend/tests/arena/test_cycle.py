"""赛道日终流程端到端测试：伪造行情源与伪造 AI，不访问网络。"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.arena import cycle
from app.arena.accounts import reconcile
from app.arena.lifecycle import ArenaError, create_track, enroll, start_track
from app.db.session import get_sessionmaker
from app.market.types import CorporateAction
from app.models import (
    AccountRow,
    AgentRow,
    DecisionRow,
    FillRow,
    NavSnapshotRow,
    OrderRow,
    PositionRow,
    RoundRow,
    RuleSetRow,
    TrackRow,
)
from tests.arena.conftest import FakeLLM, cst


def _setup_track(now: datetime) -> int:
    with get_sessionmaker()() as s:
        rs = s.scalars(select(RuleSetRow).where(RuleSetRow.name == "cn_long_only")).one()
        track = create_track(s, 1, name="测试赛道", universe_name="cn_sse50_etf", rule_set_id=rs.id,
                             initial_cash=Decimal("100000"))
        for agent in s.scalars(select(AgentRow)):
            if agent.kind == "llm" or agent.baseline_type in ("buy_hold_index", "equal_weight"):
                enroll(s, track, agent.id)
        start_track(s, track, now)
        s.commit()
        return track.id


async def test_full_cycle_decide_fill_nav_and_catch_up(arena):
    provider, llm = arena
    track_id = _setup_track(cst(2026, 9, 30, 16))
    with get_sessionmaker()() as s:
        track = s.get(TrackRow, track_id)
        assert track.start_date == date(2026, 9, 30) and track.status == "running"
        assert len(track.symbols) == 3 + 10
        assert Decimal(track.rule_params["max_position_pct"]) == Decimal("0.30")

    # 9/30 收盘后：首轮决策，订单在国庆后首个交易日 10/8 撮合
    msg = await cycle.run_track_cycle(track_id, now=cst(2026, 9, 30, 16))
    assert "决策完成" in msg and "3/3" in msg
    with get_sessionmaker()() as s:
        orders = list(s.scalars(select(OrderRow)))
        assert orders and all(o.target_date == date(2026, 10, 8) for o in orders if o.status == "pending")
        llm_dec = s.scalars(select(DecisionRow).where(DecisionRow.status == "ok")).one()
        assert llm_dec.cost == Decimal("0.0036")          # 1000×2/1e6 + 200×8/1e6
        assert "贵州茅台" in llm_dec.prompt_user and llm_dec.reasoning == "思考过程"

    # 重复执行是幂等的
    assert "已完成" in await cycle.run_track_cycle(track_id, now=cst(2026, 9, 30, 17))
    assert llm.calls == 1

    # 10/8 收盘后：结算成交 + 第二轮决策
    msg = await cycle.run_track_cycle(track_id, now=cst(2026, 10, 8, 16))
    assert "决策完成" in msg
    with get_sessionmaker()() as s:
        fills = list(s.scalars(select(FillRow)))
        assert fills and all(f.trade_date == date(2026, 10, 8) for f in fills)
        for acc in s.scalars(select(AccountRow)):
            assert reconcile(s, acc) == 0                  # K2：流水与现金一致
            navs = list(s.scalars(select(NavSnapshotRow).where(NavSnapshotRow.account_id == acc.id)
                                  .order_by(NavSnapshotRow.date)))
            assert [n.date for n in navs] == [date(2026, 9, 30), date(2026, 10, 8)]
            assert navs[0].nav == Decimal("100000")
        positions = {p.symbol for p in s.scalars(select(PositionRow))}
        assert {"600519.SH", "510300.SH", "510050.SH"} <= positions
        # 第二轮 prompt 中带上一轮的执行反馈与记忆
        last = s.scalars(select(DecisionRow).where(DecisionRow.status == "ok").order_by(DecisionRow.id.desc())).first()
        assert "上一轮订单执行结果" in last.prompt_user and "看好茅台" in last.prompt_user

    # 停机三天后补跑：补结算 10/9、10/12，10/9 与 10/12 记为错过，只对 10/13 决策
    provider.dividends.append(CorporateAction("600519.SH", date(2026, 10, 12), cash_per_share=Decimal("2")))
    cycle._actions_refreshed.clear()
    msg = await cycle.run_track_cycle(track_id, now=cst(2026, 10, 13, 18))
    assert "2026-10-13 决策完成" in msg
    with get_sessionmaker()() as s:
        rounds = {r.data_date: r.status for r in s.scalars(select(RoundRow))}
        assert rounds[date(2026, 10, 9)] == "missed" and rounds[date(2026, 10, 13)] == "done"
        # 补结算后不应留下过期未处理的订单：仍在等待的订单只能是最新一轮（10/14 撮合）的
        pending = list(s.scalars(select(OrderRow).where(OrderRow.status == "pending")))
        assert all(o.target_date == date(2026, 10, 14) for o in pending)
        assert s.get(TrackRow, track_id).last_settled_date == date(2026, 10, 13)
        for acc in s.scalars(select(AccountRow)):
            assert reconcile(s, acc) == 0
        from app.models import CashLedgerRow
        assert s.scalars(select(CashLedgerRow).where(CashLedgerRow.type == "dividend")).first() is not None


async def test_waits_for_close_when_started_intraday(arena):
    track_id = _setup_track(cst(2026, 10, 8, 10))
    with get_sessionmaker()() as s:
        assert s.get(TrackRow, track_id).start_date == date(2026, 10, 8)
    msg = await cycle.run_track_cycle(track_id, now=cst(2026, 10, 8, 11))
    assert "等待" in msg


async def test_paused_track_settles_but_does_not_decide(arena):
    _, llm = arena
    track_id = _setup_track(cst(2026, 9, 30, 16))
    await cycle.run_track_cycle(track_id, now=cst(2026, 9, 30, 16))
    with get_sessionmaker()() as s:
        s.get(TrackRow, track_id).status = "paused"
        s.commit()
    msg = await cycle.run_track_cycle(track_id, now=cst(2026, 10, 8, 16))
    assert "不发起决策" in msg
    assert llm.calls == 1
    with get_sessionmaker()() as s:
        assert s.scalar(select(func.count()).select_from(FillRow)) > 0


async def test_invalid_llm_output_retries_then_holds(arena, monkeypatch):
    bad = FakeLLM("我觉得应该买茅台")
    monkeypatch.setattr(cycle, "create_client", lambda cfg: bad)
    track_id = _setup_track(cst(2026, 9, 30, 16))
    await cycle.run_track_cycle(track_id, now=cst(2026, 9, 30, 16))
    assert bad.calls == 2
    with get_sessionmaker()() as s:
        dec = s.scalars(select(DecisionRow).where(DecisionRow.prompt_user.is_not(None))).one()
        assert dec.status == "invalid" and dec.attempts == 2
        assert s.scalars(select(OrderRow).where(OrderRow.decision_id == dec.id)).first() is None


def test_cannot_enroll_after_start(arena):
    track_id = _setup_track(cst(2026, 9, 30, 16))
    with get_sessionmaker()() as s:
        agent = s.scalars(select(AgentRow).where(AgentRow.baseline_type == "random")).one()
        with pytest.raises(ArenaError):
            enroll(s, s.get(TrackRow, track_id), agent.id)
