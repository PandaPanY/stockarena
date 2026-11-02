"""A股数据源：Tushare Pro（https://tushare.pro，用户自己注册并填写 token）。

直接调用 Tushare 的 HTTP 接口（不依赖 tushare 包）。各接口的积分要求不同（2026-11 查阅文档）：

- 股票日线 daily：120 积分起（免费注册即可，每分钟 50 次）；
- 交易日历 trade_cal、指数日线 index_daily、分红送股 dividend：2000 积分起；
- ETF 日线 fund_daily：5000 积分起。

积分不够时接口返回错误，这里原样报出（作为备用数据源时会退回新浪 / 腾讯）。

复权因子不用 adj_factor 接口（要 2000 积分），而是由日线自带的「昨收（除权价）」逐日推算：
因子(t) = 因子(t-1) × 收盘(t-1) ÷ 昨收(t)。起算基准是本次拉取的第一天，与缓存衔接时由 MarketDataService 按重叠日对齐。

token 不写入日志和错误信息。所有方法都是同步阻塞调用。
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

import httpx
import pandas as pd

from app.core.money import to_decimal
from app.market.retry import RateLimiter, call_with_retry
from app.market.types import CorporateAction, DailyBar, Instrument

logger = logging.getLogger(__name__)

API_URL = "https://api.tushare.pro"
TIMEOUT = 30
_limiter = RateLimiter(1.3)             # 120 积分每分钟 50 次


class TushareError(RuntimeError):
    """Tushare 接口返回错误（积分不足、频次超限、参数错误等）。"""


def _d(value) -> Decimal:
    return to_decimal(float(value))


def _ymd(d: date) -> str:
    return d.strftime("%Y%m%d")


def _date(s: str) -> date:
    return date(int(s[:4]), int(s[4:6]), int(s[6:8]))


class TushareClient:
    def __init__(self, token: str, transport: httpx.BaseTransport | None = None):
        self._token = token
        self._transport = transport

    def query(self, api: str, fields: str = "", **params) -> pd.DataFrame:
        """调用一个接口，返回 DataFrame。网络错误重试；接口返回的错误（如积分不足）不重试，直接抛出 TushareError。"""
        payload = {"api_name": api, "token": self._token, "params": params, "fields": fields}

        def post() -> dict:
            with httpx.Client(timeout=TIMEOUT, transport=self._transport) as c:
                r = c.post(API_URL, json=payload)
                r.raise_for_status()
                return r.json()

        body = call_with_retry(post, name=f"tushare.{api}", limiter=_limiter)
        if body.get("code") != 0:
            raise TushareError(f"Tushare {api}：{body.get('msg') or body.get('code')}")
        data = body.get("data") or {}
        return pd.DataFrame(data.get("items") or [], columns=data.get("fields") or [])


class TushareCNProvider:
    market = "CN"
    delay_minutes = 0

    def __init__(self, token: str, transport: httpx.BaseTransport | None = None):
        self.client = TushareClient(token, transport)

    # ---------- 交易日历 ----------

    def trading_days(self) -> list[date]:
        """上交所交易日（覆盖到明年年底）。"""
        end = date(date.today().year + 1, 12, 31)
        df = self.client.query("trade_cal", "cal_date,is_open", exchange="SSE", start_date="20050101",
                               end_date=_ymd(end))
        return sorted(_date(str(r.cal_date)) for r in df.itertuples() if str(r.is_open) == "1")

    # ---------- 日线 ----------

    def daily_bars(self, inst: Instrument, start: date, end: date) -> list[DailyBar]:
        api = {"index": "index_daily", "etf": "fund_daily", "lof": "fund_daily"}.get(inst.type, "daily")
        df = self.client.query(api, "trade_date,open,high,low,close,pre_close,vol,amount",
                               ts_code=inst.symbol, start_date=_ymd(start), end_date=_ymd(end))
        return self.build_bars(inst, df)

    @staticmethod
    def build_bars(inst: Instrument, df: pd.DataFrame) -> list[DailyBar]:
        """Tushare 日线 → DailyBar：成交量「手」→ 股（份），成交额「千元」→ 元；复权因子由昨收逐日推算。"""
        if df.empty:
            return []
        df = df.sort_values("trade_date").reset_index(drop=True)
        bars: list[DailyBar] = []
        factor = Decimal("1")
        prev_close: Decimal | None = None
        for r in df.itertuples(index=False):
            close = _d(r.close)
            pre = _d(r.pre_close) if pd.notna(r.pre_close) else None
            if inst.type != "index" and prev_close is not None and pre and pre > 0 and pre != prev_close:
                factor = factor * prev_close / pre          # 除权除息日：昨收是除权参考价
            bars.append(DailyBar(
                symbol=inst.symbol, date=_date(str(r.trade_date)),
                open=_d(r.open), high=_d(r.high), low=_d(r.low), close=close,
                volume=int(round(float(r.vol or 0) * 100)), amount=_d(float(r.amount or 0) * 1000),
                prev_close=pre, adj_factor=factor.quantize(Decimal("0.0000000001")),
            ))
            prev_close = close
        return bars

    # ---------- 除权除息 ----------

    def corporate_actions(self, inst: Instrument) -> list[CorporateAction]:
        if inst.type == "index":
            return []
        if inst.type in ("etf", "lof"):
            raise TushareError("Tushare 数据源暂不支持基金分红")
        df = self.client.query("dividend", "ex_date,div_proc,stk_div,cash_div_tax", ts_code=inst.symbol)
        by_date: dict[date, CorporateAction] = {}
        for r in df.itertuples(index=False):
            if r.div_proc != "实施" or not r.ex_date:
                continue
            d = _date(str(r.ex_date))
            by_date[d] = CorporateAction(symbol=inst.symbol, ex_date=d,
                                         cash_per_share=_d(r.cash_div_tax or 0),
                                         shares_per_share=_d(r.stk_div or 0), source="tushare.dividend")
        return sorted(by_date.values(), key=lambda a: a.ex_date)
