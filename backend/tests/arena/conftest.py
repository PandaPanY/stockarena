"""竞技模块测试的共享夹具：伪造行情源与伪造 AI，不访问网络。"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.arena import cycle, deps, strategy
from app.arena.seed import seed_defaults
from app.db.migrate import upgrade_to_head
from app.db.session import get_sessionmaker
from app.llm import ChatResponse
from app.market.types import CorporateAction, DailyBar
from app.models import AgentRow, LLMProviderRow

HOLIDAYS = {date(2026, 10, d) for d in range(1, 8)}
STOCKS = [("600519", "贵州茅台"), ("601318", "中国平安"), ("688981", "中芯国际")]


def cst(y, m, d, hh, mm=0) -> datetime:
    """北京时间 → UTC。"""
    return datetime(y, m, d, hh, mm, tzinfo=UTC) - timedelta(hours=8)


class FakeProvider:
    market = "CN"

    def __init__(self):
        self.dividends: list[CorporateAction] = []

    def trading_days(self):
        start = date(2026, 1, 1)
        days = (start + timedelta(days=i) for i in range(365))
        return [d for d in days if d.weekday() < 5 and d not in HOLIDAYS]

    def daily_bars(self, inst, start, end):
        bars, prev = [], None
        base = Decimal("100") if inst.type == "stock" else Decimal("4")
        for i, d in enumerate(self.trading_days()):
            close = base + Decimal(i % 7) * (inst.tick * 10)
            if start <= d <= end:
                bars.append(DailyBar(inst.symbol, d, close, close * Decimal("1.01"), close * Decimal("0.99"), close,
                                     50_000_000, close * 50_000_000, prev))
            prev = close
        return bars

    def corporate_actions(self, inst):
        return [a for a in self.dividends if a.symbol == inst.symbol]

    def stock_names(self):
        return dict(STOCKS)

    def index_constituents(self, index_code):
        return STOCKS


class FakeLLM:
    def __init__(self, content: str):
        self.content = content
        self.calls = 0
        self.reflections: list = []         # 收到的复盘请求
        self.reflection_content: str | None = None   # 为空时每次返回不同的新策略

    async def chat(self, req):
        if "<performance>" in req.messages[0]["content"]:
            self.reflections.append(req)
            content = self.reflection_content or json.dumps(
                {"review": f"第 {len(self.reflections)} 次复盘：仓位过于集中", "changes": "降低单只仓位",
                 "strategy": f"策略第 {len(self.reflections)} 版：单只不超过 15%，跌破 MA20 减仓"}, ensure_ascii=False)
            return ChatResponse(content=content, tokens_in=3000, tokens_out=500, model="fake")
        self.calls += 1
        assert "<market_data>" in req.messages[0]["content"]
        return ChatResponse(content=self.content, reasoning="思考过程", tokens_in=1000, tokens_out=200, model="fake")


@pytest.fixture
def arena(settings, monkeypatch):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    upgrade_to_head(settings.resolved_database_url)
    provider = FakeProvider()
    monkeypatch.setattr(deps, "cn_provider", lambda: provider)
    deps.secret_box.cache_clear()
    cycle._actions_refreshed.clear()
    llm = FakeLLM(json.dumps({"analysis": "看好茅台与沪深300",
                              "targets": [{"symbol": "600519", "weight": 0.2, "reason": "龙头"},
                                          {"symbol": "510300.SH", "weight": 0.3}],
                              "cash_weight": 0.5}, ensure_ascii=False))
    monkeypatch.setattr(cycle, "create_client", lambda cfg: llm)
    monkeypatch.setattr(strategy, "create_client", lambda cfg: llm)
    with get_sessionmaker()() as s:
        seed_defaults(s, settings.config_dir)
        prov = LLMProviderRow(name="本地", protocol="openai_compatible", base_url="http://127.0.0.1:1/v1",
                              default_model="fake", price_in_per_m=Decimal("2"), price_out_per_m=Decimal("8"))
        s.add(prov)
        s.flush()
        s.add(AgentRow(name="测试AI", kind="llm", provider_id=prov.id, params={"temperature": 0.2}, memory_rounds=3))
        s.commit()
    return provider, llm
