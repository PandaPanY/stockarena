"""港股、美股数据源：日线与复权信息来自新浪（AKShare 封装，直连稳定），港股每手股数来自港交所证券名单。

实测（2026-10）：Yahoo Finance（yfinance）经本机代理约一半请求被断开，不作为数据源。
所有方法都是同步阻塞调用。
"""

from __future__ import annotations

import io
import logging
from datetime import date
from decimal import Decimal

import pandas as pd
import requests

from app.market.providers.adjust import actions_from_factor_table, build_bars
from app.market.retry import RateLimiter, call_with_retry
from app.market.types import CorporateAction, DailyBar, Instrument
from app.trading.market_rules import hk_tick

logger = logging.getLogger(__name__)

_sina = RateLimiter(0.3)
_hkex = RateLimiter(1.0)

HKEX_SECURITIES_URL = "https://www.hkex.com.hk/eng/services/trading/securities/securitieslists/ListOfSecurities.xlsx"
CALENDAR_START = "2015-01-01"


def _ak():
    import akshare

    return akshare


def _calendar_days(code: str) -> list[date]:
    import exchange_calendars as xc

    cal = xc.get_calendar(code)
    return [d.date() for d in cal.sessions_in_range(CALENDAR_START, cal.last_session)]


def _actions_or_empty(provider, inst: Instrument) -> list[CorporateAction]:
    """新浪没有部分标的（如港股 ETF）的复权表：取不到时按无除权事件处理，不影响日线。"""
    try:
        return provider.corporate_actions(inst)
    except Exception as e:  # 数据源异常类型繁杂
        logger.warning("%s 没有复权数据，按无除权事件处理：%s", inst.symbol, e)
        return []


class SinaHKProvider:
    market = "HK"

    def trading_days(self) -> list[date]:
        return _calendar_days("XHKG")

    def daily_bars(self, inst: Instrument, start: date, end: date) -> list[DailyBar]:
        if inst.type == "index":
            raw = call_with_retry(lambda: _ak().stock_hk_index_daily_sina(symbol=inst.code),
                                  name=f"sina.hk_index.{inst.symbol}", limiter=_sina)
            return build_bars(inst, raw, [], start, end, tick_for=lambda _p: Decimal("0.01"), places=Decimal("0.01"))
        raw = call_with_retry(lambda: _ak().stock_hk_daily(symbol=inst.code, adjust=""),
                              name=f"sina.hk_daily.{inst.symbol}", limiter=_sina)
        return build_bars(inst, raw, _actions_or_empty(self, inst), start, end, tick_for=hk_tick,
                          places=Decimal("0.001"))

    def corporate_actions(self, inst: Instrument) -> list[CorporateAction]:
        if inst.type == "index":
            return []
        table = call_with_retry(lambda: _ak().stock_hk_daily(symbol=inst.code, adjust="hfq-factor"),
                                name=f"sina.hk_factor.{inst.symbol}", limiter=_sina, attempts=2)
        return actions_from_factor_table(inst.symbol, table, "hfq_factor", "cash", "sina.hk_factor")

    def securities(self) -> dict[str, dict]:
        """港交所证券名单：代码 → {lot, stamp_duty, name_en, category}。约 1.3MB，经系统代理访问。"""
        def fetch() -> dict[str, dict]:
            resp = requests.get(HKEX_SECURITIES_URL, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            df = pd.read_excel(io.BytesIO(resp.content), header=2, dtype=str)
            out = {}
            for r in df.itertuples(index=False):
                code, lot = str(r[0]).strip(), str(r[4]).replace(",", "").strip()
                if code.isdigit() and lot.isdigit():
                    out[code.zfill(5)] = {"lot": int(lot), "name_en": str(r[1]).strip(), "category": str(r[2]).strip(),
                                          "stamp_duty": str(r[7]).strip().upper() == "Y"}
            return out

        return call_with_retry(fetch, name="hkex.securities", limiter=_hkex)


class SinaUSProvider:
    market = "US"

    def trading_days(self) -> list[date]:
        return _calendar_days("XNYS")

    def daily_bars(self, inst: Instrument, start: date, end: date) -> list[DailyBar]:
        if inst.type == "index":
            raw = call_with_retry(lambda: _ak().index_us_stock_sina(symbol=f".{inst.code}"),
                                  name=f"sina.us_index.{inst.symbol}", limiter=_sina)
            return build_bars(inst, raw, [], start, end, places=Decimal("0.01"))
        raw = call_with_retry(lambda: _ak().stock_us_daily(symbol=inst.code, adjust=""),
                              name=f"sina.us_daily.{inst.symbol}", limiter=_sina)
        return build_bars(inst, raw, _actions_or_empty(self, inst), start, end, places=Decimal("0.0001"))

    def corporate_actions(self, inst: Instrument) -> list[CorporateAction]:
        if inst.type == "index":
            return []
        table = call_with_retry(lambda: _ak().stock_us_daily(symbol=inst.code, adjust="qfq-factor"),
                                name=f"sina.us_factor.{inst.symbol}", limiter=_sina, attempts=2)
        return actions_from_factor_table(inst.symbol, table, "qfq_factor", "adjust", "sina.us_factor")
