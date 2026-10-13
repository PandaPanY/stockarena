"""前视偏差防护（v1.0 子阶段 C）：知识截止日期风险标注、回放代码匿名化。"""

from __future__ import annotations

import json
import re
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.arena import cycle
from app.arena.anonymize import Anonymizer
from app.arena.lifecycle import ArenaError, create_track, enroll, start_track
from app.arena.queries import lookahead_risks
from app.db.session import get_sessionmaker
from app.market.types import Instrument
from app.models import AgentRow, DecisionRow, InstrumentRow, LLMProviderRow, OrderRow, RuleSetRow, TrackRow
from tests.arena.conftest import cst
from tests.arena.test_order_flow import ScriptedLLM

D = Decimal


def test_anonymizer_text_and_restore():
    insts = {"600519.SH": Instrument("600519.SH", "CN", "贵州茅台", "stock", "main", 100, D("0.01")),
             "688981.SH": Instrument("688981.SH", "CN", "中芯国际", "stock", "star", 200, D("0.01")),
             "510300.SH": Instrument("510300.SH", "CN", "沪深300ETF", "etf", "fund", 100, D("0.001"))}
    a = Anonymizer(insts, [date(2026, 9, d) for d in (1, 2, 3, 4)], date(2026, 9, 3))
    out = a.text("2026-09-03 收盘：600519.SH 贵州茅台、688981 中芯国际、沪深300ETF；"
                 "回顾 2026-09-01，2025-01-01；价格 1600519")
    assert out == "T 收盘：S01 主板股票、S02 科创板股票、ETF；回顾 T-2，(日期)；价格 1600519"
    assert a.restore('{"symbol": "s01"}, {"symbol": "E01"}') == '{"symbol": "600519.SH"}, {"symbol": "510300.SH"}'
    assert a.restore_args({"symbols": ["S02", "x"]}) == {"symbols": ["688981.SH", "x"]}


def _aliases(track_id: int) -> dict[str, str]:
    with get_sessionmaker()() as s:
        t = s.get(TrackRow, track_id)
        rows = s.scalars(select(InstrumentRow).where(InstrumentRow.symbol.in_(t.symbols)))
        insts = {r.symbol: Instrument(r.symbol, r.market, r.name, r.type, r.board, r.lot_size, r.tick) for r in rows}
    return Anonymizer(insts, [], date(2026, 9, 1)).alias


async def test_anonymized_replay_end_to_end(arena, monkeypatch):
    with get_sessionmaker()() as s:
        rs = s.scalars(select(RuleSetRow).where(RuleSetRow.name == "cn_long_only")).one()
        t = create_track(s, 1, name="匿名", universe_name="cn_sse50_etf", rule_set_id=rs.id,
                         initial_cash=D("1000000"), mode="replay", replay_start=date(2026, 9, 1),
                         end_date=date(2026, 9, 3), anonymize=True)
        enroll(s, t, s.scalars(select(AgentRow).where(AgentRow.kind == "llm")).one().id)
        start_track(s, t, cst(2026, 10, 2, 10))
        s.commit()
        track_id = t.id
    alias = _aliases(track_id)["600519.SH"]
    llm = ScriptedLLM([{"analysis": f"买入 {alias}", "targets": [{"symbol": alias, "weight": 0.2}]}])
    monkeypatch.setattr(cycle, "create_client", lambda cfg: llm)
    await cycle.run_replay(track_id)

    with get_sessionmaker()() as s:
        decs = list(s.scalars(select(DecisionRow)))
        for d in decs:
            text = d.prompt_system + d.prompt_user
            assert "600519" not in text and "贵州茅台" not in text
            assert not re.search(r"20\d\d-\d\d-\d\d", text)
            assert alias in d.prompt_user and "T+1" in d.prompt_user
        orders = list(s.scalars(select(OrderRow)))
        assert orders and all(o.symbol == "600519.SH" for o in orders)


def test_lookahead_risk_levels(arena):
    with get_sessionmaker()() as s:
        rs = s.scalars(select(RuleSetRow).where(RuleSetRow.name == "cn_long_only")).one()
        agent = s.scalars(select(AgentRow).where(AgentRow.kind == "llm")).one()
        base = s.scalars(select(AgentRow).where(AgentRow.kind == "baseline")).first()
        t = create_track(s, 1, name="回放", universe_name="cn_sse50_etf", rule_set_id=rs.id,
                         initial_cash=D("1000000"), mode="replay", replay_start=date(2026, 9, 1))
        enroll(s, t, agent.id)
        enroll(s, t, base.id)
        start_track(s, t, cst(2026, 10, 2, 10))
        provider = s.get(LLMProviderRow, agent.provider_id)
        [r] = lookahead_risks(s, t)                        # 基线选手不计
        assert r["level"] == "unknown" and r["replay_start"] == "2026-09-01"
        provider.knowledge_cutoff = date(2026, 12, 31)
        assert lookahead_risks(s, t)[0]["level"] == "high"
        provider.knowledge_cutoff = date(2025, 6, 30)
        assert lookahead_risks(s, t)[0]["level"] == "low"
        live = create_track(s, 1, name="实时", universe_name="cn_sse50_etf", rule_set_id=rs.id,
                            initial_cash=D("1000000"))
        assert lookahead_risks(s, live) == []
        with pytest.raises(ArenaError, match="匿名化只用于历史回放"):
            create_track(s, 1, name="x", universe_name="cn_sse50_etf", rule_set_id=rs.id,
                         initial_cash=D("1"), anonymize=True)
        assert json.dumps(lookahead_risks(s, t), ensure_ascii=False)
