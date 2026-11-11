"""同花顺金融数据 API（模拟 HTTP 响应）：股票日线与复权、分红送转、交易日历、只用于股票、备用链、交叉核对。"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import httpx
import pytest

from app.market.providers import fuyao_cn
from app.market.providers.fuyao_cn import FuyaoCNProvider, FuyaoError, _ms
from app.market.types import Instrument

STOCK = Instrument("600036.SH", "CN", "招商银行", "stock", "main")
ETF = Instrument("510300.SH", "CN", "沪深300ETF", "etf", "fund")
KEY = "fuyao-secret-key"


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    monkeypatch.setattr(fuyao_cn, "_limiter", None)


def _provider(handler, seen: list):
    def wrap(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json=handler(req.url.path, dict(req.url.params)))
    return FuyaoCNProvider(KEY, httpx.MockTransport(wrap))


def _ok(items: list) -> dict:
    return {"code": 0, "message": "success", "data": {"item": items}}


def _bar(d: date, close: float, vol: float = 1000) -> dict:
    return {"date_ms": _ms(d), "open_price": close, "high_price": close, "low_price": close, "close_price": close,
            "volume": vol, "turnover": close * vol}


def test_stock_bars_with_dividend_and_bonus():
    def handler(path, params):
        if path.endswith("/prices/historical"):
            assert params["adjust"] == "none" and params["thscode"] == "600036.SH"
            return _ok([_bar(date(2026, 7, 8), 37.94), _bar(date(2026, 7, 9), 37.55),
                        _bar(date(2026, 7, 10), 33.25, 2000)])
        return _ok([{"ticker": "600036", "ex_date_ms": _ms(date(2026, 7, 10)), "dividend_per_share": 1.003,
                     "per_share_bonus": 0.1}])

    seen: list = []
    bars = _provider(handler, seen).daily_bars(STOCK, date(2026, 7, 9), date(2026, 7, 10))
    assert all(r.headers["X-api-key"] == KEY for r in seen)
    assert [b.date for b in bars] == [date(2026, 7, 9), date(2026, 7, 10)]            # 多取的前几天只用来算前收
    assert bars[0].prev_close == Decimal("37.94") and bars[1].volume == 2000
    ref = (Decimal("37.55") - Decimal("1.003")) / Decimal("1.1")                     # 除权参考价
    assert bars[1].prev_close == ref.quantize(Decimal("0.01"))
    assert abs(float(bars[1].adj_factor / bars[0].adj_factor) - float(Decimal("37.55") / ref)) < 1e-9


def test_only_stocks_and_errors_hide_key():
    def handler(path, params):
        if "adjustment-factors" in path:
            return {"code": 3002, "message": "No adjustment events", "data": None}
        return {"code": 1002, "message": "Unknown thscode: 601059.SH", "data": None}

    p = _provider(handler, [])
    with pytest.raises(FuyaoError):
        p.daily_bars(ETF, date(2026, 7, 1), date(2026, 7, 10))                       # ETF 交给其他数据源
    assert p.corporate_actions(STOCK) == []                                          # 没有除权事件
    assert p.corporate_actions(Instrument("000300.SH", "CN", "沪深300", "index", "index")) == []
    with pytest.raises(FuyaoError) as e:
        p.daily_bars(Instrument("601059.SH", "CN", "信达证券", "stock", "main"), date(2026, 7, 1), date(2026, 7, 10))
    assert "Unknown thscode" in str(e.value) and KEY not in str(e.value)


def test_trading_days_chunked_and_need_future():
    future = date.today() + timedelta(days=30)

    def handler(path, params):
        a, b = params["start_date"], params["end_date"]
        assert int(b[:4]) - int(a[:4]) <= 9                                          # 单次不超过 10 年
        days = [date(2016, 7, 1), future] if a <= future.strftime("%Y%m%d") <= b else [date(2006, 1, 4)]
        return _ok([{"date": d.strftime("%Y%m%d"), "date_ms": _ms(d)} for d in days if a <= d.strftime("%Y%m%d") <= b])

    seen: list = []
    days = _provider(handler, seen).trading_days()
    assert date(2006, 1, 4) in days and future in days and len(seen) >= 2

    with pytest.raises(FuyaoError):                                                 # 不含未来日期：不覆盖缓存的日历
        _provider(lambda path, params: _ok([{"date": "20160701"}]), []).trading_days()


def test_chain_reads_key_file(tmp_path, monkeypatch):
    from app.arena import deps
    from app.core.config import get_settings
    from app.market.providers.akshare_cn import AkshareCNProvider
    from app.market.providers.fallback import FallbackProvider

    key_file = tmp_path / "fuyao.key"

    def build(**env):
        for k in ("SA_FUYAO_KEY_FILE", "SA_TUSHARE_TOKEN", "SA_CN_DATA_SOURCE"):
            monkeypatch.delenv(k, raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        get_settings.cache_clear()
        deps.cn_provider.cache_clear()
        return deps.cn_provider()

    try:
        key_file.write_text("", encoding="utf-8")
        assert isinstance(build(SA_FUYAO_KEY_FILE=str(key_file)), AkshareCNProvider)   # 空文件：不启用
        assert deps.fuyao_provider() is None
        key_file.write_text(KEY + "\n", encoding="utf-8")
        p = build(SA_FUYAO_KEY_FILE=str(key_file))
        assert isinstance(p, FallbackProvider) and isinstance(p.backup, FuyaoCNProvider)
        assert isinstance(p.primary, AkshareCNProvider) and deps.fuyao_provider() is not None
        p = build(SA_FUYAO_KEY_FILE=str(key_file), SA_TUSHARE_TOKEN="t")
        # 新浪 → Tushare → 同花顺
        assert isinstance(p.backup, FuyaoCNProvider) and isinstance(p.primary, FallbackProvider)
        assert isinstance(build(SA_FUYAO_KEY_FILE=str(key_file), SA_CN_DATA_SOURCE="akshare"), AkshareCNProvider)
    finally:
        deps.cn_provider.cache_clear()


def test_cross_check_reports_mismatch(session):  # noqa: F811
    from app.market.service import MarketDataService
    from app.quant import quality
    from tests.market.test_service import INST, FakeProvider

    svc = MarketDataService(session, FakeProvider())
    svc.upsert_instruments([INST])
    svc.update_bars([INST], until=date(2026, 7, 10), history_start=date(2026, 7, 8))   # 收盘 37.94 / 37.55 / 36.55

    class Other:
        def daily_bars(self, inst, start, end):
            from tests.market.test_service import _bar as bar

            return [bar(date(2026, 7, 8), "37.94"), bar(date(2026, 7, 9), "37.80"), bar(date(2026, 7, 10), "36.55")]

    out = quality.cross_check(session, {INST.symbol: "招商银行"}, provider=Other(), today=date(2026, 7, 10))
    assert out["symbols"] == 1 and out["compared"] == 3 and out["total"] == 1
    it = out["issues"][0]
    assert it["date"] == "2026-07-09" and it["kind"] == "cross" and "37.55" in it["detail"] and "37.8" in it["detail"]

    class Down:
        def daily_bars(self, inst, start, end):
            raise RuntimeError("连不上")

    out = quality.cross_check(session, {INST.symbol: "招商银行"}, provider=Down(), today=date(2026, 7, 10))
    assert out["total"] == 0 and "连不上" in out["failed"][0]
    assert quality.cross_check(session, {INST.symbol: "招商银行"}, provider=None) is None   # 没有配置 Key


from tests.market.test_service import session  # noqa: E402, F401
