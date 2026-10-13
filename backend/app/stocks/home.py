"""首页：各市场指数与 A股涨跌概况、明星股、我的组合今天 vs 基准。

- 指数与明星股的价格来自腾讯实时行情；指数的小走势图用库里的日线（没有时在后台补齐，不阻塞首页）。
- A股涨跌家数、涨停跌停来自乐咕乐股（AKShare），成交额为上证指数与深证成指成交额之和，缓存 60 秒。
- 我的组合：跟踪中的组合（规则账户、AI 复核账户）与单独跟踪的策略。收盘后用净值记录；盘中按持仓与实时价估算当天收益
  （只作参考，正式结果以收盘后的净值为准）。
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DEFAULT_USER_ID, AccountRow, DailyBarRow, NavSnapshotRow, PositionRow, TrackRow
from app.models.quant import QuantPortfolioRow, QuantStrategyRow
from app.models.settings import AppSettingRow, user_key
from app.stocks import service as sv

logger = logging.getLogger(__name__)

HOME_INDICES: dict[str, list[str]] = {
    "CN": ["000001.SH", "399001.SZ", "399006.SZ", "000300.SH", "000688.SH"],
    "HK": ["HSI.HK", "HSTECH.HK", "HSCEI.HK"],
    "US": ["DJI.US", "INX.US", "IXIC.US"],
}
MARKET_NAMES = {"CN": "A股", "HK": "港股", "US": "美股"}
SPARK_DAYS = 60
STARS_KEY = "home.stars"
DEFAULT_STARS = [
    "600519.SH", "300750.SZ", "002594.SZ", "600036.SH", "601318.SH",
    "00700.HK", "09988.HK", "03690.HK", "01810.HK",
    "AAPL.US", "NVDA.US", "MSFT.US", "TSLA.US",
]
MAX_STARS = 30

_cache: dict[str, tuple[float, object]] = {}
_refreshing = threading.Lock()
_last_refresh = [0.0]


def _cached(key: str, ttl: float, fn):
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    value = fn()
    _cache[key] = (time.monotonic(), value)
    return value


# ---------------------------------------------------------------- 指数与涨跌概况


def breadth() -> dict | None:
    """A股涨跌家数（乐咕乐股）；失败时返回 None。"""
    def fetch():
        import akshare as ak

        try:
            df = ak.stock_market_activity_legu()
        except Exception as e:  # noqa: BLE001
            logger.info("涨跌家数获取失败：%s", e)
            return None
        v = dict(zip(df["item"].astype(str), df["value"], strict=False))

        def n(k: str) -> int | None:
            try:
                return int(float(v[k]))
            except (KeyError, TypeError, ValueError):
                return None

        return {"up": n("上涨"), "down": n("下跌"), "flat": n("平盘"), "limit_up": n("真实涨停"),
                "limit_down": n("真实跌停"), "suspended": n("停牌"), "activity": str(v.get("活跃度", "")),
                "time": str(v.get("统计日期", ""))}

    return _cached("breadth", 60, fetch)


def _sparks(s: Session, symbols: list[str]) -> dict[str, list[float]]:
    out = {}
    for sym in symbols:
        rows = s.execute(select(DailyBarRow.close).where(DailyBarRow.symbol == sym)
                         .order_by(DailyBarRow.date.desc()).limit(SPARK_DAYS)).scalars().all()
        out[sym] = [round(float(c), 2) for c in reversed(rows)]
    return out


def _refresh_indices_later() -> None:
    """后台补齐指数日线（每 10 分钟最多一次），首页不等它。"""
    if time.monotonic() - _last_refresh[0] < 600 or not _refreshing.acquire(blocking=False):
        return
    _last_refresh[0] = time.monotonic()

    def work():
        from app.db.session import get_sessionmaker

        try:
            with get_sessionmaker()() as s:
                for syms in HOME_INDICES.values():
                    for sym in syms:
                        sv.ensure_history(s, sv.resolve(s, sym))
        except Exception:  # noqa: BLE001
            logger.exception("首页指数日线补齐失败")
        finally:
            _refreshing.release()

    threading.Thread(target=work, daemon=True, name="home-indices").start()


def markets(s: Session) -> list[dict]:
    symbols = [x for syms in HOME_INDICES.values() for x in syms]
    quotes = _cached("index_quotes", 20, lambda: sv.quotes(symbols))
    sparks = _sparks(s, symbols)
    if any(len(v) < 2 for v in sparks.values()):
        _refresh_indices_later()
    out = []
    for market, syms in HOME_INDICES.items():
        items = []
        for sym in syms:
            q = quotes.get(sym) or {}
            items.append({"symbol": sym, "name": sv.INDICES[sym][0], "price": q.get("price"),
                          "change_pct": q.get("change_pct"), "time": q.get("time"), "amount": q.get("amount"),
                          "spark": sparks.get(sym, [])})
        card = {"market": market, "name": MARKET_NAMES[market], "indices": items}
        if market == "CN":
            amount = sum(i["amount"] or 0 for i in items if i["symbol"] in ("000001.SH", "399001.SZ"))
            card["breadth"] = breadth()
            card["turnover"] = amount or None
        out.append(card)
    return out


# ---------------------------------------------------------------- 明星股


def stars(s: Session, uid: int = DEFAULT_USER_ID) -> list[str]:
    row = s.get(AppSettingRow, user_key(STARS_KEY, uid))
    v = (row.value or {}).get("symbols") if row else None
    return list(v) if isinstance(v, list) else list(DEFAULT_STARS)


def set_stars(s: Session, symbols: list[str], uid: int = DEFAULT_USER_ID) -> list[str]:
    clean: list[str] = []
    for x in symbols:
        sym = sv.normalize_symbol(x)
        if sym not in clean:
            clean.append(sym)
    clean = clean[:MAX_STARS]
    key = user_key(STARS_KEY, uid)
    row = s.get(AppSettingRow, key) or AppSettingRow(key=key)
    row.value = {"symbols": clean}
    s.add(row)
    s.flush()
    return clean


def star_quotes(s: Session, uid: int = DEFAULT_USER_ID) -> list[dict]:
    syms = stars(s, uid)
    quotes = _cached("star_quotes:" + ",".join(syms), 20, lambda: sv.quotes(syms))
    out = []
    for sym in syms:
        q = quotes.get(sym) or {}
        out.append({"symbol": sym, "name": q.get("name") or sym, "market": sv.market_of(sym),
                    "price": q.get("price"), "change_pct": q.get("change_pct"), "time": q.get("time")})
    return out


# ---------------------------------------------------------------- 我的组合


def _account_summary(s: Session, acc_id: int, label: str) -> dict | None:
    acc = s.get(AccountRow, acc_id)
    if acc is None:
        return None
    track = s.get(TrackRow, acc.track_id)
    navs = s.execute(select(NavSnapshotRow).where(NavSnapshotRow.account_id == acc_id)
                     .order_by(NavSnapshotRow.date)).scalars().all()
    out: dict = {"account_id": acc_id, "label": label, "track_status": track.status if track else None,
                 "benchmark": track.benchmark_symbol if track else None,
                 "benchmark_name": sv.INDICES.get(track.benchmark_symbol, ("",))[0] if track else ""}
    if not navs:
        return out
    last, first = navs[-1], navs[0]
    prev = navs[-2] if len(navs) > 1 else None
    nav = float(last.nav)

    def ratio(a, b) -> float | None:
        return float(a) / float(b) - 1 if a and b and float(b) > 0 else None

    out |= {"date": last.date.isoformat(), "nav": nav, "since": first.date.isoformat(),
            "day": ratio(last.nav, prev.nav) if prev else None,
            "bench_day": ratio(last.benchmark_close, prev.benchmark_close) if prev else None,
            "total": ratio(last.nav, first.nav), "bench_total": ratio(last.benchmark_close, first.benchmark_close)}
    # 盘中估算：实时价比最后一次净值记录更新时，按持仓与实时价估算当天的收益
    positions = s.execute(select(PositionRow.symbol, PositionRow.qty).where(
        PositionRow.account_id == acc_id, PositionRow.qty > 0)).all()
    if not positions:          # 还没有持仓（如刚建仓、成交尚未结算）时不估算
        return out
    syms = [p.symbol for p in positions]
    if track and track.benchmark_symbol:
        syms.append(track.benchmark_symbol)
    quotes = _cached("acct_quotes:" + ",".join(sorted(syms)), 20, lambda: sv.quotes(syms))
    bq = quotes.get(track.benchmark_symbol) if track else None
    if not bq or not _quote_after(bq.get("time"), last.date):
        return out
    pnl = 0.0
    for sym, qty in positions:
        q = quotes.get(sym)
        if q and _quote_after(q.get("time"), last.date):
            pnl += qty * (q["price"] - q["prev_close"])
    out["intraday"] = {"est": pnl / nav if nav else None, "bench": bq.get("change_pct"), "time": bq.get("time")}
    return out


def _quote_after(t: str | None, d: date) -> bool:
    """行情时间是否晚于 d（即当天的行情尚未计入净值）。"""
    import re

    m = re.match(r"(\d{4})[-/]?(\d{2})[-/]?(\d{2})", t or "")
    return bool(m) and date(int(m[1]), int(m[2]), int(m[3])) > d


def my_portfolios(s: Session, uid: int = DEFAULT_USER_ID) -> list[dict]:
    from app.quant import review

    out = []
    q = select(QuantPortfolioRow).where(QuantPortfolioRow.user_id == uid).order_by(QuantPortfolioRow.id)
    for pf in s.scalars(q):
        if not pf.follow_account_id:
            continue
        accounts = [_account_summary(s, pf.follow_account_id, "纯规则")]
        ai_id = review.settings(pf).get("account_id")
        if ai_id and review.settings(pf).get("enabled"):
            accounts.append(_account_summary(s, ai_id, "规则 + AI 复核"))
        tilt_cfg = (pf.settings or {}).get("tilt") or {}
        if tilt_cfg.get("enabled") and tilt_cfg.get("account_id"):
            accounts.append(_account_summary(s, tilt_cfg["account_id"], "自选倾斜"))
        accounts = [a for a in accounts if a]
        if accounts and accounts[0].get("track_status") == "running":
            out.append({"kind": "portfolio", "id": pf.id, "name": pf.name, "accounts": accounts})
    for st in s.scalars(select(QuantStrategyRow).where(QuantStrategyRow.follow_account_id.is_not(None),
                                                     QuantStrategyRow.user_id == uid)):
        a = _account_summary(s, st.follow_account_id, "规则")
        if a and a.get("track_status") == "running":
            out.append({"kind": "strategy", "id": st.id, "name": st.name, "accounts": [a]})
    return out


def home(s: Session, uid: int = DEFAULT_USER_ID) -> dict:
    """首页：各市场概况（公共）、该用户的明星股与组合。"""
    return {"markets": markets(s), "stars": star_quotes(s, uid), "portfolios": my_portfolios(s, uid),
            "today": date.today().isoformat()}
