from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.db.migrate import upgrade_to_head
from app.db.session import build_engine
from app.market.service import MarketDataService
from app.market.types import CorporateAction, DailyBar, Instrument
from app.market.universe import UniverseResolver, load_universe_specs
from app.models.market import DataSourceStatusRow

INST = Instrument("600036.SH", "CN", "招商银行", "stock", "main", price_limit_pct=Decimal("0.10"))


def _bar(d: date, close: str, factor: str = "1") -> DailyBar:
    c = Decimal(close)
    return DailyBar(INST.symbol, d, c, c, c, c, 100, c * 100, c, Decimal(factor))


class FakeProvider:
    market = "CN"

    def __init__(self):
        self.calls: list[tuple[date, date]] = []
        self.bars = [
            _bar(date(2026, 7, 8), "37.94", "6.5"),
            _bar(date(2026, 7, 9), "37.55", "6.5"),
            _bar(date(2026, 7, 10), "36.55", "6.67"),   # 除权后因子上升
        ]
        self.fail = False

    def trading_days(self):
        if self.fail:
            raise RuntimeError("network down")
        start = date(2026, 1, 1)
        return [start + timedelta(days=i) for i in range(400) if (start + timedelta(days=i)).weekday() < 5]

    def daily_bars(self, inst, start, end):
        if self.fail:
            raise RuntimeError("network down")
        self.calls.append((start, end))
        return [b for b in self.bars if start <= b.date <= end]

    def corporate_actions(self, inst):
        return [CorporateAction(inst.symbol, date(2026, 7, 10), Decimal("1.003"), Decimal("0"), "fake")]


@pytest.fixture
def session(tmp_path):
    url = f"sqlite:///{(tmp_path / 't.db').as_posix()}"
    upgrade_to_head(url)
    engine = build_engine(url)
    with Session(engine) as s:
        yield s
    engine.dispose()


def test_update_bars_incremental(session):
    provider = FakeProvider()
    svc = MarketDataService(session, provider)
    svc.upsert_instruments([INST])
    assert svc.update_bars([INST], until=date(2026, 7, 9), history_start=date(2026, 7, 1)) == {INST.symbol: "ok"}
    assert svc.update_bars([INST], until=date(2026, 7, 10), history_start=date(2026, 7, 1)) == {INST.symbol: "ok"}
    # 第二次只请求缺失的日期
    assert provider.calls == [(date(2026, 7, 1), date(2026, 7, 9)), (date(2026, 7, 10), date(2026, 7, 10))]
    assert svc.update_bars([INST], until=date(2026, 7, 10), history_start=date(2026, 7, 1)) == {
        INST.symbol: "up-to-date"}
    bars = svc.get_bars(INST.symbol, date(2026, 7, 1), date(2026, 7, 31))
    assert [b.close for b in bars] == [Decimal("37.94"), Decimal("37.55"), Decimal("36.55")]
    assert svc.get_instrument(INST.symbol).price_limit_pct == Decimal("0.10")


def test_history_frame_forward_adjusted(session):
    svc = MarketDataService(session, FakeProvider())
    svc.update_bars([INST], until=date(2026, 7, 10), history_start=date(2026, 7, 1))
    df = svc.history_frame(INST.symbol, end=date(2026, 7, 10), lookback=10)
    # 前复权以最后一天为基准：最后一天价格不变，之前的价格按因子比例下调
    assert df["close"].iloc[-1] == pytest.approx(36.55)
    assert df["close"].iloc[0] == pytest.approx(37.94 * 6.5 / 6.67)


def test_update_failure_recorded_in_status(session):
    provider = FakeProvider()
    provider.fail = True
    svc = MarketDataService(session, provider)
    result = svc.update_bars([INST], until=date(2026, 7, 10), history_start=date(2026, 7, 1))
    assert result[INST.symbol].startswith("error")
    status = session.get(DataSourceStatusRow, "CN.daily_bars")
    assert status.consecutive_failures == 1
    assert "network down" in status.last_error


def test_status_counts_failures_and_resets(session):
    svc = MarketDataService(session, FakeProvider())

    def boom():
        raise RuntimeError("timeout")

    for _ in range(2):
        with pytest.raises(RuntimeError):
            svc._track("CN.x", boom)
    assert session.get(DataSourceStatusRow, "CN.x").consecutive_failures == 2
    assert svc._track("CN.x", lambda: 42) == 42
    status = session.get(DataSourceStatusRow, "CN.x")
    assert status.consecutive_failures == 0 and status.last_success_at is not None
    assert status.last_error == "timeout"  # 保留最近一次错误信息供系统页查看


def test_status_concurrent_insert_does_not_fail(session):
    """另一个赛道在本次网络请求期间先写入了同名状态记录（并发拉数据），本次写入不能因唯一约束失败。"""
    svc = MarketDataService(session, FakeProvider())

    def other_track_writes_first():
        with Session(session.get_bind()) as other:
            other.add(DataSourceStatusRow(name="CN.daily_bars", consecutive_failures=3))
            other.commit()
        return "ok"

    assert svc._track("CN.daily_bars", other_track_writes_first) == "ok"
    session.commit()
    assert session.get(DataSourceStatusRow, "CN.daily_bars").consecutive_failures == 0


def test_calendar_cached_and_fallback(session):
    provider = FakeProvider()
    svc = MarketDataService(session, provider)
    cal = svc.calendar(today=date(2026, 3, 1))
    assert cal.is_trading_day(date(2026, 3, 2))
    # 数据源故障时继续使用缓存
    provider.fail = True
    cal2 = svc.calendar(today=date(2027, 1, 20))
    assert cal2.last == cal.last


def test_corporate_actions(session):
    svc = MarketDataService(session, FakeProvider())
    svc.update_corporate_actions([INST])
    svc.update_corporate_actions([INST])  # 重复更新不产生重复记录
    actions = svc.actions_on([INST.symbol], date(2026, 7, 10))
    assert len(actions) == 1 and actions[0].cash_per_share == Decimal("1.003")


class FakeUniverseSource:
    def stock_names(self):
        return {"600519": "贵州茅台", "688981": "中芯国际", "600000": "ST浦发"}

    def index_constituents(self, index_code):
        return [("600519", "贵州茅台"), ("688981", "中芯国际"), ("600000", "浦发银行")]


def test_universe_resolve_default_pool(settings, tmp_path):
    specs = load_universe_specs(settings.config_dir)
    resolver = UniverseResolver(specs, FakeUniverseSource(), tmp_path / "cache")
    insts = resolver.resolve("cn_sse50_etf", today=date(2026, 10, 1))
    by_symbol = {i.symbol: i for i in insts}
    assert len(insts) == 3 + 10
    assert by_symbol["688981.SH"].board == "star"
    assert by_symbol["688981.SH"].price_limit_pct == Decimal("0.20")
    assert by_symbol["600000.SH"].is_st and by_symbol["600000.SH"].price_limit_pct == Decimal("0.05")
    assert by_symbol["513100.SH"].t0 is True
    assert by_symbol["510300.SH"].t0 is False
    assert by_symbol["510300.SH"].tick == Decimal("0.001")
    assert by_symbol["159915.SZ"].price_limit_pct == Decimal("0.2")
