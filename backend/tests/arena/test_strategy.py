"""策略笔记与定期复盘（PRD 4.4.4）。"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.agent.parse import DecisionParseError
from app.agent.reflection import MAX_STRATEGY_CHARS, parse_reflection
from app.arena import cycle, strategy
from app.arena.lifecycle import create_track, enroll, start_track
from app.db.session import get_sessionmaker
from app.models import (
    AccountRow,
    AgentRow,
    DecisionRow,
    ReflectionRow,
    RoundRow,
    RuleSetRow,
    StrategyVersionRow,
)
from tests.arena.conftest import FakeProvider, cst


def _setup_agent(**kw) -> None:
    with get_sessionmaker()() as s:
        agent = s.scalars(select(AgentRow).where(AgentRow.kind == "llm")).one()
        for k, v in kw.items():
            setattr(agent, k, v)
        s.commit()


def _replay_track(start=date(2026, 9, 1), end: date | None = None) -> tuple[int, int]:
    with get_sessionmaker()() as s:
        rs = s.scalars(select(RuleSetRow).where(RuleSetRow.name == "cn_long_only")).one()
        track = create_track(s, 1, name="进化", universe_name="cn_sse50_etf", rule_set_id=rs.id,
                             initial_cash=Decimal("1000000"), mode="replay", replay_start=start, end_date=end)
        agent = s.scalars(select(AgentRow).where(AgentRow.kind == "llm")).one()
        acc = enroll(s, track, agent.id)
        enroll(s, track, s.scalars(select(AgentRow).where(AgentRow.baseline_type == "buy_hold_index")).one().id)
        start_track(s, track, cst(2026, 10, 2, 10))
        s.commit()
        return track.id, acc.id


def _days(a: date, b: date) -> list[date]:
    return [d for d in FakeProvider().trading_days() if a <= d <= b]


# ---------------------------------------------------------------- 解析


def test_parse_reflection():
    r = parse_reflection(json.dumps({"review": ["追高亏损", "止损太慢"], "changes": "加入止损",
                                     "strategy": "跌破成本 8% 止损"}, ensure_ascii=False))
    assert r.strategy == "跌破成本 8% 止损" and r.review == "- 追高亏损\n- 止损太慢"
    with pytest.raises(DecisionParseError, match="strategy"):
        parse_reflection('{"review": "x"}')
    with pytest.raises(DecisionParseError, match="超过"):
        parse_reflection(json.dumps({"strategy": "长" * (MAX_STRATEGY_CHARS + 1)}))


def test_parse_reflection_structured_changes_and_limit():
    change = {"type": "删除", "rule": "RSI>80 减仓", "reason": "5 笔全部卖早", "turnover_impact": "减少交易"}
    r = parse_reflection(json.dumps({"review": "x", "changes": [change], "strategy": "s"}, ensure_ascii=False), 3)
    assert r.changes == "1. 删除：RSI>80 减仓（原因：5 笔全部卖早；换手影响：减少交易）"
    assert parse_reflection('{"changes": [], "strategy": "s"}', 3).changes == "无修改"
    four = json.dumps({"changes": [change] * 4, "strategy": "s"}, ensure_ascii=False)
    with pytest.raises(DecisionParseError, match="最多修改 3 条"):
        parse_reflection(four, 3)
    assert parse_reflection(four, None).strategy == "s"          # 写第一版时不限条数


# ---------------------------------------------------------------- 回放中的定期复盘


async def test_replay_reflects_every_n_days_without_lookahead(arena):
    _, llm = arena
    _setup_agent(reflect_enabled=True, reflect_every=5, initial_strategy="初始策略：只买 ETF")
    track_id, acc_id = _replay_track()
    with get_sessionmaker()() as s:
        v1 = strategy.current_version(s, acc_id)
        assert (v1.version, v1.source, v1.content) == (1, "initial", "初始策略：只买 ETF")

    await cycle.run_replay(track_id)
    days = _days(date(2026, 9, 1), date(2026, 9, 30))
    expected = days[5::5]            # 首日只决策；之后每满 5 个交易日的结算复盘一次
    assert len(llm.reflections) == len(expected)

    with get_sessionmaker()() as s:
        refl = list(s.scalars(select(ReflectionRow).where(ReflectionRow.account_id == acc_id)
                              .order_by(ReflectionRow.id)))
        assert [r.data_date for r in refl] == expected
        assert all(r.status == "ok" and r.trigger == "schedule" for r in refl)
        assert [r.period_start for r in refl] == [days[0], *expected[:-1]]   # 区间首尾相接
        assert [(r.base_version, r.new_version) for r in refl] == [(i + 1, i + 2) for i in range(len(refl))]
        assert refl[0].stats["days"] == 5 and refl[0].stats["trades"] > 0
        # 首个区间期初空仓：不交易的对照收益为 0，交易增减等于区间收益
        assert refl[0].stats["hold_return"] == "+0.00%"
        assert refl[0].stats["trading_alpha"] == refl[0].stats["period_return"]
        assert refl[1].stats["hold_return"].endswith("%") and refl[1].stats["turnover"].endswith("%")
        assert "持仓一直不动" in refl[0].prompt_user and "最多修改 3 条" in refl[0].prompt_system
        # 复盘只用到复盘日为止的数据
        for r in refl:
            later = [str(d) for d in days if d > r.data_date]
            assert not any(d in r.prompt_user for d in later)
            assert str(r.data_date) in r.prompt_user
        assert "初始策略：只买 ETF" in refl[0].prompt_user
        assert "基线·" in refl[0].prompt_user and "（你）" in refl[0].prompt_user     # 同赛道对比

        versions = list(s.scalars(select(StrategyVersionRow).where(StrategyVersionRow.account_id == acc_id)))
        assert [v.source for v in versions] == ["initial"] + ["reflection"] * len(expected)

        # 每条决策记录使用的版本：复盘当天的决策已使用新版本
        rows = s.execute(select(DecisionRow, RoundRow).join(RoundRow, DecisionRow.round_id == RoundRow.id)
                         .where(DecisionRow.account_id == acc_id).order_by(RoundRow.data_date)).all()
        used = {rnd.data_date: dec.strategy_version for dec, rnd in rows}
        assert used[days[0]] == 1 and used[days[4]] == 1
        assert used[expected[0]] == 2 and used[days[-1]] == 1 + len(expected)
        dec = next(dec for dec, rnd in rows if rnd.data_date == expected[0])
        assert "第 2 版" in dec.prompt_system and "策略第 1 版：单只不超过 15%" in dec.prompt_system


async def test_reflection_disabled_and_unchanged_strategy(arena):
    _, llm = arena
    track_id, acc_id = _replay_track(end=date(2026, 9, 15))
    await cycle.run_replay(track_id)
    assert llm.reflections == []
    with get_sessionmaker()() as s:
        assert strategy.current_version(s, acc_id) is None
        dec = s.scalars(select(DecisionRow).where(DecisionRow.account_id == acc_id)).first()
        assert dec.strategy_version is None and "策略笔记" not in dec.prompt_system

    # 开启后手动复盘；AI 原样返回策略时不生成新版本
    _setup_agent(reflect_enabled=True, reflect_every=5)
    with get_sessionmaker()() as s:
        strategy.add_version(s, acc_id, "保持现状", "manual")
        s.commit()
    llm.reflection_content = json.dumps({"review": "一切正常", "changes": "无修改", "strategy": "保持现状"},
                                        ensure_ascii=False)
    msg = await strategy.reflect_now(acc_id)
    assert "未修改" in msg
    with get_sessionmaker()() as s:
        r = s.scalars(select(ReflectionRow)).one()
        assert (r.trigger, r.data_date, r.period_start, r.new_version) == ("manual", date(2026, 9, 15),
                                                                           date(2026, 9, 1), None)
        assert strategy.current_version(s, acc_id).version == 1


async def test_too_many_changes_rejected_then_retried(arena):
    _, llm = arena
    _setup_agent(reflect_enabled=True, reflect_every=5, initial_strategy="v1")
    change = {"type": "新增", "rule": "r", "reason": "x", "turnover_impact": "增加"}
    replies = iter([json.dumps({"changes": [change] * 5, "strategy": "v2"}, ensure_ascii=False),
                    json.dumps({"changes": [change], "strategy": "v2"}, ensure_ascii=False)])
    original = llm.chat

    async def chat(req):
        if "<performance>" in req.messages[0]["content"]:
            llm.reflection_content = next(replies)
        return await original(req)

    llm.chat = chat
    track_id, acc_id = _replay_track(end=date(2026, 9, 9))
    await cycle.run_replay(track_id)
    with get_sessionmaker()() as s:
        r = s.scalars(select(ReflectionRow)).one()
        assert (r.status, r.attempts, r.new_version) == ("ok", 2, 2)
        assert "每次最多修改 3 条" in llm.reflections[-1].messages[-1]["content"]


async def test_failed_reflection_keeps_strategy(arena):
    _, llm = arena
    _setup_agent(reflect_enabled=True, reflect_every=3, initial_strategy="v1")
    llm.reflection_content = "我觉得应该继续持有"          # 不是 JSON，重试后仍失败
    track_id, acc_id = _replay_track(end=date(2026, 9, 10))
    await cycle.run_replay(track_id)
    with get_sessionmaker()() as s:
        refl = list(s.scalars(select(ReflectionRow)))
        assert refl and all(r.status == "invalid" and r.attempts == 2 for r in refl)
        assert strategy.current_version(s, acc_id).version == 1
        assert [r.data_date for r in refl] == _days(date(2026, 9, 1), date(2026, 9, 10))[3::3]


# ---------------------------------------------------------------- 接口


async def test_strategy_api(arena, client):
    _, llm = arena
    _setup_agent(reflect_enabled=True, reflect_every=5, initial_strategy="v1 策略")
    track_id, acc_id = _replay_track(end=date(2026, 9, 10))
    await cycle.run_replay(track_id)

    data = client.get(f"/api/accounts/{acc_id}/strategy").json()
    assert data["reflect_enabled"] and [v["version"] for v in data["versions"]] == [2, 1]
    assert data["reflections"][0]["new_version"] == 2 and data["reflections"][0]["stats"]["days"] == 5
    detail = client.get(f"/api/reflections/{data['reflections'][0]['id']}").json()
    assert "<performance>" in detail["prompt_user"]

    r = client.put(f"/api/accounts/{acc_id}/strategy", json={"content": "手动改写的策略"})
    assert r.status_code == 200 and r.json()["version"] == 3 and r.json()["source"] == "manual"
    r = client.post(f"/api/accounts/{acc_id}/strategy/rollback", json={"version": 1})
    assert r.json()["version"] == 4 and r.json()["content"] == "v1 策略"
    assert client.post(f"/api/accounts/{acc_id}/strategy/rollback", json={"version": 99}).status_code == 400

    board = client.get(f"/api/tracks/{track_id}/leaderboard").json()
    mine = next(b for b in board if b["account_id"] == acc_id)
    assert mine["reflect_enabled"] and mine["strategy_version"] == 4

    with get_sessionmaker()() as s:
        base = s.scalars(select(AccountRow).where(AccountRow.track_id == track_id,
                                                  AccountRow.id != acc_id)).one()
    assert client.put(f"/api/accounts/{base.id}/strategy", json={"content": "x"}).status_code == 400

    agent_id = client.get(f"/api/accounts/{acc_id}").json()["agent"]["id"]
    agent = next(a for a in client.get("/api/agents").json() if a["id"] == agent_id)
    assert agent["reflect_enabled"] and agent["initial_strategy"] == "v1 策略"
