"""个股新闻（PRD 4.5.1：AKShare stock_news_em，东方财富）。

- 接口只返回每个标的最近约 10 条新闻，因此历史回放基本拿不到当时的新闻；查询一律按发布时间 ≤ 快照时刻过滤，不会前视。
- 新闻缓存到数据库，同一标的在 TTL 内不重复请求。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import NewsRow

logger = logging.getLogger(__name__)

BEIJING = ZoneInfo("Asia/Shanghai")
FETCH_TTL_S = 1800
MAX_CONTENT = 400


@dataclass
class NewsItem:
    symbol: str
    published_at: datetime          # UTC
    title: str
    content: str
    source: str
    url: str


class EastmoneyNewsProvider:
    """东方财富个股新闻：A股 / 港股 / 美股 / ETF 都按交易所代码查询（600519、00700、AAPL）。"""

    name = "eastmoney_news"

    def fetch(self, symbol: str) -> list[NewsItem]:
        import akshare as ak

        df = ak.stock_news_em(symbol=symbol.split(".")[0])
        return parse_eastmoney(symbol, df)


def parse_eastmoney(symbol: str, df: pd.DataFrame) -> list[NewsItem]:
    out = []
    for _, row in df.iterrows():
        try:
            local = datetime.strptime(str(row["发布时间"]).strip()[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        title = str(row.get("新闻标题") or "").strip()
        url = str(row.get("新闻链接") or "").strip()
        if not title or not url:
            continue
        content = " ".join(str(row.get("新闻内容") or "").split())
        out.append(NewsItem(symbol, local.replace(tzinfo=BEIJING).astimezone(ZoneInfo("UTC")), title,
                            content[:MAX_CONTENT], str(row.get("文章来源") or ""), url))
    return out


class NewsService:
    def __init__(self, session: Session, provider):
        self.s = session
        self.provider = provider

    _fetched: dict[str, float] = {}       # 代码 → 上次请求时间（进程内）

    def refresh(self, symbols: list[str]) -> None:
        """拉取并缓存新闻；单个标的失败不影响其他标的。"""
        now = time.monotonic()
        for sym in symbols:
            if now - self._fetched.get(sym, -1e9) < FETCH_TTL_S:
                continue
            try:
                items = self.provider.fetch(sym)
            except Exception as e:  # 新闻是辅助信息，失败只记录日志
                logger.warning("新闻获取失败 %s：%s", sym, e)
                continue
            self._fetched[sym] = now
            known = set(self.s.scalars(select(NewsRow.url).where(NewsRow.symbol == sym)))
            for it in items:
                if it.url not in known:
                    self.s.add(NewsRow(symbol=sym, published_at=it.published_at, title=it.title, content=it.content,
                                       source=it.source, url=it.url))
                    known.add(it.url)
        self.s.flush()

    def recent(self, symbols: list[str], until: datetime, days: int = 3, per_symbol: int = 3) -> list[NewsRow]:
        """until 之前 days 天内发布的新闻（按发布时间倒序），每个标的最多 per_symbol 条。"""
        if not symbols:
            return []
        rows = self.s.scalars(select(NewsRow).where(
            NewsRow.symbol.in_(symbols), NewsRow.published_at <= until,
            NewsRow.published_at > until - timedelta(days=days)).order_by(NewsRow.published_at.desc()))
        out, count = [], {}
        for r in rows:
            if count.get(r.symbol, 0) < per_symbol:
                out.append(r)
                count[r.symbol] = count.get(r.symbol, 0) + 1
        return out
