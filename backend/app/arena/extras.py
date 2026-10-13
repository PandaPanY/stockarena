"""快照模式附带的数据：财报指标与新闻（PRD 3.3 <news>、US-1.5）。同一轮所有选手看到相同内容。

防前视：财报只用 available_from ≤ 决策日期的；新闻只用发布时间 ≤ 快照时刻的。历史回放不附带新闻
（新闻接口只有最近几条，回放时拿到的会少于实时运行，造成信息不一致；PRD 4.5.4）。
"""

from __future__ import annotations

import logging
from datetime import date, datetime

from sqlalchemy.orm import Session

from app.arena.deps import fundamentals_service, news_service
from app.core.timeutils import to_market_time
from app.market.fundamentals import format_value
from app.market.types import Instrument

logger = logging.getLogger(__name__)

NEWS_MAX_SYMBOLS = 20
NEWS_MAX_LINES = 30
FUND_COLUMNS = ["revenue", "revenue_yoy", "net_profit", "net_profit_yoy", "roe", "gross_margin", "debt_ratio"]
FUND_HEADER = "代码,名称,报告期,营收(亿),营收同比%,净利润(亿),净利同比%,ROE%,毛利率%,资产负债率%"


def _clean(text: str) -> str:
    """外部文本去掉尖括号与换行，避免伪造数据标签或注入指令格式。"""
    return " ".join(text.replace("<", "＜").replace(">", "＞").split())


def fundamentals_table(s: Session, market: str, instruments: dict[str, Instrument], as_of: date) -> str:
    stocks = [sym for sym, inst in instruments.items() if inst.type == "stock"]
    svc = fundamentals_service(s)
    svc.refresh(stocks, market)
    rows = [FUND_HEADER]
    for sym in stocks:
        latest = svc.latest(sym, as_of, n=1)
        if not latest:
            continue
        r = latest[0]
        values = [format_value(k, r.data.get(k)) for k in FUND_COLUMNS]
        rows.append(",".join([sym, instruments[sym].name, str(r.period), *values]))
    return "\n".join(rows) if len(rows) > 1 else ""


def news_text(s: Session, symbols: list[str], names: dict[str, str], until: datetime, market: str) -> str:
    svc = news_service(s)
    picked = list(dict.fromkeys(symbols))[:NEWS_MAX_SYMBOLS]
    svc.refresh(picked)
    lines = []
    for r in svc.recent(picked, until, days=3, per_symbol=2)[:NEWS_MAX_LINES]:
        when = to_market_time(r.published_at, market)
        summary = _clean(r.content)[:120]
        lines.append(f"- {when:%m-%d %H:%M} [{r.symbol} {names.get(r.symbol, '')}] {_clean(r.title)}"
                     + (f"：{summary}" if summary and summary not in r.title else ""))
    return "\n".join(lines)


def news_symbols(held: set[str], movers: list[str]) -> list[str]:
    """新闻范围：任一选手持有的标的 + 当日涨跌幅最大的标的（所有选手相同）。"""
    return [*sorted(held), *[m for m in movers if m not in held]]


def safe(fn, *args, **kwargs) -> str:
    """附加数据失败不影响决策轮次。"""
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        logger.warning("附加数据获取失败：%s", e)
        return ""
