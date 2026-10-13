"""离线进化实验（v1.0 子阶段 G）。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select

from app.arena import experiment
from app.db.session import get_sessionmaker
from app.models import AccountRow, AgentRow, ExperimentRow, ReflectionRow, RuleSetRow, StrategyVersionRow, TrackRow


def _exp(s, **kw) -> ExperimentRow:
    rs = s.scalars(select(RuleSetRow).where(RuleSetRow.name == "cn_long_only")).one()
    agent = s.scalars(select(AgentRow).where(AgentRow.kind == "llm")).one()
    base = dict(user_id=1, name="实验", agent_id=agent.id, universe_name="cn_sse50_etf", rule_set_id=rs.id,
                initial_cash=Decimal("1000000"), train_start=date(2026, 9, 1), train_end=date(2026, 9, 14),
                valid_start=date(2026, 9, 15), valid_end=date(2026, 9, 22), generations=2, reflect_every=3)
    return ExperimentRow(**{**base, **kw})


def test_validation(arena):
    with get_sessionmaker()() as s:
        import pytest

        from app.arena.lifecycle import ArenaError

        with pytest.raises(ArenaError, match="验证区间必须在训练区间之后"):
            experiment.validate(s, _exp(s, valid_start=date(2026, 9, 10)))
        with pytest.raises(ArenaError, match="至少一周"):
            experiment.validate(s, _exp(s, train_end=date(2026, 9, 3)))
        experiment.validate(s, _exp(s))


async def test_experiment_runs_generations(arena, client):
    _, llm = arena
    with get_sessionmaker()() as s:
        agent = s.scalars(select(AgentRow).where(AgentRow.kind == "llm")).one()
        agent.initial_strategy = "初始策略"
        agent.reflect_enabled = False                  # 实验在账户级开启复盘，不依赖选手设置
        e = _exp(s)
        s.add(e)
        s.commit()
        exp_id = e.id
    msg = await experiment.run(exp_id)
    assert msg == "实验完成：2 代"

    with get_sessionmaker()() as s:
        e = s.get(ExperimentRow, exp_id)
        assert e.status == "done" and [r["generation"] for r in e.results] == [0, 1, 2]
        assert e.results[0]["train"] is None and e.results[0]["strategy"] == "初始策略"
        g1, g2 = e.results[1], e.results[2]
        assert g1["train"]["reflections"] >= 2 and g1["valid"]["reflections"] == 0       # 验证区间策略固定
        # 第 2 代从第 1 代训练结束时的策略出发
        assert g1["strategy"] == g1["train"]["final_strategy"] != "初始策略"
        train2 = s.get(TrackRow, g2["train"]["track_id"])
        acc = s.scalars(select(AccountRow).where(AccountRow.track_id == train2.id)).one()
        first = s.scalars(select(StrategyVersionRow).where(StrategyVersionRow.account_id == acc.id)
                          .order_by(StrategyVersionRow.version)).first()
        assert first.content == g1["strategy"] and first.source == "initial"
        assert all(r["valid"]["days"] > 0 for r in e.results)
        tracks = list(s.scalars(select(TrackRow).where(TrackRow.name.like(f"进化实验#{exp_id} %"))))
        assert len(tracks) == 5 and all(t.status == "finished" for t in tracks)
        valid_accounts = [a.id for t in tracks if "验证" in t.name
                          for a in s.scalars(select(AccountRow).where(AccountRow.track_id == t.id))]
        assert not s.scalars(select(ReflectionRow).where(ReflectionRow.account_id.in_(valid_accounts))).all()

    data = client.get(f"/api/experiments/{exp_id}").json()
    assert data["status"] == "done" and len(data["results"]) == 3
    assert client.get("/api/experiments").json()[0]["id"] == exp_id
