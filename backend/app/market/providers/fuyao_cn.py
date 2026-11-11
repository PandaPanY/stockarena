"""A股数据源：同花顺金融数据 API（https://fuyao.aicubes.cn，用户用同花顺账号自己申请 API Key）。

只用于股票：不复权日线、分红送转事件、交易日历。实测（2026-11）与新浪的不复权收盘价、成交量逐日一致，
复权后的每日收益几乎一致；股票历史从 2005 年起。不用于：
- ETF（只有前复权价、近 5 年）、指数（近 5 年）：调用时报错，由其他数据源处理；
- 已退市股票（查不到代码）；财报（披露日是最近一次披露的日期，不是最初公布的日期，不能用来避免前视偏差）。

复权因子由分红送转事件推算（与港美股、A股 ETF 共用 adjust.build_bars），起算基准是本次拉取的第一天，
与缓存衔接时由 MarketDataService 按重叠日对齐。Key 从文件读取（配置 fuyao_key_file），不写入日志和错误信息。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pandas as pd

from app.market.providers.adjust import build_bars
from app.market.retry import RateLimiter, call_with_retry
from app.market.types import CorporateAction, DailyBar, Instrument

logger = logging.getLogger(__name__)

BASE = "https://fuyao.aicubes.cn"
TIMEOUT = 30
TZ = ZoneInfo("Asia/Shanghai")
MAX_YEARS = 9                   # 单次最多 10 个自然年，留余量
CALENDAR_START = date(2005, 1, 1)
NO_EVENTS = 3002                # 「没有复权事件」
_LOOKBACK_DAYS = 20             # 多取一段，算第一天的前收盘价
_limiter = RateLimiter(0.2)


class FuyaoError(RuntimeError):
    """接口返回错误（参数、权限、找不到代码等）。"""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


def _ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=TZ).timestamp() * 1000)


def _day(v: int) -> date:
    return datetime.fromtimestamp(v / 1000, TZ).date()


def read_key(path: Path | str | None) -> str | None:
    """从文件读取 Key；文件不存在或为空时返回 None。"""
    if not path:
        return None
    try:
        key = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return key or None


class FuyaoClient:
    def __init__(self, key: str, transport: httpx.BaseTransport | None = None):
        self._key = key
        self._transport = transport

    def get(self, path: str, **params) -> dict:
        """调用一个接口，返回 data。网络错误重试；接口返回的错误不重试，抛出 FuyaoError。"""
        def fetch() -> dict:
            with httpx.Client(timeout=TIMEOUT, transport=self._transport,
                              headers={"X-api-key": self._key}) as c:
                r = c.get(BASE + path, params=params)
                if r.status_code == 429:
                    raise httpx.HTTPStatusError("被限流（HTTP 429）", request=r.request, response=r)
                r.raise_for_status()
                return r.json()

        body = call_with_retry(fetch, name=f"fuyao.{path.rsplit('/', 2)[-2]}", limiter=_limiter)
        if body.get("code") != 0:
            raise FuyaoError(f"同花顺 {path}：{body.get('message') or body.get('code')}", body.get("code"))
        return body.get("data") or {}


class FuyaoCNProvider:
    market = "CN"
    delay_minutes = 0

    def __init__(self, key: str, transport: httpx.BaseTransport | None = None):
        self.client = FuyaoClient(key, transport)

    # ---------- 交易日历 ----------

    def trading_days(self) -> list[date]:
        """2005 年起的交易日。不含未来两周的日期时视为失败（不覆盖缓存里排到年底的日历）。"""
        end = date(date.today().year + 1, 12, 31)
        days: set[date] = set()
        a = CALENDAR_START
        while a <= end:
            b = min(end, a.replace(year=a.year + MAX_YEARS))
            data = self.client.get("/api/a-share/calendar/trading-days", start_date=a.strftime("%Y%m%d"),
                                   end_date=b.strftime("%Y%m%d"))
            days |= {date(int(x["date"][:4]), int(x["date"][4:6]), int(x["date"][6:8])) for x in data.get("item") or []}
            a = b + timedelta(days=1)
        if not days or max(days) < date.today() + timedelta(days=14):
            raise FuyaoError("同花顺交易日历不含之后两周的日期，不使用")
        return sorted(days)

    # ---------- 日线 ----------

    def _raw(self, inst: Instrument, start: date, end: date) -> pd.DataFrame:
        rows: list[dict] = []
        a = start
        while a <= end:
            b = min(end, a.replace(year=a.year + MAX_YEARS))
            data = self.client.get("/api/a-share/prices/historical", thscode=inst.symbol, interval="1d",
                                   start=_ms(a), end=_ms(b), adjust="none")
            rows += data.get("item") or []
            a = b + timedelta(days=1)
        if not rows:
            return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume", "amount"])
        df = pd.DataFrame([{"date": _day(x["date_ms"]), "open": x["open_price"], "high": x["high_price"],
                            "low": x["low_price"], "close": x["close_price"], "volume": x.get("volume") or 0,
                            "amount": x.get("turnover")} for x in rows])
        return df.drop_duplicates("date").sort_values("date").reset_index(drop=True)

    def daily_bars(self, inst: Instrument, start: date, end: date) -> list[DailyBar]:
        if inst.type != "stock":
            raise FuyaoError(f"同花顺数据源只用于股票（{inst.symbol} 是 {inst.type}）")
        raw = self._raw(inst, start - timedelta(days=_LOOKBACK_DAYS), end)
        actions = [a for a in self.corporate_actions(inst) if a.ex_date <= end]
        return build_bars(inst, raw, actions, start, end, places=Decimal("0.01"))

    # ---------- 除权除息 ----------

    def corporate_actions(self, inst: Instrument) -> list[CorporateAction]:
        if inst.type == "index":
            return []
        if inst.type != "stock":
            raise FuyaoError(f"同花顺数据源只用于股票（{inst.symbol} 是 {inst.type}）")
        try:
            data = self.client.get("/api/a-share/corporate-actions/adjustment-factors", thscode=inst.symbol)
        except FuyaoError as e:
            if e.code == NO_EVENTS:
                return []
            raise
        out = {}
        for x in data.get("item") or []:
            d = _day(x["ex_date_ms"])
            out[d] = CorporateAction(symbol=inst.symbol, ex_date=d,
                                     cash_per_share=Decimal(str(x.get("dividend_per_share") or 0)),
                                     shares_per_share=Decimal(str(x.get("per_share_bonus") or 0)),
                                     source="fuyao.adjustment_factors")
        return sorted(out.values(), key=lambda a: a.ex_date)
