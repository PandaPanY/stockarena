"""历史回放：从过去日期逐日快进到最近收盘日，之后转为实时。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.arena import cycle
from app.arena.accounts import reconcile
from app.arena.lifecycle import ArenaError, create_track, enroll, start_track
from app.db.session import get_sessionmaker
from app.models import AccountRow, AgentRow, NavSnapshotRow, RoundRow, RuleSetRow, TrackRow
from tests.arena.conftest import FakeProvider, cst


def _replay_track(start: date, now, end: date | None = None) -> int:
    with get_sessionmaker()() as s:
        rs = s.scalars(select(RuleSetRow).where(RuleSetRow.name == "cn_long_only")).one()
        track = create_track(s, 1, name="回放", universe_name="cn_sse50_etf", rule_set_id=rs.id,
                             initial_cash=Decimal("100000"), mode="replay", replay_start=start, end_date=end)
        for agent in s.scalars(select(AgentRow)):
            if agent.kind == "llm" or agent.baseline_type == "buy_hold_index":
                enroll(s, track, agent.id)
        start_track(s, track, now)
        s.commit()
        return track.id


def _trading_days(a: date, b: date) -> list[date]:
    return [d for d in FakeProvider().trading_days() if a <= d <= b]


async def test_replay_runs_every_day_then_hands_over_to_live(arena):
    _, llm = arena
    # 国庆假期中（10/2）启动，从 9/1 回放到 9/30
    track_id = _replay_track(date(2026, 9, 1), cst(2026, 10, 2, 10))
    with get_sessionmaker()() as s:
        t = s.get(TrackRow, track_id)
        assert (t.start_date, t.replay_until, t.mode) == (date(2026, 9, 1), date(2026, 9, 30), "replay")
        assert cycle.replay_pending(t)

    progress: list[str] = []
    msg = await cycle.run_replay(track_id, progress.append)
    days = _trading_days(date(2026, 9, 1), date(2026, 9, 30))
    assert "回放完成" in msg and f"（{len(days)} 个交易日）" in msg
    assert progress[-1].endswith(f"（{len(days)}/{len(days)}）")
    assert llm.calls == len(days)

    with get_sessionmaker()() as s:
        t = s.get(TrackRow, track_id)
        assert t.last_settled_date == date(2026, 9, 30) and t.status == "running"
        assert not cycle.replay_pending(t)
        rounds = list(s.scalars(select(RoundRow).where(RoundRow.track_id == track_id)))
        assert [r.data_date for r in rounds] == days                       # 每个交易日一轮，没有错过
        assert all(r.trigger == "replay" and r.status == "done" for r in rounds)
        for acc in s.scalars(select(AccountRow).where(AccountRow.track_id == track_id)):
            navs = [n.date for n in s.scalars(select(NavSnapshotRow).where(NavSnapshotRow.account_id == acc.id))]
            assert navs == days
            assert reconcile(s, acc) == 0

    # 回放完成后转为实时：10/8 收盘后的定时任务正常结算与决策
    msg = await cycle.run_track_cycle(track_id, now=cst(2026, 10, 8, 16))
    assert "2026-10-08 决策完成" in msg


async def test_replay_resumes_after_interruption(arena, monkeypatch):
    track_id = _replay_track(date(2026, 9, 1), cst(2026, 10, 2, 10))
    calls = {"n": 0}
    original = cycle.run_track_cycle

    async def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 4:
            return "执行失败：模拟网络中断"
        return await original(*args, **kwargs)

    monkeypatch.setattr(cycle, "run_track_cycle", flaky)
    msg = await cycle.run_replay(track_id)
    assert "中断" in msg
    with get_sessionmaker()() as s:
        assert s.get(TrackRow, track_id).last_settled_date == date(2026, 9, 3)

    monkeypatch.setattr(cycle, "run_track_cycle", original)
    # 定时任务发现回放未完成，继续回放而不是直接跳到最新一天
    await cycle.run_market_tracks("CN")
    with get_sessionmaker()() as s:
        assert s.get(TrackRow, track_id).last_settled_date == date(2026, 9, 30)
        assert not any(r.status == "missed" for r in s.scalars(select(RoundRow)))


async def test_replay_stops_when_paused(arena, monkeypatch):
    track_id = _replay_track(date(2026, 9, 1), cst(2026, 10, 2, 10))
    original = cycle.run_track_cycle

    async def pause_after_first(track_id_, *args, **kwargs):
        msg = await original(track_id_, *args, **kwargs)
        with get_sessionmaker()() as s:
            s.get(TrackRow, track_id_).status = "paused"
            s.commit()
        return msg

    monkeypatch.setattr(cycle, "run_track_cycle", pause_after_first)
    assert "回放已停止（进度 1/" in await cycle.run_replay(track_id)


def test_replay_with_end_date_and_validation(arena):
    track_id = _replay_track(date(2026, 9, 5), cst(2026, 10, 2, 10), end=date(2026, 9, 15))
    with get_sessionmaker()() as s:
        t = s.get(TrackRow, track_id)
        assert t.start_date == date(2026, 9, 7)        # 9/5 是周六，顺延到下一个交易日
        assert t.replay_until == date(2026, 9, 15)
    with pytest.raises(ArenaError, match="早于"):
        _replay_track(date(2026, 9, 30), cst(2026, 10, 2, 10))
    with get_sessionmaker()() as s, pytest.raises(ArenaError, match="开始日期"):
        rs = s.scalars(select(RuleSetRow)).first()
        create_track(s, 1, name="x", universe_name="cn_sse50_etf", rule_set_id=rs.id,
                     initial_cash=Decimal("1"), mode="replay")
