"""股票详情的数据。

- 股票池里的股票（约 800 只）已有完整日线，直接用；股票池以外的 A股 / 港股 / 美股与常用指数，第一次打开时
  按需补齐日线（自 2016 年起）并入库，之后增量更新。
- 只补到「已收盘且数据稳定」的交易日（各市场 data_ready 之后才算当天），不会把盘中未完成的日线写进库里
  ——研究与回测共用这份日线；当天的实时价格另由新浪行情快照提供，只用于展示。
- 与平台的关联全部来自已有记录：跟踪账户的持仓、组合规则账户最近一次决策中各成员策略的选股、
  AI 复核记录，以及在所属股票池中的因子排名（只描述高低，不做好坏判断）。
"""

from __future__ import annotations

import json
import logging
import math
import re
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.timeutils import utcnow
from app.market.markets import get_market
from app.market.symbols import normalize
from app.market.types import Instrument
from app.models import (
    DEFAULT_USER_ID,
    AccountRow,
    AgentRow,
    CorporateActionRow,
    DailyBarRow,
    DecisionRow,
    FundamentalRow,
    IndustryRow,
    InstrumentRow,
    PositionRow,
    RoundRow,
    TrackRow,
)
from app.models.quant import QuantPortfolioRow

logger = logging.getLogger(__name__)

HISTORY_START = date(2016, 1, 1)

# 常用指数：代码 → (名称, 市场)。新浪实时行情代码另见 quote_code
INDICES: dict[str, tuple[str, str]] = {
    "000001.SH": ("上证指数", "CN"), "399001.SZ": ("深证成指", "CN"), "399006.SZ": ("创业板指", "CN"),
    "000300.SH": ("沪深300", "CN"), "000688.SH": ("科创50", "CN"), "000016.SH": ("上证50", "CN"),
    "000905.SH": ("中证500", "CN"), "000906.SH": ("中证800", "CN"),
    "HSI.HK": ("恒生指数", "HK"), "HSTECH.HK": ("恒生科技", "HK"), "HSCEI.HK": ("国企指数", "HK"),
    "DJI.US": ("道琼斯", "US"), "INX.US": ("标普500", "US"), "IXIC.US": ("纳斯达克", "US"),
    "NDX.US": ("纳斯达克100", "US"),
}

# 因子排名展示：分组 → [(因子, 说明)]
RANK_GROUPS: list[tuple[str, list[str]]] = [
    ("估值", ["ep", "bp", "dividend_yield"]),
    ("质量", ["roe", "gross_margin", "net_margin", "debt_ratio"]),
    ("成长", ["profit_growth", "revenue_growth", "growth_accel"]),
    ("走势", ["momentum", "reversal", "sharpe_mom", "price_position", "ma_trend"]),
    ("风险", ["volatility", "downside_vol", "beta"]),
    ("规模与流动性", ["size", "turnover", "amount_avg"]),
]
RANK_UNIVERSES = ("cn_csi300_hist", "cn_csi800_hist")      # 最后一天的成分与中证指数公司当前名单一致

_corp_fetched: set[str] = set()
_rank_cache: dict[tuple, dict] = {}


class StockError(Exception):
    pass


# ---------------------------------------------------------------- 证券与日线


def market_of(symbol: str) -> str:
    suffix = symbol.rsplit(".", 1)[-1].upper() if "." in symbol else ""
    if suffix == "HK":
        return "HK"
    if suffix == "US":
        return "US"
    if suffix in ("SH", "SZ", "BJ"):
        return "CN"
    raise StockError(f"无法识别的代码：{symbol}（A股如 600519.SH，港股如 00700.HK，美股如 AAPL.US）")


def normalize_symbol(symbol: str) -> str:
    symbol = symbol.strip()
    if symbol.upper() in INDICES:
        return symbol.upper()
    market = market_of(symbol)
    try:
        return normalize(symbol, market)
    except ValueError as e:
        raise StockError(str(e)) from None


def resolve(s: Session, symbol: str) -> Instrument:
    """代码 → 证券信息；数据库里没有的按市场规则生成并入库。"""
    from app.arena.deps import market_service, universe_resolver
    from app.market.service import _instrument_from_row
    from app.market.universe import UniverseResolver, index_instrument

    symbol = normalize_symbol(symbol)
    row = s.get(InstrumentRow, symbol)
    if row is not None:
        return _instrument_from_row(row)
    if symbol in INDICES:
        inst = index_instrument(symbol, INDICES[symbol][0])
    else:
        market = market_of(symbol)
        res = universe_resolver()
        if market == "CN":
            name = res.stock_names(date.today()).get(symbol.split(".")[0])
            if not name and not symbol.split(".")[0].startswith(("1", "5")):      # 基金不在股票名单里
                raise StockError(f"没有找到 A股 {symbol}")
            inst = UniverseResolver._cn_instrument({"symbol": symbol, "name": name or symbol.split(".")[0]})
        elif market == "HK":
            secs = res._cached("hkex_securities", timedelta(days=7), date.today(), res.sources["HK"].securities)
            if symbol.split(".")[0] not in secs:
                raise StockError(f"没有找到港股 {symbol}")
            inst = UniverseResolver._hk_instrument({"symbol": symbol}, secs)
        else:
            inst = UniverseResolver._us_instrument({"symbol": symbol})
    market_service(s, inst.market).upsert_instruments([inst])
    return inst


def settled_until(s: Session, market: str, now: datetime | None = None) -> date:
    """已经收盘且日线稳定的最近交易日：当地时间未到 data_ready 时不算当天。"""
    from app.arena.deps import market_service

    spec = get_market(market)
    local = (now or utcnow()).astimezone(spec.zone)
    d = local.date() if local.time() >= spec.data_ready else local.date() - timedelta(days=1)
    return market_service(s, market).calendar(local.date()).latest_on_or_before(d)


def ensure_history(s: Session, inst: Instrument) -> str:
    """按需补齐日线（股票池里的股票由日终流程更新，这里通常是 up-to-date）；失败时用库里已有的数据。"""
    from app.arena.deps import market_service

    svc = market_service(s, inst.market)
    try:
        until = settled_until(s, inst.market)
        status = svc.update_bars([inst], until, HISTORY_START).get(inst.symbol, "")
        if inst.type != "index" and inst.symbol not in _corp_fetched:
            _corp_fetched.add(inst.symbol)
            if s.scalar(select(CorporateActionRow.id).where(CorporateActionRow.symbol == inst.symbol).limit(1)) is None:
                svc.update_corporate_actions([inst])
        s.commit()
        return status
    except Exception as e:  # noqa: BLE001 - 数据源失败时仍展示已有数据
        s.rollback()
        logger.warning("补齐 %s 日线失败：%s", inst.symbol, e)
        return f"error: {e}"


def bars(s: Session, symbol: str, freq: str = "d", adjust: str = "qfq") -> list[list]:
    """K 线：[日期, 开, 收, 低, 高, 成交量]。adjust：qfq 前复权（以最新价为准）/ hfq 后复权 / none 不复权；
    freq：d 日 / w 周 / m 月。"""
    rows = s.execute(select(DailyBarRow.date, DailyBarRow.open, DailyBarRow.high, DailyBarRow.low, DailyBarRow.close,
                            DailyBarRow.volume, DailyBarRow.adj_factor)
                     .where(DailyBarRow.symbol == symbol).order_by(DailyBarRow.date)).all()
    if not rows:
        return []
    df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume", "factor"])
    for c in ("open", "high", "low", "close", "factor"):
        df[c] = df[c].astype(float)
    if adjust != "none":
        k = df["factor"] / (df["factor"].iloc[-1] if adjust == "qfq" else 1.0)
        for c in ("open", "high", "low", "close"):
            df[c] = df[c] * k
    if freq in ("w", "m"):
        df.index = pd.to_datetime(df["date"])
        g = df.resample("W-FRI" if freq == "w" else "ME")
        df = pd.DataFrame({"date": g["date"].last(), "open": g["open"].first(), "high": g["high"].max(),
                           "low": g["low"].min(), "close": g["close"].last(), "volume": g["volume"].sum()}).dropna()
    nd = 3 if df["close"].abs().median() < 10 else 2
    return [[d.isoformat() if hasattr(d, "isoformat") else str(d)[:10], round(o, nd), round(c, nd), round(lo, nd),
             round(h, nd), int(v)]
            for d, o, c, lo, h, v in zip(df["date"], df["open"], df["close"], df["low"], df["high"], df["volume"],
                                          strict=True)]


# ---------------------------------------------------------------- 搜索


def search(s: Session, q: str, limit: int = 20) -> list[dict]:
    """按代码或名称搜索：已入库的证券、全部 A股、常用指数；输入完整的港股 / 美股代码也可以直接打开。"""
    from app.arena.deps import universe_resolver

    q = q.strip()
    if not q:
        return []
    key = q.upper()
    found: dict[str, dict] = {}

    def add(symbol: str, name: str, market: str, kind: str) -> None:
        found.setdefault(symbol, {"symbol": symbol, "name": name, "market": market, "type": kind})

    for sym, (name, market) in INDICES.items():
        if key in sym or q in name:
            add(sym, name, market, "index")
    for r in s.execute(select(InstrumentRow.symbol, InstrumentRow.name, InstrumentRow.market, InstrumentRow.type)):
        if key in r.symbol.upper() or q in r.name or key in r.name.upper():
            add(r.symbol, r.name, r.market, r.type)
    try:
        names = universe_resolver().stock_names(date.today())
    except Exception:  # noqa: BLE001 - 名单获取失败时只搜索已入库的
        names = {}
    for code, name in names.items():
        if code.startswith(key) or q in name:
            try:
                add(normalize(code, "CN"), name, "CN", "stock")
            except ValueError:
                continue
        if len(found) > limit * 5:
            break
    if re.fullmatch(r"\d{1,5}\.HK|[A-Z][A-Z0-9.\-]{0,9}\.US", key):
        add(normalize_symbol(key), key, market_of(key), "stock")
    if len(found) < limit:          # 港股 / 美股的中文名、拼音首字母：用腾讯的搜索联想补充
        for it in smartbox(q):
            add(it["symbol"], it["name"], it["market"], it["type"])

    def rank(it: dict) -> tuple:
        code = it["symbol"].split(".")[0].upper()
        return (code != key, it["name"] != q, not code.startswith(key), it["type"] == "index", it["symbol"])

    return sorted(found.values(), key=rank)[:limit]


SMARTBOX_URL = "https://smartbox.gtimg.cn/s3/"


def parse_smartbox(text: str) -> list[dict]:
    """腾讯搜索联想：条目以 ^ 分隔，字段以 ~ 分隔：市场、代码、名称（Unicode 转义）、拼音、类型。
    只保留股票与基金（不要权证、牛熊证、场外美股）；指数只保留平台支持的常用指数。"""
    out = []
    m = re.search(r'v_hint="(.*)"', text, re.S)
    if not m or not m.group(1) or m.group(1) == "N":
        return out
    for entry in m.group(1).split("^"):
        f = entry.split("~")
        if len(f) < 5:
            continue
        mk, code, kind = f[0].lower(), f[1], f[4].upper()
        try:
            name = json.loads(f'"{f[2]}"')
        except ValueError:
            name = f[2]
        if mk in ("sh", "sz", "bj"):
            symbol, market = f"{code}.{mk.upper()}", "CN"
        elif mk == "hk":
            symbol, market = f"{code}.HK", "HK"
        elif mk == "us":
            ticker, _, exch = code.partition(".")
            if exch not in ("oq", "n", "am", ""):          # .ps 等为场外交易
                continue
            symbol, market = f"{ticker.upper()}.US", "US"
        else:
            continue
        if kind == "ZS":
            if symbol not in INDICES:
                continue
            t = "index"
        elif "ETF" in kind or "LOF" in kind or kind.startswith("JJ"):
            t = "etf"
        elif kind.startswith("GP"):
            t = "stock"
        else:
            continue
        out.append({"symbol": symbol, "name": name, "market": market, "type": t})
    return out


def smartbox(q: str) -> list[dict]:
    import requests

    try:
        resp = requests.get(SMARTBOX_URL, params={"v": 2, "t": "all", "c": 1, "q": q}, timeout=5,
                            headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        return parse_smartbox(resp.text)
    except Exception as e:  # noqa: BLE001 - 搜索联想只是补充
        logger.info("搜索联想失败：%s", e)
        return []


# ---------------------------------------------------------------- 实时行情
# 腾讯行情（qt.gtimg.cn）：A股 / 港股 / 美股与指数一次请求，带中文名称与行情时间。
# 2026-10-08 实测服务器访问新浪实时行情（hq.sinajs.cn）建立连接后无响应，改用腾讯。

QT_URL = "https://qt.gtimg.cn/q="
_QT_LINE = re.compile(r'v_([A-Za-z0-9_.$]+)="([^"]*)"')


def quote_code(symbol: str) -> str:
    code, market = symbol.rsplit(".", 1)
    if market in ("SH", "SZ", "BJ"):
        return f"{market.lower()}{code}"
    if market == "HK":
        return f"hk{code}"
    if market == "US":
        return f"us{code}"
    raise ValueError(f"不支持的代码：{symbol}")


def _f(x: str) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return math.nan


def parse_qt(text: str, codes: dict[str, str]) -> dict[str, dict]:
    """解析腾讯行情：字段以 ~ 分隔，1 名称、3 现价、4 昨收、5 今开、6 成交量、30 时间、33 最高、34 最低。"""
    out: dict[str, dict] = {}
    for m in _QT_LINE.finditer(text):
        sym = codes.get(m.group(1))
        f = m.group(2).split("~")
        if sym is None or len(f) < 35:
            continue
        price, prev = _f(f[3]), _f(f[4])
        if not price > 0:
            continue
        out[sym] = {"name": f[1], "price": price, "prev_close": prev,
                    "change_pct": price / prev - 1 if prev > 0 else None,
                    "open": _clean(_f(f[5])), "high": _clean(_f(f[33])), "low": _clean(_f(f[34])),
                    "volume": _clean(_f(f[6]), 0), "time": f[30],
                    # A股第 35 个字段为「现价/成交量/成交额（元）」
                    "amount": _clean(_f(f[35].split("/")[2]), 0) if sym.endswith((".SH", ".SZ", ".BJ"))
                    and f[35].count("/") == 2 else None}
    return out


def quotes(symbols: list[str]) -> dict[str, dict]:
    """实时快照：{代码: {name, price, prev_close, change_pct, open, high, low, volume, time}}；获取失败的不返回。"""
    import requests

    codes: dict[str, str] = {}
    for sym in symbols:
        try:
            codes[quote_code(sym)] = sym
        except ValueError:
            continue
    out: dict[str, dict] = {}
    items = list(codes.items())
    for k in range(0, len(items), 60):
        batch = dict(items[k:k + 60])
        try:
            resp = requests.get(QT_URL + ",".join(batch), timeout=8, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            out.update(parse_qt(resp.content.decode("gbk", errors="replace"), batch))
        except Exception as e:  # noqa: BLE001 - 实时行情只用于展示
            logger.info("实时行情获取失败：%s", e)
    return out


# ---------------------------------------------------------------- 详情


def _num(v) -> float:
    try:
        x = float(v)
        return x if math.isfinite(x) else math.nan
    except (TypeError, ValueError):
        return math.nan


def _clean(v: float, nd: int = 4) -> float | None:
    return None if v is None or not math.isfinite(v) else round(v, nd)


def fundamentals(s: Session, inst: Instrument, as_of: date, n: int = 8) -> list[dict]:
    """最近 n 期财报（已到法定披露截止日 / 公告日的）；库里没有时按需获取。"""
    from app.arena.deps import fundamentals_provider
    from app.market.fundamentals import FundamentalsService

    if inst.type != "stock":
        return []
    svc = FundamentalsService(s, fundamentals_provider())
    rows = svc.latest(inst.symbol, as_of, n)
    if not rows or (as_of - rows[0].available_from).days > 120:
        try:
            svc.refresh([inst.symbol], inst.market)
            s.commit()
            rows = svc.latest(inst.symbol, as_of, n)
        except Exception as e:  # noqa: BLE001
            s.rollback()
            logger.info("财报获取失败 %s：%s", inst.symbol, e)
    return [{"period": r.period.isoformat(), "available_from": r.available_from.isoformat(),
             **{k: _clean(_num(v), 3) for k, v in (r.data or {}).items()}} for r in rows]


def valuation(s: Session, inst: Instrument, close: float, as_of: date) -> dict:
    """市盈率（TTM）、市净率、股息率（近 12 个月）、总市值（亿，按净利润 / 每股收益估算股本）。"""
    from app.quant.panel import _ttm

    out: dict = {}
    if inst.type != "stock" or not close:
        return out
    recs = list(s.scalars(select(FundamentalRow).where(FundamentalRow.symbol == inst.symbol,
                                                       FundamentalRow.available_from <= as_of)
                          .order_by(FundamentalRow.period)))
    if recs:
        pairs = [(r.period, r.data or {}) for r in recs]
        eps_ttm = _ttm(pairs, "eps", len(pairs) - 1)
        last = recs[-1].data or {}
        bps = _num(last.get("bps"))
        out["pe_ttm"] = _clean(close / eps_ttm, 2) if eps_ttm and eps_ttm > 0 else None
        out["pb"] = _clean(close / bps, 2) if bps and bps > 0 else None
        shares = math.nan
        for r in reversed(recs):
            np_, eps = _num((r.data or {}).get("net_profit")), _num((r.data or {}).get("eps"))
            if abs(eps) >= 0.05 and np_ * eps > 0:
                shares = np_ / eps
                break
        out["market_cap"] = _clean(shares * close, 1)
    cash = s.scalars(select(CorporateActionRow.cash_per_share).where(
        CorporateActionRow.symbol == inst.symbol, CorporateActionRow.ex_date > as_of - timedelta(days=365),
        CorporateActionRow.ex_date <= as_of)).all()
    total = sum(_num(c) for c in cash if _num(c) > 0)
    out["dividend_yield"] = _clean(total / close * 100, 2) if close else None
    return out


def relations(s: Session, symbol: str, uid: int = DEFAULT_USER_ID) -> dict:
    """与平台的关联（只看该用户自己的）：跟踪账户的持仓、组合成员策略最近一次的选股、AI 复核记录。"""
    from app.quant import review

    portfolios = list(s.scalars(select(QuantPortfolioRow).where(QuantPortfolioRow.user_id == uid)))
    acc_label: dict[int, str] = {}
    for pf in portfolios:
        if pf.follow_account_id:
            acc_label[pf.follow_account_id] = f"{pf.name}（规则）"
        ai_id = review.settings(pf).get("account_id")
        if ai_id:
            acc_label[ai_id] = f"{pf.name}（AI 复核）"
    holdings = []
    q = (select(PositionRow, AccountRow, TrackRow, AgentRow)
         .join(AccountRow, PositionRow.account_id == AccountRow.id)
         .join(TrackRow, AccountRow.track_id == TrackRow.id).join(AgentRow, AccountRow.agent_id == AgentRow.id)
         .where(PositionRow.symbol == symbol, PositionRow.qty > 0, TrackRow.status == "running",
                TrackRow.user_id == uid))
    for pos, acc, track, agent in s.execute(q):
        holdings.append({"account_id": acc.id, "label": acc_label.get(acc.id, f"{track.name} · {agent.name}"),
                         "qty": pos.qty, "avg_cost": float(pos.avg_cost),
                         "opened": pos.opened_date.isoformat() if pos.opened_date else None})
    picks, reviews = [], []
    for pf in portfolios:
        if not pf.follow_account_id:
            continue
        row = s.execute(select(DecisionRow, RoundRow).join(RoundRow, DecisionRow.round_id == RoundRow.id)
                        .where(DecisionRow.account_id == pf.follow_account_id)
                        .order_by(RoundRow.data_date.desc()).limit(1)).first()
        if row:
            p = row[0].parsed or {}
            members = review._member_picks(p.get("members") or {}, pf.id).get(symbol, [])
            target = _num((p.get("targets") or {}).get(symbol))
            if members or target > 0:
                picks.append({"portfolio": pf.name, "date": row[1].data_date.isoformat(), "members": members,
                              "target": _clean(target)})
        ai_id = review.settings(pf).get("account_id")
        if ai_id:
            for dec, rnd in s.execute(select(DecisionRow, RoundRow).join(RoundRow, DecisionRow.round_id == RoundRow.id)
                                      .where(DecisionRow.account_id == ai_id).order_by(RoundRow.data_date.desc())):
                for it in ((dec.parsed or {}).get("review") or {}).get("items", []):
                    if it.get("symbol") == symbol:
                        reviews.append({"portfolio": pf.name, "date": rnd.data_date.isoformat(),
                                        "decision": it.get("decision"), "risk": it.get("risk"),
                                        "reason": it.get("reason")})
    return {"holdings": holdings, "picks": picks, "reviews": reviews[:50]}


def factor_ranks(s: Session, symbol: str) -> dict | None:
    """在所属股票池（沪深300 / 中证800 半时点）中的因子分位：0～100，越大表示因子值越高（不代表好坏）。"""
    from app.quant import factors
    from app.quant.data import universe_members
    from app.quant.live import live_panel

    for uni in RANK_UNIVERSES:
        try:
            insts, _bench, _ = universe_members(uni)
        except Exception:  # noqa: BLE001
            continue
        if symbol not in {i.symbol for i in insts}:
            continue
        panel, i = live_panel(s, uni, date.today())
        j = panel.col.get(symbol)
        if j is None:
            continue
        key = (uni, panel.dates[i], symbol)
        if key in _rank_cache:
            return _rank_cache[key]
        ok = (panel.types == "stock") & np.isfinite(panel.close_ff[i])
        if panel.member is not None:
            ok &= panel.member[i]
        if not ok[j]:
            return None
        groups = []
        for title, names in RANK_GROUPS:
            items = []
            for name in names:
                fd = factors.FACTORS.get(name)
                if fd is None:
                    continue
                v = factors.compute(panel, name)[i]
                x = v[j]
                peers = v[ok & np.isfinite(v)]
                if not np.isfinite(x) or len(peers) < 20:
                    continue
                pct = float((peers < x).mean() * 100 + (peers == x).mean() * 50)
                items.append({"name": name, "title": fd.title, "description": fd.description,
                              "value": _clean(float(x), 4), "pct": round(pct, 1)})
            if items:
                groups.append({"title": title, "items": items})
        out = {"universe": uni, "date": panel.dates[i].isoformat(), "peers": int(ok.sum()), "groups": groups}
        _rank_cache.clear() if len(_rank_cache) > 500 else None
        _rank_cache[key] = out
        return out
    return None


def detail(s: Session, symbol: str, uid: int = DEFAULT_USER_ID) -> dict:
    inst = resolve(s, symbol)
    status = ensure_history(s, inst)
    last = s.execute(select(DailyBarRow).where(DailyBarRow.symbol == inst.symbol)
                     .order_by(DailyBarRow.date.desc()).limit(2)).scalars().all()
    industry = s.scalar(select(IndustryRow.level1).where(IndustryRow.symbol == inst.symbol)
                        .order_by(IndustryRow.start_date.desc()).limit(1))
    as_of = last[0].date if last else date.today()
    close = float(last[0].close) if last else 0.0
    prev = float(last[1].close) if len(last) > 1 else None
    quote = quotes([inst.symbol]).get(inst.symbol)
    name = inst.name
    if quote and quote.get("name") and name.isascii() and not quote["name"].isascii():   # 港股 / 美股用中文名
        name = quote["name"]
    out = {
        "symbol": inst.symbol, "name": name, "market": inst.market, "quote": quote, "type": inst.type,
        "board": inst.board,
        "currency": get_market(inst.market).currency, "industry": industry, "is_st": inst.is_st,
        "lot_size": inst.lot_size, "update": status,
        "last": {"date": as_of.isoformat(), "close": close,
                 "change_pct": _clean(close / prev - 1) if prev else None} if last else None,
        "valuation": valuation(s, inst, close, as_of),
        "fundamentals": fundamentals(s, inst, as_of),
        "relations": relations(s, inst.symbol, uid),
    }
    return out


def news(symbol: str, days: int = 180) -> dict:
    """最近的公告（巨潮，A股）与新闻（东方财富）。"""
    from app.market.news import EastmoneyNewsProvider
    from app.textsignal.announcements import fetch as fetch_ann

    symbol = normalize_symbol(symbol)
    anns, items, errors = [], [], []
    if symbol not in INDICES and market_of(symbol) == "CN":
        try:
            for a in sorted(fetch_ann(symbol, date.today() - timedelta(days=days), date.today()),
                            key=lambda a: a.date, reverse=True)[:60]:
                anns.append({"date": a.date, "title": a.title, "url": a.url})
        except Exception as e:  # noqa: BLE001
            errors.append(f"公告：{e}")
    if symbol not in INDICES:
        try:
            for n in sorted(EastmoneyNewsProvider().fetch(symbol), key=lambda n: n.published_at, reverse=True)[:40]:
                items.append({"time": n.published_at.isoformat(), "title": n.title, "source": n.source,
                              "url": n.url})
        except Exception as e:  # noqa: BLE001
            errors.append(f"新闻：{e}")
    return {"announcements": anns, "news": items, "errors": errors}
