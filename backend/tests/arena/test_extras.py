"""快照模式附带财报与新闻：防前视、回放不带新闻、数据源失败不影响决策（v1.0 子阶段 A）。"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pandas as pd
import pytest
from sqlalchemy import select

from app.arena import cycle, deps
from app.arena.lifecycle import create_track, enroll, start_track
from app.db.session import get_sessionmaker
from app.market import fundamentals as fmod
from app.market import news as nmod
from app.market.fundamentals import FundamentalRecord, cn_available_from, hk_available_from, parse_cn
from app.market.news import NewsItem
from app.models import AgentRow, DecisionRow, RoundRow, RuleSetRow
from tests.arena.conftest import cst


class FakeFundamentals:
    name = "fake"

    def __init__(self):
        self.calls = 0

    def fetch(self, symbol, market):
        self.calls += 1
        if symbol != "600519.SH":
            return []
        return [FundamentalRecord(symbol, date(2026, 6, 30), date(2026, 8, 31), {"revenue": 922.8, "roe": 16.8}),
                FundamentalRecord(symbol, date(2026, 9, 30), date(2026, 10, 31), {"revenue": 1400.0, "roe": 25.0})]


class FakeNews:
    def __init__(self, fail=False):
        self.fail = fail

    def fetch(self, symbol):
        if self.fail:
            raise ConnectionError("网络中断")
        return [NewsItem(symbol, datetime(2026, 9, 30, 2, 0, tzinfo=UTC), f"{symbol} 发布新品 <b>注意</b>", "摘要内容",
                         "测试", f"http://x/{symbol}/1"),
                NewsItem(symbol, datetime(2026, 10, 8, 2, 0, tzinfo=UTC), f"{symbol} 未来的新闻", "", "测试",
                         f"http://x/{symbol}/2")]


@pytest.fixture
def fakes(arena, monkeypatch):
    fund, news = FakeFundamentals(), FakeNews()
    monkeypatch.setattr(deps, "fundamentals_provider", lambda: fund)
    monkeypatch.setattr(deps, "news_provider", lambda: news)
    fmod.FundamentalsService._fetched.clear()
    nmod.NewsService._fetched.clear()
    return fund, news


def _track(options: dict, mode: str = "live") -> int:
    with get_sessionmaker()() as s:
        rs = s.scalars(select(RuleSetRow).where(RuleSetRow.name == "cn_long_only")).one()
        kw = ({"mode": "replay", "replay_start": date(2026, 9, 1), "end_date": date(2026, 9, 4)}
              if mode == "replay" else {})
        t = create_track(s, 1, name="附加数据", universe_name="cn_sse50_etf", rule_set_id=rs.id,
                         initial_cash=Decimal("1000000"), data_options=options, **kw)
        enroll(s, t, s.scalars(select(AgentRow).where(AgentRow.kind == "llm")).one().id)
        start_track(s, t, cst(2026, 9, 30, 16) if mode == "live" else cst(2026, 10, 2, 10))
        s.commit()
        return t.id


def _prompt(track_id: int) -> str:
    with get_sessionmaker()() as s:
        return s.scalars(select(DecisionRow.prompt_user).join(RoundRow, DecisionRow.round_id == RoundRow.id)
                         .where(RoundRow.track_id == track_id).order_by(DecisionRow.id.desc())).first()


def test_parsers_and_availability():
    assert cn_available_from(date(2026, 3, 31)) == date(2026, 4, 30)
    assert cn_available_from(date(2025, 12, 31)) == date(2026, 4, 30)
    assert hk_available_from(date(2026, 6, 30)) == date(2026, 8, 31)
    assert hk_available_from(date(2025, 12, 31)) == date(2026, 3, 31)
    df = pd.DataFrame([{"报告期": "2026-06-30", "营业总收入": "922.78亿", "营业总收入同比增长率": "1.30%",
                        "净利润": "4500万", "净利润同比增长率": False, "销售毛利率": "89.56%"}])
    [r] = parse_cn("600519.SH", df)
    assert r.data["revenue"] == 922.78 and r.data["net_profit"] == pytest.approx(0.45)
    assert r.data["net_profit_yoy"] is None and r.data["gross_margin"] == 89.56


async def test_live_round_includes_fundamentals_and_news_without_lookahead(fakes, monkeypatch):
    monkeypatch.setattr(cycle, "utcnow", lambda: datetime(2026, 9, 30, 8, 30, tzinfo=UTC))
    track_id = _track({"news": True, "fundamentals": True})
    await cycle.run_track_cycle(track_id, now=cst(2026, 9, 30, 16, 30))
    prompt = _prompt(track_id)
    fund = prompt.split("<fundamentals>")[1].split("</fundamentals>")[0]
    assert "600519.SH,贵州茅台,2026-06-30,922.8" in fund and "2026-09-30" not in fund   # 三季报 10/31 才可用
    news = prompt.split("<news>")[1].split("</news>")[0]
    assert "发布新品" in news and "未来的新闻" not in news
    assert "<b>" not in news and "＜b＞" in news                                       # 外部文本去掉尖括号
    fund_calls = fakes[0].calls
    await cycle.run_track_cycle(track_id, now=cst(2026, 9, 30, 18), force_round=True)
    assert fakes[0].calls == fund_calls                                                # 7 天内不重复请求


async def test_replay_has_no_news_and_failures_do_not_block(fakes, monkeypatch):
    track_id = _track({"news": True, "fundamentals": True}, mode="replay")
    await cycle.run_replay(track_id)
    prompt = _prompt(track_id)
    assert "<news>" not in prompt and "<fundamentals>" in prompt

    monkeypatch.setattr(deps, "news_provider", lambda: FakeNews(fail=True))
    monkeypatch.setattr(deps, "fundamentals_provider", lambda: (_ for _ in ()).throw(RuntimeError("坏了")))
    nmod.NewsService._fetched.clear()
    fmod.FundamentalsService._fetched.clear()
    monkeypatch.setattr(cycle, "utcnow", lambda: datetime(2026, 9, 30, 8, 30, tzinfo=UTC))
    live = _track({"news": True, "fundamentals": True})
    msg = await cycle.run_track_cycle(live, now=cst(2026, 9, 30, 16, 30))
    assert "决策完成" in msg and "<news>" not in _prompt(live)


def test_options_off_by_default(arena):
    with get_sessionmaker()() as s:
        rs = s.scalars(select(RuleSetRow)).first()
        t = create_track(s, 1, name="x", universe_name="cn_sse50_etf", rule_set_id=rs.id,
                         initial_cash=Decimal("1"), data_options={"news": 1, "evil": True})
        assert t.data_options == {"news": True} and t.decision_mode == "snapshot"
