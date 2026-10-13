"""一致性评估与重复实验（v1.0 子阶段 E）。"""

from __future__ import annotations

import json
from datetime import date

import pytest
from sqlalchemy import select

from app.agent.parse import DecisionParseError
from app.arena import consistency, cycle
from app.arena.consistency import parse_verdict
from app.db.session import get_sessionmaker
from app.llm import ChatResponse
from app.models import AccountRow, AgentRow, ConsistencyCheckRow, LLMProviderRow
from tests.arena.conftest import cst
from tests.arena.test_replay import _replay_track


class Judge:
    def __init__(self):
        self.prompts = []

    async def chat(self, req):
        self.prompts.append(req.messages[0]["content"])
        verdict = "部分一致" if len(self.prompts) == 1 else "一致"
        return ChatResponse(content=json.dumps({"verdict": verdict, "reason": "测试"}, ensure_ascii=False),
                            tokens_in=50, tokens_out=10)


def test_parse_verdict():
    assert parse_verdict('{"verdict": "不一致", "reason": "说买实卖"}').verdict == "inconsistent"
    assert parse_verdict('{"verdict": "Consistent"}').verdict == "consistent"
    with pytest.raises(DecisionParseError):
        parse_verdict('{"verdict": "还行"}')


async def test_consistency_checks_and_metrics(arena, client, monkeypatch):
    track_id = _replay_track(date(2026, 9, 1), cst(2026, 10, 2, 10), end=date(2026, 9, 8))
    await cycle.run_replay(track_id)
    judge = Judge()
    monkeypatch.setattr(consistency, "create_client", lambda cfg: judge)
    with get_sessionmaker()() as s:
        provider_id = s.scalars(select(LLMProviderRow.id)).first()

    msg = await consistency.run_checks(track_id, provider_id, "judge-model", sample=3)
    assert "3/3 条有效" in msg
    assert "<decision>" in judge.prompts[0] and "看好茅台" in judge.prompts[0] and "订单：" in judge.prompts[0]
    with get_sessionmaker()() as s:
        rows = list(s.scalars(select(ConsistencyCheckRow)))
        assert sorted(r.score for r in rows) == [0.5, 1.0, 1.0]
    # 已评估过的决策不重复评估：5 轮决策中只剩 2 条
    assert "2/2" in await consistency.run_checks(track_id, provider_id, "judge-model", sample=3)
    assert "没有需要评估" in await consistency.run_checks(track_id, provider_id, "judge-model", sample=3)
    with get_sessionmaker()() as s:
        assert len(list(s.scalars(select(ConsistencyCheckRow)))) == 5

    data = client.get(f"/api/tracks/{track_id}/consistency").json()
    [acc] = data["accounts"]
    assert acc["checked"] == 5 and 0.5 < acc["rate"] < 1 and acc["judge_model"] == "judge-model"
    assert len(data["items"]) == 5 and data["running"] is False
    board = client.get(f"/api/tracks/{track_id}/leaderboard").json()
    llm_row = next(b for b in board if b["kind"] == "llm")
    assert llm_row["ai"]["consistency_checked"] == 5 and llm_row["ai"]["consistency_rate"] == acc["rate"]


async def test_clone_and_repeat_statistics(arena, client):
    first = _replay_track(date(2026, 9, 1), cst(2026, 10, 2, 10), end=date(2026, 9, 8))
    r = client.post(f"/api/tracks/{first}/clone")
    assert r.status_code == 200, r.text
    copy = r.json()
    assert copy["status"] == "draft" and copy["name"].endswith("（副本 1）")
    assert copy["mode"] == "replay" and copy["end_date"] == "2026-09-08" and len(copy["accounts"]) == 2
    assert client.post(f"/api/tracks/{copy['id']}/start").status_code == 200
    await cycle.run_replay(first)
    await cycle.run_replay(copy["id"])

    with get_sessionmaker()() as s:
        llm = s.scalars(select(AgentRow).where(AgentRow.kind == "llm")).one()
        ids = [a.id for a in s.scalars(select(AccountRow).where(AccountRow.agent_id == llm.id))]
    data = client.get("/api/compare", params={"account_ids": ",".join(map(str, ids))}).json()
    [rep] = data["repeats"]
    assert rep["runs"] == 2 and rep["cumulative_return"]["std"] >= 0
    assert rep["cumulative_return"]["min"] <= rep["cumulative_return"]["mean"] <= rep["cumulative_return"]["max"]
