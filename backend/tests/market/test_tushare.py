"""Tushare 数据源（模拟 HTTP 响应）：日线单位换算与复权因子推算、分红、接口错误、备用数据源切换、配置选择。"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import httpx
import pytest

from app.market.providers import tushare_cn
from app.market.providers.fallback import FallbackProvider
from app.market.providers.tushare_cn import TushareCNProvider, TushareError
from app.market.types import Instrument

STOCK = Instrument("600036.SH", "CN", "招商银行", "stock", "main", price_limit_pct=Decimal("0.10"))
ETF = Instrument("510300.SH", "CN", "沪深300ETF", "etf", "fund")
TOKEN = "t0ken-secret-123"


def _transport(replies: dict, seen: list):
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        seen.append(body)
        reply = replies[body["api_name"]]
        return httpx.Response(200, json=reply)
    return httpx.MockTransport(handler)


def _data(fields: list[str], items: list[list]) -> dict:
    return {"code": 0, "msg": "", "data": {"fields": fields, "items": items}}


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    monkeypatch.setattr(tushare_cn, "_limiter", None)


def test_daily_bars_units_and_factor_from_pre_close():
    seen: list = []
    fields = ["trade_date", "open", "high", "low", "close", "pre_close", "vol", "amount"]
    items = [["20260710", 36.0, 36.8, 35.9, 36.55, 36.55, 2000.0, 7300.0],      # 除息日：昨收 36.55 < 前一天收盘 37.55
             ["20260709", 37.9, 38.0, 37.4, 37.55, 37.94, 1500.5, 5632.1],
             ["20260708", 37.0, 38.1, 36.9, 37.94, 37.10, 1000.0, 3794.0]]
    p = TushareCNProvider(TOKEN, _transport({"daily": _data(fields, items)}, seen))
    bars = p.daily_bars(STOCK, date(2026, 7, 8), date(2026, 7, 10))
    assert seen[0]["api_name"] == "daily" and seen[0]["params"] == {
        "ts_code": "600036.SH", "start_date": "20260708", "end_date": "20260710"}
    assert [b.date for b in bars] == [date(2026, 7, 8), date(2026, 7, 9), date(2026, 7, 10)]
    assert bars[1].volume == 150050 and bars[1].amount == Decimal("5632100")       # 手 → 股，千元 → 元
    assert bars[0].adj_factor == bars[1].adj_factor == 1
    assert bars[2].adj_factor == (Decimal("37.55") / Decimal("36.55")).quantize(Decimal("0.0000000001"))
    assert bars[2].prev_close == Decimal("36.55")


def test_etf_and_index_use_their_own_interfaces():
    seen: list = []
    reply = _data(["trade_date", "open", "high", "low", "close", "pre_close", "vol", "amount"],
                  [["20260710", 4.0, 4.1, 3.9, 4.05, 4.0, 10.0, 4.0]])
    p = TushareCNProvider(TOKEN, _transport({"fund_daily": reply, "index_daily": reply}, seen))
    p.daily_bars(ETF, date(2026, 7, 10), date(2026, 7, 10))
    p.daily_bars(Instrument("000300.SH", "CN", "沪深300", "index", "index"), date(2026, 7, 10), date(2026, 7, 10))
    assert [s["api_name"] for s in seen] == ["fund_daily", "index_daily"]


def test_dividends_trading_days_and_errors():
    seen: list = []
    replies = {
        "dividend": _data(["ex_date", "div_proc", "stk_div", "cash_div_tax"],
                          [["20260710", "实施", 0.0, 1.003], [None, "预案", 0.0, 1.1], ["20250711", "实施", 0.3, 0.9]]),
        "trade_cal": _data(["cal_date", "is_open"], [["20260710", "1"], ["20260711", "0"], ["20260709", 1]]),
        "fund_daily": {"code": 40203, "msg": "抱歉，您没有接口访问权限", "data": None},
    }
    p = TushareCNProvider(TOKEN, _transport(replies, seen))
    acts = p.corporate_actions(STOCK)
    assert [(a.ex_date, a.cash_per_share, a.shares_per_share) for a in acts] == [
        (date(2025, 7, 11), Decimal("0.9"), Decimal("0.3")), (date(2026, 7, 10), Decimal("1.003"), Decimal("0"))]
    assert p.trading_days() == [date(2026, 7, 9), date(2026, 7, 10)]
    with pytest.raises(TushareError) as e:
        p.daily_bars(ETF, date(2026, 7, 10), date(2026, 7, 10))
    assert "没有接口访问权限" in str(e.value) and TOKEN not in str(e.value)       # 错误信息不含 token
    with pytest.raises(TushareError):
        p.corporate_actions(ETF)                                            # 不支持基金分红：交给备用数据源


class _Src:
    market = "CN"

    def __init__(self, name, fail=False):
        self.name, self.fail, self.calls = name, fail, 0

    def daily_bars(self, *a):
        self.calls += 1
        if self.fail:
            raise RuntimeError(f"{self.name} 坏了")
        return self.name


def test_fallback_provider():
    main, backup = _Src("新浪"), _Src("Tushare")
    fp = FallbackProvider(main, backup)
    assert fp.market == "CN" and fp.daily_bars() == "新浪" and backup.calls == 0
    main.fail = True
    assert fp.daily_bars() == "Tushare"
    backup.fail = True
    with pytest.raises(RuntimeError) as e:
        fp.daily_bars()
    assert "新浪 坏了" in str(e.value) and "Tushare 坏了" in str(e.value)
    main.industry_history = lambda code: ["行业"]                            # 只有一方有的方法直接用那一方
    assert fp.industry_history("600036") == ["行业"] and hasattr(fp, "industry_history")
    assert not hasattr(fp, "index_weights")


def test_cn_provider_follows_settings(monkeypatch):
    from app.arena import deps
    from app.core.config import get_settings
    from app.market.providers.akshare_cn import AkshareCNProvider

    def build(source, token=None):
        monkeypatch.setenv("SA_CN_DATA_SOURCE", source)
        if token:
            monkeypatch.setenv("SA_TUSHARE_TOKEN", token)
        else:
            monkeypatch.delenv("SA_TUSHARE_TOKEN", raising=False)
        get_settings.cache_clear()
        deps.cn_provider.cache_clear()
        return deps.cn_provider()

    try:
        assert isinstance(build("auto"), AkshareCNProvider)                 # 没有 token：只用新浪 / 腾讯
        p = build("auto", TOKEN)
        assert isinstance(p.primary, AkshareCNProvider) and isinstance(p.backup, TushareCNProvider)
        p = build("tushare", TOKEN)
        assert isinstance(p.primary, TushareCNProvider)
        assert isinstance(build("akshare", TOKEN), AkshareCNProvider)
    finally:
        deps.cn_provider.cache_clear()
