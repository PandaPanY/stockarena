"""因子库：策略规格只能使用这里登记的因子。每个因子返回 (T, N) 数组，第 t 行只使用 t 日（含）之前的数据。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
import pandas as pd

from app.quant.panel import Panel

ANNUAL = 252


@dataclass(frozen=True)
class Param:
    default: float
    lo: float
    hi: float
    integer: bool = True
    title: str = ""


@dataclass(frozen=True)
class FactorDef:
    name: str
    title: str
    description: str
    params: dict[str, Param]
    fn: Callable[..., pd.DataFrame]
    fundamental: bool = False
    # 可用于大盘择时（只需要价格序列）
    timing: bool = False


def momentum(px: pd.DataFrame, window: int, skip: int = 0) -> pd.DataFrame:
    return px.shift(skip) / px.shift(window) - 1


def reversal(px: pd.DataFrame, window: int) -> pd.DataFrame:
    return -(px / px.shift(window) - 1)


def volatility(px: pd.DataFrame, window: int) -> pd.DataFrame:
    return px.pct_change(fill_method=None).rolling(window, min_periods=max(5, window * 2 // 3)).std() * np.sqrt(ANNUAL)


def downside_vol(px: pd.DataFrame, window: int) -> pd.DataFrame:
    r = px.pct_change(fill_method=None).clip(upper=0)
    return (r ** 2).rolling(window, min_periods=max(5, window * 2 // 3)).mean() ** 0.5 * np.sqrt(ANNUAL)


def ma_bias(px: pd.DataFrame, window: int) -> pd.DataFrame:
    return px / px.rolling(window).mean() - 1


def ma_trend(px: pd.DataFrame, fast: int, slow: int) -> pd.DataFrame:
    if fast >= slow:
        fast, slow = slow, fast
    return px.rolling(fast).mean() / px.rolling(slow).mean() - 1


def rsi(px: pd.DataFrame, window: int) -> pd.DataFrame:
    d = px.diff()
    up = d.clip(lower=0).ewm(alpha=1 / window, adjust=False, min_periods=window).mean()
    down = (-d.clip(upper=0)).ewm(alpha=1 / window, adjust=False, min_periods=window).mean()
    return 100 - 100 / (1 + up / down.replace(0, np.nan))


def drawdown(px: pd.DataFrame, window: int) -> pd.DataFrame:
    return px / px.rolling(window, min_periods=1).max() - 1


def price_position(px: pd.DataFrame, window: int) -> pd.DataFrame:
    lo, hi = px.rolling(window).min(), px.rolling(window).max()
    return (px - lo) / (hi - lo).replace(0, np.nan)


def sharpe_mom(px: pd.DataFrame, window: int) -> pd.DataFrame:
    r = px.pct_change(fill_method=None)
    m = r.rolling(window, min_periods=window * 2 // 3)
    return m.mean() / m.std().replace(0, np.nan) * np.sqrt(ANNUAL)


def max_ret(px: pd.DataFrame, window: int) -> pd.DataFrame:
    return px.pct_change(fill_method=None).rolling(window, min_periods=window // 2).max()


@dataclass
class FactorInputs:
    """因子计算的输入：后复权价格、成交量 / 额、基准收益与财报。"""

    adj: pd.DataFrame
    volume: pd.DataFrame
    amount: pd.DataFrame
    bench: pd.Series
    close: pd.DataFrame
    fundamentals: dict[str, pd.DataFrame]


def _volume_ratio(x: FactorInputs, short: int, long: int) -> pd.DataFrame:
    v = x.volume
    return v.rolling(short, min_periods=1).mean() / v.rolling(long, min_periods=long // 2).mean()


def _amount_avg(x: FactorInputs, window: int) -> pd.DataFrame:
    return x.amount.rolling(window, min_periods=window // 2).mean() / 1e8


def _beta(x: FactorInputs, window: int) -> pd.DataFrame:
    r = x.adj.pct_change(fill_method=None)
    b = x.bench.pct_change()
    cov = r.rolling(window, min_periods=window * 2 // 3).cov(b)
    return cov.div(b.rolling(window, min_periods=window * 2 // 3).var(), axis=0)


def _fdata(x: FactorInputs, key: str) -> pd.DataFrame:
    """财报 / 分红数据；行情矩阵里没有该字段时（如测试用的合成数据）返回全 NaN。"""
    v = x.fundamentals.get(key)
    return v if v is not None else x.close * np.nan


def _per_price(key: str):
    def fn(x: FactorInputs) -> pd.DataFrame:
        return _fdata(x, key) / x.close
    return fn


def _fund(key: str):
    def fn(x: FactorInputs) -> pd.DataFrame:
        return _fdata(x, key)
    return fn


def _size(x: FactorInputs) -> pd.DataFrame:
    """总市值（亿元）= 估算总股本 × 股价。"""
    return _fdata(x, "shares") * x.close


def _turnover(x: FactorInputs, window: int) -> pd.DataFrame:
    """日均换手率（%）= 平均成交量 / 估算总股本（用总股本，A股流通股比例不一，数值偏低但排序可比）。"""
    vol = x.volume.rolling(window, min_periods=window // 2).mean()
    return vol / (_fdata(x, "shares") * 1e8) * 100


def _idio_vol(x: FactorInputs, window: int) -> pd.DataFrame:
    """特质波动率（年化）：剔除与基准同涨同跌的部分后的残差波动，√(Var(r) − Cov(r,b)² / Var(b))。"""
    r = x.adj.pct_change(fill_method=None)
    b = x.bench.pct_change()
    mp = window * 2 // 3
    var_r = r.rolling(window, min_periods=mp).var()
    cov = r.rolling(window, min_periods=mp).cov(b)
    var_b = b.rolling(window, min_periods=mp).var()
    resid = (var_r - cov.pow(2).div(var_b, axis=0)).clip(lower=0)
    return resid ** 0.5 * np.sqrt(ANNUAL)


def _skewness(x: FactorInputs, window: int) -> pd.DataFrame:
    return x.adj.pct_change(fill_method=None).rolling(window, min_periods=window * 2 // 3).skew()


def _illiquidity(x: FactorInputs, window: int) -> pd.DataFrame:
    """Amihud 非流动性：平均每亿元成交额引起的价格变动（%），越大越难交易、冲击越大。"""
    r = x.adj.pct_change(fill_method=None).abs() * 100
    amt = x.amount.where(x.amount > 0) / 1e8
    return (r / amt).rolling(window, min_periods=window // 2).mean()


def _wrap(f):
    def fn(x: FactorInputs, **kw) -> pd.DataFrame:
        return f(x.adj, **kw)
    return fn


W = lambda d, lo, hi, t="窗口（交易日）": Param(d, lo, hi, True, t)  # noqa: E731

FACTORS: dict[str, FactorDef] = {f.name: f for f in [
    FactorDef("momentum", "动量", "过去 window 日涨幅（跳过最近 skip 日，常用于剔除短期反转）",
              {"window": W(60, 5, 250), "skip": W(0, 0, 20, "跳过最近天数")}, _wrap(momentum), timing=True),
    FactorDef("reversal", "短期反转", "过去 window 日涨幅的相反数（跌得越多值越大）",
              {"window": W(5, 2, 20)}, _wrap(reversal)),
    FactorDef("volatility", "波动率", "日收益率标准差（年化）", {"window": W(20, 5, 250)}, _wrap(volatility),
              timing=True),
    FactorDef("downside_vol", "下行波动率", "只统计下跌日的波动（年化）", {"window": W(60, 10, 250)},
              _wrap(downside_vol)),
    FactorDef("ma_bias", "均线乖离", "收盘价相对 window 日均线的偏离（>0 在均线上方）", {"window": W(20, 5, 250)},
              _wrap(ma_bias), timing=True),
    FactorDef("ma_trend", "均线趋势", "快均线相对慢均线的偏离（>0 为多头排列）",
              {"fast": W(20, 3, 120, "快线"), "slow": W(60, 10, 250, "慢线")}, _wrap(ma_trend), timing=True),
    FactorDef("rsi", "RSI", "相对强弱指标（0～100）", {"window": W(14, 5, 60)}, _wrap(rsi), timing=True),
    FactorDef("drawdown", "距高点回撤", "收盘价相对 window 日内最高价的回撤（≤0）", {"window": W(60, 10, 250)},
              _wrap(drawdown), timing=True),
    FactorDef("price_position", "价格位置", "收盘价在 window 日最高最低区间中的位置（0～1）",
              {"window": W(120, 20, 500)}, _wrap(price_position)),
    FactorDef("sharpe_mom", "夏普动量", "过去 window 日收益均值 / 波动（年化），稳健上涨得分高",
              {"window": W(120, 20, 250)}, _wrap(sharpe_mom)),
    FactorDef("max_ret", "最大单日涨幅", "过去 window 日最大单日涨幅（彩票效应）", {"window": W(20, 5, 60)},
              _wrap(max_ret)),
    FactorDef("volume_ratio", "量比", "短期平均成交量 / 长期平均成交量",
              {"short": W(5, 1, 20, "短期"), "long": W(60, 10, 250, "长期")}, _volume_ratio),
    FactorDef("amount_avg", "平均成交额", "过去 window 日平均成交额（亿元），衡量流动性 / 规模",
              {"window": W(20, 5, 120)}, _amount_avg),
    FactorDef("beta", "贝塔", "相对基准指数的贝塔系数", {"window": W(120, 40, 250)}, _beta),
    FactorDef("ep", "盈利收益率（E/P）", "滚动 12 个月每股收益 / 股价，即市盈率的倒数（越高越便宜，亏损为负）",
              {}, _per_price("eps_ttm"), fundamental=True),
    FactorDef("bp", "账面市值比（B/P）", "每股净资产 / 股价，即市净率的倒数（越高越便宜）", {},
              _per_price("bps"), fundamental=True),
    FactorDef("roe", "ROE", "净资产收益率（%，年化）", {}, _fund("roe"), fundamental=True),
    FactorDef("profit_growth", "净利润增速", "归母净利润同比增长（%）", {}, _fund("net_profit_yoy"), fundamental=True),
    FactorDef("revenue_growth", "营收增速", "营业收入同比增长（%）", {}, _fund("revenue_yoy"), fundamental=True),
    FactorDef("gross_margin", "毛利率", "销售毛利率（%）", {}, _fund("gross_margin"), fundamental=True),
    FactorDef("debt_ratio", "资产负债率", "资产负债率（%）", {}, _fund("debt_ratio"), fundamental=True),
    # ---- v1.9 新增 ----
    FactorDef("dividend_yield", "股息率", "过去 12 个月现金分红 / 股价（%），按除息日计入；没有分红为 0（ETF 也适用）",
              {}, _fund("div_yield")),
    FactorDef("net_margin", "净利率", "销售净利率（%）", {}, _fund("net_margin"), fundamental=True),
    FactorDef("growth_accel", "业绩加速度", "本期净利润同比增速 − 上一报告期的同比增速（百分点），>0 表示增长在加快",
              {}, _fund("growth_accel"), fundamental=True),
    FactorDef("size", "总市值", "估算总市值（亿元）= 归母净利润 / 每股收益 × 股价；direction=low 偏好小盘",
              {}, _size, fundamental=True),
    FactorDef("turnover", "换手率", "过去 window 日平均换手率（%，按估算总股本）", {"window": W(20, 5, 120)}, _turnover,
              fundamental=True),
    FactorDef("idio_vol", "特质波动率", "剔除大盘影响后的残差波动（年化），低特质波动通常更稳健",
              {"window": W(60, 20, 250)}, _idio_vol),
    FactorDef("skewness", "收益偏度", "过去 window 日日收益率的偏度，正偏（彩票型）个股长期收益往往偏低",
              {"window": W(60, 20, 250)}, _skewness),
    FactorDef("illiquidity", "非流动性", "Amihud 指标：平均每亿元成交引起的涨跌幅（%），越大越难交易",
              {"window": W(20, 5, 120)}, _illiquidity),
]}


def normalize_params(name: str, params: dict | None) -> dict[str, float]:
    """补全默认值并检查范围。参数不合法时抛出 ValueError（中文说明，返回给 AI 修正）。"""
    fd = FACTORS.get(name)
    if fd is None:
        raise ValueError(f"未知因子 {name}，可用：{', '.join(FACTORS)}")
    params = dict(params or {})
    unknown = set(params) - set(fd.params)
    if unknown:
        raise ValueError(f"因子 {name} 没有参数 {', '.join(sorted(unknown))}"
                         + (f"，可用参数：{', '.join(fd.params)}" if fd.params else "（该因子没有参数）"))
    out = {}
    for k, p in fd.params.items():
        v = params.get(k, p.default)
        try:
            v = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"因子 {name} 的参数 {k} 必须是数字") from None
        if not p.lo <= v <= p.hi:
            raise ValueError(f"因子 {name} 的参数 {k}={v:g} 超出范围 [{p.lo:g}, {p.hi:g}]")
        out[k] = int(round(v)) if p.integer else v
    return out


def _inputs(panel: Panel) -> FactorInputs:
    x = panel.cache.get("_inputs")
    if x is None:
        x = FactorInputs(
            adj=panel.frame(panel.adj_close), volume=panel.frame(panel.volume).fillna(0.0),
            amount=panel.frame(panel.amount).fillna(0.0), bench=pd.Series(panel.bench, index=pd.Index(panel.dates)),
            close=panel.frame(panel.close_ff),
            fundamentals={k: panel.frame(v) for k, v in panel.fundamentals.items()},
        )
        panel.cache["_inputs"] = x
    return x


def compute(panel: Panel, name: str, params: dict | None = None) -> np.ndarray:
    """(T, N) 因子值，带缓存。停牌日的值保留（使用停牌前价格），是否可交易由回测判断。"""
    p = normalize_params(name, params)
    key = (name, tuple(sorted(p.items())))
    v = panel.cache.get(key)
    if v is None:
        df = FACTORS[name].fn(_inputs(panel), **p)
        v = df.to_numpy(dtype=float, copy=True)
        v[~np.isfinite(v)] = np.nan
        panel.cache[key] = v
    return v


def compute_bench(panel: Panel, name: str, params: dict | None = None) -> np.ndarray:
    """基准指数上的因子值（大盘择时），(T,)。"""
    fd = FACTORS[name]
    if not fd.timing:
        raise ValueError(f"因子 {name} 不能用于大盘择时，可用：{', '.join(k for k, f in FACTORS.items() if f.timing)}")
    p = normalize_params(name, params)
    key = ("_bench", name, tuple(sorted(p.items())))
    v = panel.cache.get(key)
    if v is None:
        px = pd.DataFrame({"b": panel.bench}, index=pd.Index(panel.dates))
        v = fd.fn(FactorInputs(adj=px, volume=px * 0, amount=px * 0, bench=px["b"], close=px, fundamentals={}),
                  **p)["b"].to_numpy(dtype=float)
        panel.cache[key] = v
    return v


def catalog() -> list[dict]:
    """因子说明（给 AI 和界面）。"""
    return [{
        "name": f.name, "title": f.title, "description": f.description, "fundamental": f.fundamental,
        "timing": f.timing,
        "params": {k: {"default": p.default, "min": p.lo, "max": p.hi, "title": p.title, "integer": p.integer}
                   for k, p in f.params.items()},
    } for f in FACTORS.values()]
