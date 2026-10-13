"""财报指标（PRD US-1.5 get_fundamentals）。

数据源：A股同花顺财务摘要、港股 / 美股东方财富主要指标（均经 AKShare）。
防前视：每份财报记录「最早可用日期」available_from，查询只返回 available_from ≤ 决策日期的财报：
- 有公告日（美股）：公告日的下一天
- 没有公告日（A股、港股）：按法定披露截止日（宁晚勿早），A股一季报 4/30、半年报 8/31、三季报 10/31、年报次年 4/30；
  港股中期报告期末后 2 个月、年报期末后 3 个月
"""

from __future__ import annotations

import logging
import math
import re
import time
from dataclasses import dataclass, field
from datetime import date, timedelta

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import FundamentalRow

logger = logging.getLogger(__name__)

FETCH_TTL_S = 7 * 24 * 3600
# 统一的指标字段（金额单位：亿元 / 亿港元 / 亿美元；比例为百分数）
FIELDS = {
    "revenue": "营业收入（亿）",
    "revenue_yoy": "营收同比 %",
    "net_profit": "归母净利润（亿）",
    "net_profit_yoy": "净利润同比 %",
    "eps": "每股收益",
    "bps": "每股净资产",
    "roe": "ROE %",
    "gross_margin": "毛利率 %",
    "net_margin": "净利率 %",
    "debt_ratio": "资产负债率 %",
}


@dataclass
class FundamentalRecord:
    symbol: str
    period: date
    available_from: date
    data: dict[str, float | None] = field(default_factory=dict)


def _month_end(y: int, m: int) -> date:
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    return nxt - timedelta(days=1)


def cn_available_from(period: date) -> date:
    """A股法定披露截止日。"""
    if period.month == 3:
        return date(period.year, 4, 30)
    if period.month == 6:
        return date(period.year, 8, 31)
    if period.month == 9:
        return date(period.year, 10, 31)
    return date(period.year + 1, 4, 30)


def hk_available_from(period: date) -> date:
    months = 3 if period.month == 12 else 2
    m = period.month + months
    return _month_end(period.year + (m - 1) // 12, (m - 1) % 12 + 1)


_UNITS = {"万亿": 1e4, "亿": 1.0, "万": 1e-4}


def _cn_number(v) -> float | None:
    """解析同花顺的「1.47亿」「23.38%」「False」，金额统一为亿元。"""
    if v is None or isinstance(v, bool):
        return None
    s = str(v).strip().replace(",", "")
    if s in ("", "False", "--", "nan", "None"):
        return None
    m = re.fullmatch(r"(-?\d+(?:\.\d+)?)(万亿|亿|万)?(%)?", s)
    if not m:
        return None
    x = float(m.group(1))
    if m.group(2):
        x *= _UNITS[m.group(2)]
    return x


def _num(v, scale: float = 1.0) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) else x * scale


def parse_cn(symbol: str, df: pd.DataFrame) -> list[FundamentalRecord]:
    out = []
    for _, r in df.iterrows():
        try:
            period = date.fromisoformat(str(r["报告期"])[:10])
        except ValueError:
            continue
        out.append(FundamentalRecord(symbol, period, cn_available_from(period), {
            "revenue": _cn_number(r.get("营业总收入")), "revenue_yoy": _cn_number(r.get("营业总收入同比增长率")),
            "net_profit": _cn_number(r.get("净利润")), "net_profit_yoy": _cn_number(r.get("净利润同比增长率")),
            "eps": _cn_number(r.get("基本每股收益")), "bps": _cn_number(r.get("每股净资产")),
            "roe": _cn_number(r.get("净资产收益率")), "gross_margin": _cn_number(r.get("销售毛利率")),
            "net_margin": _cn_number(r.get("销售净利率")), "debt_ratio": _cn_number(r.get("资产负债率")),
        }))
    return out


def _em_record(symbol: str, r, available_from: date, profit_key: str) -> FundamentalRecord:
    return FundamentalRecord(symbol, date.fromisoformat(str(r["REPORT_DATE"])[:10]), available_from, {
        "revenue": _num(r.get("OPERATE_INCOME"), 1e-8), "revenue_yoy": _num(r.get("OPERATE_INCOME_YOY")),
        "net_profit": _num(r.get(profit_key), 1e-8), "net_profit_yoy": _num(r.get(f"{profit_key}_YOY")),
        "eps": _num(r.get("BASIC_EPS")), "bps": _num(r.get("BPS")), "roe": _num(r.get("ROE_AVG")),
        "gross_margin": _num(r.get("GROSS_PROFIT_RATIO")), "net_margin": _num(r.get("NET_PROFIT_RATIO")),
        "debt_ratio": _num(r.get("DEBT_ASSET_RATIO")),
    })


def parse_hk(symbol: str, df: pd.DataFrame) -> list[FundamentalRecord]:
    out = []
    for _, r in df.iterrows():
        period = date.fromisoformat(str(r["REPORT_DATE"])[:10])
        out.append(_em_record(symbol, r, hk_available_from(period), "HOLDER_PROFIT"))
    return out


def parse_us(symbol: str, df: pd.DataFrame) -> list[FundamentalRecord]:
    out = []
    for _, r in df.iterrows():
        notice = r.get("NOTICE_DATE")
        period = date.fromisoformat(str(r["REPORT_DATE"])[:10])
        avail = (date.fromisoformat(str(notice)[:10]) + timedelta(days=1)) if notice and str(notice) != "nan" \
            else hk_available_from(period)
        out.append(_em_record(symbol, r, avail, "PARENT_HOLDER_NETPROFIT"))
    return out


class AkshareFundamentalsProvider:
    name = "akshare_fundamentals"

    def fetch(self, symbol: str, market: str) -> list[FundamentalRecord]:
        import akshare as ak

        code = symbol.split(".")[0]
        if market == "CN":
            return parse_cn(symbol, ak.stock_financial_abstract_ths(symbol=code, indicator="按报告期"))
        if market == "HK":
            return parse_hk(symbol, ak.stock_financial_hk_analysis_indicator_em(symbol=code, indicator="报告期"))
        if market == "US":
            return parse_us(symbol, ak.stock_financial_us_analysis_indicator_em(symbol=code, indicator="单季报"))
        return []


class FundamentalsService:
    _fetched: dict[str, float] = {}

    def __init__(self, session: Session, provider):
        self.s = session
        self.provider = provider

    def refresh(self, symbols: list[str], market: str) -> None:
        now = time.monotonic()
        for sym in symbols:
            if now - self._fetched.get(sym, -1e9) < FETCH_TTL_S:
                continue
            try:
                records = self.provider.fetch(sym, market)
            except Exception as e:  # 基本面是辅助信息，失败只记录日志
                logger.warning("财报获取失败 %s：%s", sym, e)
                continue
            self._fetched[sym] = now
            for rec in records:
                row = self.s.get(FundamentalRow, (sym, rec.period)) or FundamentalRow(symbol=sym, period=rec.period)
                row.available_from, row.data, row.source = rec.available_from, rec.data, self.provider.name
                self.s.add(row)
        self.s.flush()

    def latest(self, symbol: str, as_of: date, n: int = 4) -> list[FundamentalRow]:
        """as_of 当天已经可以使用的最近 n 期财报（报告期倒序）。"""
        return list(self.s.scalars(select(FundamentalRow).where(
            FundamentalRow.symbol == symbol, FundamentalRow.available_from <= as_of)
            .order_by(FundamentalRow.period.desc()).limit(n)))


def format_value(key: str, v: float | None) -> str:
    if v is None:
        return "-"
    return f"{v:.2f}" if key in ("eps", "bps") else f"{v:.1f}"
