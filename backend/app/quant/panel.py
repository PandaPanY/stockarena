"""行情矩阵：把一段时间内全部标的的日线、复权因子和财报读成「日期 × 标的」数组，供因子计算和回测使用。

分析用途，使用 float 计算。日历以基准指数的交易日为准；某标的当天没有日线视为停牌。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd
from sqlalchemy import String, select, type_coerce
from sqlalchemy.orm import Session

from app.models.market import CorporateActionRow, DailyBarRow, FundamentalRow, IndustryRow, InstrumentRow

# 财报超过该天数没有更新视为过期（停止披露、退市等）
_FUNDAMENTAL_STALE_DAYS = 500


@dataclass
class Panel:
    dates: list[date]
    symbols: list[str]
    names: list[str]
    types: np.ndarray            # stock / etf / lof
    is_st: np.ndarray
    limit_pct: np.ndarray        # 涨跌幅限制，NaN 表示不限
    lot: np.ndarray
    tick: np.ndarray
    open: np.ndarray             # (T, N) 未复权价格，停牌为 NaN
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    prev_close: np.ndarray
    amount: np.ndarray           # 成交额（元）
    volume: np.ndarray
    factor: np.ndarray           # 后复权因子（已向前填充）
    has_bar: np.ndarray          # 当天有日线（未停牌）
    bench: np.ndarray            # (T,) 基准指数收盘
    benchmark: str = ""
    fundamentals: dict[str, np.ndarray] = field(default_factory=dict)
    cache: dict = field(default_factory=dict, repr=False)
    # 时点股票池：member[t, j] 表示第 t 日该标的是否为指数成分（非时点股票池时为 None，即全部可选）
    member: np.ndarray | None = None
    # 行业：industry[t, j] 为第 t 日所属申万一级行业在 industry_names 中的下标，-1 表示未知（含 ETF）
    industry: np.ndarray | None = None
    industry_names: list[str] = field(default_factory=list)
    # 每股派现（税前，元）：cash_div[t, j] 为第 t 日除息的现金分红，用于回测扣红利税；None 表示未加载
    cash_div: np.ndarray | None = None
    # 全时点股票池：delisted[j] 为退市后第一个交易日的下标（没有退市为 T），回测在这一天按最后收盘价清算持仓
    delisted: np.ndarray | None = None

    def __post_init__(self):
        self.index = {d: i for i, d in enumerate(self.dates)}
        self.col = {s: j for j, s in enumerate(self.symbols)}
        # 填充后的未复权收盘价（停牌按最后价格估值）与后复权收盘价（计算收益和因子）
        self.close_ff = pd.DataFrame(self.close).ffill().to_numpy()
        self.adj_close = self.close_ff * self.factor
        # 上市（有数据）以来的交易日数
        self.bars_count = np.cumsum(self.has_bar, axis=0)

    @property
    def shape(self) -> tuple[int, int]:
        return self.close.shape

    def frame(self, arr: np.ndarray) -> pd.DataFrame:
        return pd.DataFrame(arr, index=pd.Index(self.dates), columns=self.symbols)

    def locate(self, d: date, side: str = "after") -> int:
        """d 当天或之后（side=after）/ 之前（side=before）最近的交易日下标。"""
        idx = np.searchsorted(np.array(self.dates, dtype="datetime64[D]"), np.datetime64(d),
                              side="left" if side == "after" else "right")
        return int(idx) if side == "after" else int(idx) - 1


def _num(v) -> float:
    if v is None:
        return math.nan
    try:
        return float(v)
    except (TypeError, ValueError):
        return math.nan


def _floats(col) -> np.ndarray:
    try:
        return np.array([math.nan if v is None else float(v) for v in col], dtype=float)
    except (TypeError, ValueError):             # 个别无法解析的值：逐个按 NaN 处理
        return np.array([_num(v) for v in col], dtype=float)


def load_panel(s: Session, symbols: list[str], benchmark: str, start: date, end: date,
               membership: dict[str, list[tuple[date, date | None]]] | None = None) -> Panel:
    """读取 [start, end] 的日线（含 start 之前用于因子计算的部分需要调用方把 start 提前）。"""
    bench_rows = s.execute(
        select(DailyBarRow.date, DailyBarRow.close).where(
            DailyBarRow.symbol == benchmark, DailyBarRow.date >= start, DailyBarRow.date <= end)
        .order_by(DailyBarRow.date)).all()
    if not bench_rows:
        raise ValueError(f"缺少基准 {benchmark} 在 {start} ～ {end} 的行情，请先准备数据")
    dates = [r[0] for r in bench_rows]
    bench = np.array([float(r[1]) for r in bench_rows])

    insts = {r.symbol: r for r in s.scalars(select(InstrumentRow).where(InstrumentRow.symbol.in_(symbols)))}
    symbols = [x for x in symbols if x in insts]
    T, N = len(dates), len(symbols)
    di = {d: i for i, d in enumerate(dates)}
    sj = {x: j for j, x in enumerate(symbols)}
    arrays = {k: np.full((T, N), np.nan) for k in
              ("open", "high", "low", "close", "prev_close", "amount", "volume", "factor")}
    # 直接取数据库里的原始字符串、整列转换：逐行经 ORM 转成 Decimal 再转 float，
    # 全时点沪深300（648 个标的、约 130 万行）要 30 多秒。转换仍用 float()（精确舍入），结果与原来逐位相同
    keys = ("open", "high", "low", "close", "prev_close", "amount", "volume", "factor")
    cols = (DailyBarRow.open, DailyBarRow.high, DailyBarRow.low, DailyBarRow.close, DailyBarRow.prev_close,
            DailyBarRow.amount, DailyBarRow.volume, DailyBarRow.adj_factor)
    q = select(DailyBarRow.symbol, type_coerce(DailyBarRow.date, String()),
               *(type_coerce(c, String()) for c in cols)).where(
        DailyBarRow.symbol.in_(symbols), DailyBarRow.date >= start, DailyBarRow.date <= end)
    s.flush()                                   # 直接走连接不会自动 flush
    raw = s.connection().execute(q).fetchall()
    if raw:
        sym_col, date_col, *val_cols = zip(*raw, strict=True)
        dk = {**di, **{d.isoformat(): i for d, i in di.items()}}     # SQLite 返回字符串，其他数据库返回日期
        ii = np.fromiter((dk.get(d, -1) for d in date_col), dtype=np.int64, count=len(raw))
        jj = np.fromiter((sj[x] for x in sym_col), dtype=np.int64, count=len(raw))
        ok = ii >= 0
        ii, jj = ii[ok], jj[ok]
        for k, col in zip(keys, val_cols, strict=True):
            arrays[k][ii, jj] = _floats(col)[ok]
    has_bar = ~np.isnan(arrays["close"]) & (np.nan_to_num(arrays["volume"]) > 0)
    for k in ("open", "high", "low", "close", "prev_close", "amount", "volume"):
        arrays[k][~has_bar] = np.nan
    factor = pd.DataFrame(arrays.pop("factor")).ffill().bfill().fillna(1.0).to_numpy(copy=True)

    meta = [insts[x] for x in symbols]
    _fix_fund_splits(arrays["close"], factor, [m.type for m in meta],
                     [float(m.price_limit_pct) if m.price_limit_pct is not None else np.nan for m in meta])
    panel = Panel(
        dates=dates, symbols=symbols, names=[m.name for m in meta],
        types=np.array([m.type for m in meta]), is_st=np.array([bool(m.is_st) for m in meta]),
        limit_pct=np.array([float(m.price_limit_pct) if m.price_limit_pct is not None else np.nan for m in meta]),
        lot=np.array([m.lot_size or 100 for m in meta], dtype=float),
        tick=np.array([float(m.tick) for m in meta]),
        factor=factor, has_bar=has_bar, bench=bench, benchmark=benchmark, **arrays,
    )
    panel.fundamentals = load_fundamentals(s, panel)
    panel.fundamentals["div_yield"] = load_dividend_yield(s, panel)
    panel.cash_div = load_cash_dividends(s, panel)
    panel.industry, panel.industry_names = load_industries(s, panel)
    if membership is not None:
        panel.member = member_mask(panel, membership)
    return panel


def member_mask(panel: Panel, membership: dict[str, list[tuple[date, date | None]]]) -> np.ndarray:
    """按纳入 / 剔除日期生成成分掩码。不在成分记录中的股票视为非成分；基金（ETF）不受限制。"""
    T, N = panel.shape
    dates64 = np.array(panel.dates, dtype="datetime64[D]")
    mask = np.zeros((T, N), bool)
    for j, sym in enumerate(panel.symbols):
        if panel.types[j] != "stock":
            mask[:, j] = True
            continue
        for in_d, out_d in membership.get(sym, []):
            i0 = int(np.searchsorted(dates64, np.datetime64(in_d), side="left"))
            i1 = int(np.searchsorted(dates64, np.datetime64(out_d), side="left")) if out_d else T
            mask[i0:i1, j] = True
    return mask


# 基金单日价格变化超出该范围视为份额折算 / 拆分（ETF 涨跌幅限制最多 20%）
_SPLIT_UP, _SPLIT_DOWN = 1.45, 0.69
_LIMIT_TOL = 0.015      # 基金单日变动超过自身涨跌停限制（10%，创业板 / 科创板类 ETF 为 20%）+ 余量时视为份额折算


def _fix_fund_splits(close: np.ndarray, factor: np.ndarray, types: list[str],
                     limits: list[float] | None = None) -> None:
    """兜底：数据源已在复权因子中计入 ETF 份额折算（app/market/providers/akshare_cn.py fund_actions），
    这里只防止旧缓存或新数据源漏掉折算时出现单日 -80% / +176% 的假涨跌。

    检测到跳变时把比例计入复权因子（假设折算当日真实涨跌为 0），使复权价连续。原地修改 factor。
    """
    for j, t in enumerate(types):
        if t not in ("etf", "lof"):
            continue
        col = close[:, j]
        up, down = _SPLIT_UP, _SPLIT_DOWN
        if limits is not None and np.isfinite(limits[j]):
            # 已知涨跌停限制时收紧：如 510500 在 2022-08-29 单日 -12.7%（指数持平）的折算 ±45% 识别不到
            up, down = 1 + limits[j] + _LIMIT_TOL, 1 - limits[j] - _LIMIT_TOL
        valid = np.where(~np.isnan(col))[0]
        for a, b in zip(valid[:-1], valid[1:], strict=True):
            r = (col[b] * factor[b, j]) / (col[a] * factor[a, j])
            if r > up or r < down:
                factor[b:, j] /= r


# ---------------------------------------------------------------- 财报


def _ttm(records: list[tuple[date, dict]], key: str, i: int) -> float:
    """A股财报的每股收益等为年初至今累计值：TTM = 本期累计 + 上年年报 − 上年同期累计；缺数据时按月份年化。"""
    period, data = records[i]
    cur = _num(data.get(key))
    if math.isnan(cur):
        return math.nan
    if period.month == 12:
        return cur
    by_period = {(p.year, p.month): d for p, d in records}
    last_annual = by_period.get((period.year - 1, 12))
    same_last = by_period.get((period.year - 1, period.month))
    if last_annual and same_last:
        a, b = _num(last_annual.get(key)), _num(same_last.get(key))
        if not math.isnan(a) and not math.isnan(b):
            return cur + a - b
    return cur * 12 / period.month


def load_fundamentals(s: Session, panel: Panel) -> dict[str, np.ndarray]:
    """财报指标按最早可用日期对齐到交易日（向前填充）。返回 {字段: (T, N)}。"""
    stock_cols = [j for j, t in enumerate(panel.types) if t == "stock"]
    fields = ("eps_ttm", "bps", "roe", "net_profit_yoy", "revenue_yoy", "gross_margin", "debt_ratio",
              "net_margin", "growth_accel", "shares")
    T, N = panel.shape
    out = {k: np.full((T, N), np.nan) for k in fields}
    if not stock_cols:
        return out
    rows = s.execute(select(FundamentalRow.symbol, FundamentalRow.period, FundamentalRow.available_from,
                            FundamentalRow.data).where(
        FundamentalRow.symbol.in_([panel.symbols[j] for j in stock_cols])).order_by(
        FundamentalRow.symbol, FundamentalRow.period)).all()
    by_symbol: dict[str, list] = {}
    for sym, period, avail, data in rows:
        by_symbol.setdefault(sym, []).append((period, avail, data or {}))
    dates64 = np.array(panel.dates, dtype="datetime64[D]")
    for sym, recs in by_symbol.items():
        j = panel.col[sym]
        pairs = [(p, d) for p, _, d in recs]
        values = []
        shares = math.nan
        for i, (period, avail, data) in enumerate(recs):
            roe = _num(data.get("roe"))
            # 估算总股本（亿股）= 归母净利润（亿元）/ 每股收益（同一报告期的累计值）；每股收益太小时误差大，沿用上一次
            np_, eps = _num(data.get("net_profit")), _num(data.get("eps"))
            if abs(eps) >= 0.05 and np_ * eps > 0:
                shares = np_ / eps
            # 业绩加速度：本期净利润同比增速 − 上一报告期的同比增速（相隔不超过约一个季度才计算）
            accel = math.nan
            if i > 0 and (period - recs[i - 1][0]).days <= 100:
                accel = _num(data.get("net_profit_yoy")) - _num(recs[i - 1][2].get("net_profit_yoy"))
            values.append((avail, period, {
                "eps_ttm": _ttm(pairs, "eps", i),
                "bps": _num(data.get("bps")),
                "roe": roe * 12 / period.month if not math.isnan(roe) else math.nan,
                "net_profit_yoy": _num(data.get("net_profit_yoy")),
                "revenue_yoy": _num(data.get("revenue_yoy")),
                "gross_margin": _num(data.get("gross_margin")),
                "debt_ratio": _num(data.get("debt_ratio")),
                "net_margin": _num(data.get("net_margin")),
                "growth_accel": accel,
                "shares": shares,
            }))
        # 同一天可用多份（如年报与一季报都以 4/30 为截止日）时取报告期最新的
        values.sort(key=lambda v: (v[0], v[1]))
        for k, (avail, period, vals) in enumerate(values):
            i0 = int(np.searchsorted(dates64, np.datetime64(avail), side="left"))
            nxt = values[k + 1][0] if k + 1 < len(values) else None
            i1 = int(np.searchsorted(dates64, np.datetime64(nxt), side="left")) if nxt else T
            stale = int(np.searchsorted(dates64, np.datetime64(period) + _FUNDAMENTAL_STALE_DAYS, side="left"))
            i1 = min(i1, stale)
            if i0 >= i1:
                continue
            for f in fields:
                out[f][i0:i1, j] = vals[f]
    return out


def load_dividend_yield(s: Session, panel: Panel) -> np.ndarray:
    """滚动 12 个月股息率（%）：过去 365 天内每次现金分红 / 除息前一日收盘价，逐次相加。

    用「分红 / 当时股价」相加，不受之后送转股的影响；按除息日计入（实际公告更早，宁晚勿早）。
    """
    T, N = panel.shape
    out = np.zeros((T, N))
    rows = s.execute(select(CorporateActionRow.symbol, CorporateActionRow.ex_date, CorporateActionRow.cash_per_share)
                     .where(CorporateActionRow.symbol.in_(panel.symbols))).all()
    dates64 = np.array(panel.dates, dtype="datetime64[D]")
    for sym, ex, cash in rows:
        c = _num(cash)
        if not c or c <= 0:
            continue
        j = panel.col[sym]
        i0 = int(np.searchsorted(dates64, np.datetime64(ex), side="left"))
        if i0 <= 0 or i0 >= T:
            continue
        px = panel.close_ff[i0 - 1, j]
        if not np.isfinite(px) or px <= 0:
            continue
        i1 = int(np.searchsorted(dates64, np.datetime64(ex, "D") + np.timedelta64(365, "D"), side="left"))
        out[i0:i1, j] += c / px * 100
    # 还没有行情的日期没有意义；股票池里上市不足一年的股票股息率为 0（没有分红记录）也是真实情况
    out[~np.isfinite(panel.close_ff)] = np.nan
    return out


def load_cash_dividends(s: Session, panel: Panel) -> np.ndarray:
    """除息日的每股派现（税前）。除息日不是交易日时记在之后第一个交易日（复权因子在那天变化）。"""
    T, N = panel.shape
    out = np.zeros((T, N))
    rows = s.execute(select(CorporateActionRow.symbol, CorporateActionRow.ex_date, CorporateActionRow.cash_per_share)
                     .where(CorporateActionRow.symbol.in_(panel.symbols))).all()
    dates64 = np.array(panel.dates, dtype="datetime64[D]")
    for sym, ex, cash in rows:
        c = _num(cash)
        if not c or c <= 0:
            continue
        i = int(np.searchsorted(dates64, np.datetime64(ex), side="left"))
        if 0 < i < T:
            out[i, panel.col[sym]] += c
    return out


def load_industries(s: Session, panel: Panel) -> tuple[np.ndarray, list[str]]:
    """每个交易日每只股票所属的申万一级行业（按生效日期；早于最早记录的日期沿用最早一条）。"""
    T, N = panel.shape
    codes = np.full((T, N), -1, dtype=np.int16)
    rows = s.execute(select(IndustryRow.symbol, IndustryRow.start_date, IndustryRow.level1)
                     .where(IndustryRow.symbol.in_(panel.symbols)).order_by(IndustryRow.symbol,
                                                                              IndustryRow.start_date)).all()
    names: list[str] = sorted({r[2] for r in rows})
    idx = {n: k for k, n in enumerate(names)}
    dates64 = np.array(panel.dates, dtype="datetime64[D]")
    by: dict[str, list] = {}
    for sym, d, l1 in rows:
        by.setdefault(sym, []).append((d, l1))
    for sym, recs in by.items():
        j = panel.col[sym]
        for k, (d, l1) in enumerate(recs):
            i0 = 0 if k == 0 else int(np.searchsorted(dates64, np.datetime64(d), side="left"))
            i1 = int(np.searchsorted(dates64, np.datetime64(recs[k + 1][0]), side="left")) if k + 1 < len(recs) else T
            codes[i0:i1, j] = idx[l1]
    return codes, names
