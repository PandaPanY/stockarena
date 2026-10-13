"""由策略规格计算某个交易日收盘后的目标权重。只使用该日（含）之前的数据。"""

from __future__ import annotations

import operator

import numpy as np
import pandas as pd

from app.quant.factors import compute, compute_bench
from app.quant.panel import Panel
from app.quant.spec import StrategySpec

OPS = {">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le}


def eligible(spec: StrategySpec, panel: Panel, i: int) -> np.ndarray:
    u = spec.universe
    types = set(u.types) | ({"lof"} if "etf" in u.types else set())
    mask = np.isin(panel.types, list(types)) & panel.has_bar[i]
    if panel.member is not None:
        mask &= panel.member[i]                      # 时点股票池：当天不是指数成分的不可选
    if u.symbols:
        mask &= np.isin(np.array(panel.symbols), u.symbols)
    if u.exclude:
        mask &= ~np.isin(np.array(panel.symbols), u.exclude)
    if u.exclude_st:
        mask &= ~panel.is_st
    if u.min_listed_days:
        mask &= panel.bars_count[i] >= u.min_listed_days
    if u.min_amount:
        amt = compute(panel, "amount_avg", {"window": 20})[i]
        mask &= np.nan_to_num(amt, nan=-1.0) >= u.min_amount
    for f in spec.filters:
        v = compute(panel, f.name, f.params)[i]
        with np.errstate(invalid="ignore"):
            mask &= np.isfinite(v) & OPS[f.op](v, f.value)
    if u.industries or u.exclude_industries:
        names = industry_of(panel, i)
        is_stock = panel.types == "stock"
        if u.industries:
            mask &= ~is_stock | np.isin(names, u.industries)
        if u.exclude_industries:
            mask &= ~(is_stock & np.isin(names, u.exclude_industries))
    return mask


def industry_of(panel: Panel, i: int) -> np.ndarray:
    """第 i 日各标的的申万一级行业名称（未知为空字符串）。"""
    n = panel.shape[1]
    if panel.industry is None or not panel.industry_names:
        return np.full(n, "", dtype=object)
    names = np.array([*panel.industry_names, ""], dtype=object)
    return names[panel.industry[i]]                  # -1 取到最后一个（空字符串）


def factor_pcts(spec: StrategySpec, panel: Panel, i: int, ok: np.ndarray) -> list[np.ndarray]:
    """各因子在可选标的中的百分位排名（按方向，越接近 1 越好）；neutralize=industry 时在同行业内排名。"""
    out = []
    groups = panel.industry[i] if panel.industry is not None else None
    for f in spec.factors:
        v = compute(panel, f.name, f.params)[i]
        s = pd.Series(np.where(ok, v if f.direction == "high" else -v, np.nan))
        if f.neutralize == "industry" and groups is not None:
            out.append(s.groupby(pd.Series(groups)).rank(pct=True).to_numpy())
        else:
            out.append(s.rank(pct=True).to_numpy())
    return out


def scores(spec: StrategySpec, panel: Panel, i: int, mask: np.ndarray) -> np.ndarray:
    """综合得分 ∈ (0, 1]：各因子在可选标的中的百分位排名按权重平均；任一因子缺失的标的不参与。"""
    ok = mask.copy()
    for f in spec.factors:
        ok &= np.isfinite(compute(panel, f.name, f.params)[i])
    if not ok.any():
        return np.full(panel.shape[1], np.nan)
    total = np.zeros(panel.shape[1])
    wsum = 0.0
    for f, pct in zip(spec.factors, factor_pcts(spec, panel, i, ok), strict=True):
        total += np.nan_to_num(pct) * f.weight
        wsum += f.weight
    out = total / wsum
    out[~ok] = np.nan
    return out


def timing_on(spec: StrategySpec, panel: Panel, i: int) -> bool:
    t = spec.timing
    if not t.enabled:
        return True
    v = compute_bench(panel, t.name, t.params)[i]
    return bool(np.isfinite(v) and OPS[t.op](v, t.value))


def target_weights(spec: StrategySpec, panel: Panel, i: int, held: np.ndarray | None = None,
                   blocked: np.ndarray | None = None) -> tuple[np.ndarray, dict]:
    """返回 (权重数组, 说明)。held：当前持有的标的（用于 buffer）；blocked：止损后暂不买回的标的。"""
    N = panel.shape[1]
    held = held if held is not None else np.zeros(N, bool)
    mask = eligible(spec, panel, i)
    if blocked is not None:
        mask &= ~blocked
    sc = scores(spec, panel, i, mask)
    valid = np.where(np.isfinite(sc))[0]
    order = valid[np.argsort(-sc[valid], kind="stable")]
    n = spec.selection.top_n
    keep_rank = n + spec.selection.buffer
    cap = spec.selection.max_per_industry
    groups = panel.industry[i] if cap and panel.industry is not None else None
    count: dict[int, int] = {}

    def room(j) -> bool:
        """同行业还能再选（行业未知的股票与 ETF 不受限制）。"""
        return groups is None or groups[j] < 0 or count.get(int(groups[j]), 0) < cap

    def take(j) -> None:
        chosen.append(int(j))
        if groups is not None and groups[j] >= 0:
            count[int(groups[j])] = count.get(int(groups[j]), 0) + 1

    chosen: list[int] = []
    if spec.selection.buffer:
        for j in order[:keep_rank]:
            if held[j] and len(chosen) < n and room(j):
                take(j)
    for j in order:
        if len(chosen) >= n:
            break
        if j not in chosen and room(j):
            take(j)
    w = np.zeros(N)
    on = timing_on(spec, panel, i)
    info = {"eligible": int(mask.sum()), "chosen": len(chosen), "timing_on": on}
    if not chosen:
        return w, info
    idx = np.array(chosen)
    wt = spec.weighting
    if wt.method == "score":
        raw = sc[idx]
    elif wt.method == "inverse_vol":
        vol = compute(panel, "volatility", {"window": wt.vol_window})[i][idx]
        med = np.nanmedian(vol) if np.isfinite(vol).any() else 0.2
        vol = np.where(np.isfinite(vol) & (vol > 0), vol, med)
        raw = 1 / vol
    else:
        raw = np.ones(len(idx))
    exposure = spec.risk.exposure
    stock_exposure = exposure if on else min(spec.timing.exposure_off, exposure)
    ww = raw / raw.sum() * stock_exposure
    w[idx] = np.minimum(ww, wt.max_weight)
    if not on and spec.timing.defensive:
        j = panel.col.get(spec.timing.defensive)
        if j is not None and panel.has_bar[i, j]:
            w[j] += max(exposure - w.sum(), 0.0)
    return w, info
