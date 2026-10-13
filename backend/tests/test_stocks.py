"""股票详情：代码识别、腾讯实时行情解析、K 线复权与周 / 月线、搜索、只补到已收盘的交易日。"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.stocks import service as sv


def test_normalize_and_quote_codes():
    assert sv.normalize_symbol("600519.sh") == "600519.SH"
    assert sv.normalize_symbol("700.HK") == "00700.HK"
    assert sv.normalize_symbol("aapl.us") == "AAPL.US"
    assert sv.normalize_symbol("hsi.hk") == "HSI.HK"                # 指数
    with pytest.raises(sv.StockError):
        sv.normalize_symbol("600519")
    assert [sv.quote_code(x) for x in ("600519.SH", "000001.SH", "00700.HK", "HSI.HK", "DJI.US", "AAPL.US")] == [
        "sh600519", "sh000001", "hk00700", "hkHSI", "usDJI", "usAAPL"]


def test_parse_tencent_quote():
    fields = ["1", "贵州茅台", "600519", "1255.79", "1258.62", "1252.90", "25168"] + ["0"] * 23
    fields += ["20261008161457", "-2.83", "-0.22", "1258.00", "1242.00", "x"]
    hk = ["100", "腾讯控股", "00700", "411.4", "420.6", "422.0", "30202875"] + ["0"] * 23
    hk += ["2026/10/08 16:08:24", "-9.2", "-2.19", "424.6", "411.2", "x"]
    text = (f'v_sh600519="{"~".join(fields)}";\nv_hk00700="{"~".join(hk)}";\n'
            'v_usXXXX="1~停牌~XXXX~0~0";\n')
    out = sv.parse_qt(text, {"sh600519": "600519.SH", "hk00700": "00700.HK", "usXXXX": "XXXX.US"})
    assert set(out) == {"600519.SH", "00700.HK"}
    q = out["600519.SH"]
    assert q["name"] == "贵州茅台" and q["price"] == 1255.79 and q["high"] == 1258.0 and q["low"] == 1242.0
    assert q["change_pct"] == pytest.approx(1255.79 / 1258.62 - 1)
    assert out["00700.HK"]["time"] == "2026/10/08 16:08:24"


@pytest.fixture
def db(settings):
    from app.db.migrate import upgrade_to_head
    from app.db.session import get_engine, get_sessionmaker

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    upgrade_to_head(settings.resolved_database_url)
    get_engine()
    with get_sessionmaker()() as s:
        yield s


def _bars(s, symbol: str, closes: list[float], factors: list[float], start: date = date(2026, 9, 1)):
    from app.models import DailyBarRow, InstrumentRow

    s.add(InstrumentRow(symbol=symbol, market="CN", name="测试股份", type="stock", board="main", lot_size=100,
                        tick=Decimal("0.01")))
    d = start
    for c, f in zip(closes, factors, strict=True):
        while d.weekday() >= 5:
            d += timedelta(days=1)
        s.add(DailyBarRow(symbol=symbol, date=d, open=Decimal(str(c)), high=Decimal(str(c + 1)),
                          low=Decimal(str(c - 1)), close=Decimal(str(c)), volume=100, amount=Decimal("1000"),
                          prev_close=None, adj_factor=Decimal(str(f))))
        d += timedelta(days=1)
    s.commit()


def test_bars_adjust_and_resample(db):
    # 第 3 天除权（复权因子 1 → 2，价格减半）
    _bars(db, "600000.SH", [10, 11, 6, 6.5, 7, 7.5, 8], [1, 1, 2, 2, 2, 2, 2])
    raw = sv.bars(db, "600000.SH", adjust="none")
    assert [b[2] for b in raw][:3] == [10, 11, 6]
    qfq = sv.bars(db, "600000.SH", adjust="qfq")
    assert [b[2] for b in qfq][:3] == [5, 5.5, 6] and qfq[-1][2] == 8      # 以最新价为准
    hfq = sv.bars(db, "600000.SH", adjust="hfq")
    assert hfq[-1][2] == 16
    week = sv.bars(db, "600000.SH", freq="w", adjust="none")
    # 2026-09-01 是周二：第一周 4 个交易日、第二周 3 个
    assert len(week) == 2 and week[0][0] == "2026-09-04" and week[0][1] == 10 and week[0][2] == 6.5
    assert week[0][3] == 5 and week[0][4] == 12 and week[0][5] == 400       # 低 / 高 / 成交量合计
    assert sv.bars(db, "NOPE.SH") == []


def test_search_ranks_exact_code_first(db, monkeypatch):
    from app.arena import deps

    _bars(db, "600000.SH", [10], [1])

    class FakeResolver:
        def stock_names(self, today):
            return {"600000": "浦发银行", "600001": "测试一号", "000001": "平安银行"}

    monkeypatch.setattr(deps, "universe_resolver", lambda: FakeResolver())
    monkeypatch.setattr(sv, "smartbox", lambda q: [])
    r = sv.search(db, "600000")
    assert r[0]["symbol"] == "600000.SH"
    assert [x["symbol"] for x in sv.search(db, "平安")] == ["000001.SZ"]
    names = [x["name"] for x in sv.search(db, "上证")]
    assert "上证指数" in names and "上证50" in names
    assert sv.search(db, "00700.HK")[0]["symbol"] == "00700.HK"          # 完整港股代码可以直接打开
    assert sv.search(db, "  ") == []


def test_settled_until_waits_for_data_ready(db, monkeypatch):
    from app.market.calendar import TradingCalendar
    from app.market.service import MarketDataService

    days = [date(2026, 10, 8), date(2026, 10, 9), date(2026, 10, 12)]
    monkeypatch.setattr(MarketDataService, "calendar", lambda self, today=None: TradingCalendar("CN", days))
    cst = timezone(timedelta(hours=8))
    # 收盘数据 15:40 才稳定：之前只算到上一个交易日，之后算当天
    assert sv.settled_until(db, "CN", datetime(2026, 10, 9, 14, 0, tzinfo=cst)) == date(2026, 10, 8)
    assert sv.settled_until(db, "CN", datetime(2026, 10, 9, 16, 0, tzinfo=cst)) == date(2026, 10, 9)
    assert sv.settled_until(db, "CN", datetime(2026, 10, 11, 10, 0, tzinfo=cst)) == date(2026, 10, 9)   # 周末


def test_parse_smartbox_filters_types():
    t = ('v_hint="sh~000847~\u817e\u8baf\u6d4e\u5b89~txja~ZS^hk~00700~\u817e\u8baf\u63a7\u80a1~txkg~GP'
         '^us~tcehy.ps~x~y~GP^us~nvda.oq~\u82f1\u4f1f\u8fbe~ywd~GP^hk~13005~w~w~QZ'
         '^sh~513180~\u6052\u751fETF~h~QDII-ETF^sh~000001~\u4e0a\u8bc1~szzs~ZS"')
    got = [(x["symbol"], x["name"], x["type"]) for x in sv.parse_smartbox(t)]
    # 不支持的指数、场外美股（.ps）、权证（QZ）不要
    assert got == [("00700.HK", "腾讯控股", "stock"), ("NVDA.US", "英伟达", "stock"),
                   ("513180.SH", "恒生ETF", "etf"), ("000001.SH", "上证", "index")]
    assert sv.parse_smartbox('v_hint="N"') == []


def test_search_falls_back_to_smartbox(db, monkeypatch):
    from app.arena import deps

    class FakeResolver:
        def stock_names(self, today):
            return {}

    monkeypatch.setattr(deps, "universe_resolver", lambda: FakeResolver())
    monkeypatch.setattr(sv, "smartbox", lambda q: [{"symbol": "00700.HK", "name": "腾讯控股", "market": "HK",
                                                    "type": "stock"}])
    assert [x["symbol"] for x in sv.search(db, "腾讯")] == ["00700.HK"]
