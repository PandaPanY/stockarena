"""首页：明星股设置、涨跌家数解析、组合收益与盘中估算。"""

from __future__ import annotations

import sys
import types
from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from app.stocks import home


@pytest.fixture
def db(settings):
    from app.db.migrate import upgrade_to_head
    from app.db.session import get_engine, get_sessionmaker

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    upgrade_to_head(settings.resolved_database_url)
    get_engine()
    home._cache.clear()
    with get_sessionmaker()() as s:
        yield s


def test_stars_default_and_set(db):
    assert home.stars(db) == home.DEFAULT_STARS
    assert home.set_stars(db, ["600519.sh", "700.HK", "aapl.us", "600519.SH"]) == ["600519.SH", "00700.HK", "AAPL.US"]
    db.commit()
    assert home.stars(db) == ["600519.SH", "00700.HK", "AAPL.US"]
    with pytest.raises(home.sv.StockError):
        home.set_stars(db, ["600519"])


def test_breadth_parsing(monkeypatch):
    df = pd.DataFrame({"item": ["上涨", "真实涨停", "下跌", "真实跌停", "平盘", "停牌", "活跃度", "统计日期"],
                       "value": [1634.0, 39.0, 3466.0, 13.0, 109.0, 15.0, "31.28%", "2026-10-08 15:00:00"]})
    monkeypatch.setitem(sys.modules, "akshare", types.SimpleNamespace(stock_market_activity_legu=lambda: df))
    home._cache.clear()
    b = home.breadth()
    assert b["up"] == 1634 and b["down"] == 3466 and b["limit_up"] == 39 and b["limit_down"] == 13
    assert b["activity"] == "31.28%"


def test_quote_after():
    d = date(2026, 10, 8)
    assert home._quote_after("20261009093500", d)
    assert not home._quote_after("20261008161457", d)
    assert home._quote_after("2026/10/09 10:00:00", d)
    assert not home._quote_after("2026-10-07 16:55:25", d)
    assert not home._quote_after(None, d)


def test_account_summary_with_intraday_estimate(db, monkeypatch):
    from app.models import AccountRow, AgentRow, NavSnapshotRow, PositionRow, RuleSetRow, TrackRow

    rs = RuleSetRow(name="r", title="r", market="CN", params={})
    db.add(rs)
    db.flush()
    track = TrackRow(name="t", market="CN", currency="CNY", universe_name="u", benchmark_symbol="000300.SH",
                     rule_set_id=rs.id, initial_cash=Decimal("1000000"), status="running")
    agent = AgentRow(name="规则选手", kind="quant")
    db.add_all([track, agent])
    db.flush()
    acc = AccountRow(track_id=track.id, agent_id=agent.id, cash=Decimal(0))
    db.add(acc)
    db.flush()
    for d, nav, bench in [(date(2026, 10, 6), 1_000_000, 4000), (date(2026, 10, 7), 1_010_000, 4040)]:
        db.add(NavSnapshotRow(account_id=acc.id, date=d, nav=Decimal(nav), cash=Decimal(0), market_value=Decimal(nav),
                              benchmark_close=Decimal(bench)))
    db.add(PositionRow(account_id=acc.id, symbol="600519.SH", qty=1000, avg_cost=Decimal("1000")))
    db.commit()
    monkeypatch.setattr(home.sv, "quotes", lambda syms: {
        "600519.SH": {"price": 1010.1, "prev_close": 1000.0, "change_pct": 0.0101, "time": "20261008100000"},
        "000300.SH": {"price": 4060.0, "prev_close": 4040.0, "change_pct": 0.00495, "time": "20261008100000"}})
    a = home._account_summary(db, acc.id, "纯规则")
    assert a["day"] == pytest.approx(0.01) and a["bench_day"] == pytest.approx(0.01)
    assert a["total"] == pytest.approx(0.01) and a["since"] == "2026-10-06"
    assert a["intraday"]["est"] == pytest.approx(1000 * 10.1 / 1_010_000)
    assert a["intraday"]["bench"] == pytest.approx(0.00495)
