"""因子成绩单（PRD 3.9 G2）：用整个股票池的横截面检验每个因子，而不是用一个十几只股票的组合。

做法（标准的因子研究）：每隔 step 个交易日，在可选股票中计算
- IC：因子值与之后 horizon 个交易日收益的排名相关系数；
- 五分组：按因子值把股票等分为 5 组，各组之后 horizon 日的平均收益。
汇总为平均 IC、ICIR（IC 均值 / 标准差，年化）、t 值、IC 为正的比例、逐年平均 IC、各组年化收益与多空收益。
只使用 t 日及以前的因子值，远期收益只用于评价，不进入任何信号。
"""

from __future__ import annotations

import math
from collections import OrderedDict
from datetime import date

import numpy as np
import pandas as pd

from app.quant.factors import FACTORS, compute, normalize_params
from app.quant.panel import Panel

HORIZON = 20
STEP = 20
MIN_STOCKS = 30
ANNUAL = 252

_cache: OrderedDict[tuple, list[dict]] = OrderedDict()


def _universe(panel: Panel, i: int) -> np.ndarray:
    """与默认选股范围一致：股票、未停牌、非 ST、上市满 120 个交易日、20 日平均成交额 ≥ 0.5 亿元。"""
    amt = compute(panel, "amount_avg", {"window": 20})[i]
    m = ((panel.types == "stock") & panel.has_bar[i] & ~panel.is_st & (panel.bars_count[i] >= 120)
         & (np.nan_to_num(amt, nan=-1) >= 0.5))
    return m & panel.member[i] if panel.member is not None else m


def _rank(x: np.ndarray) -> np.ndarray:
    return pd.Series(x).rank().to_numpy()


def _ann(r: float, horizon: int) -> float:
    return (1 + r) ** (ANNUAL / horizon) - 1


def factor_label(name: str, params: dict) -> str:
    fd = FACTORS[name]
    if not params:
        return fd.title
    return f"{fd.title}({','.join(f'{v:g}' for v in params.values())})"


def default_factors(extra: list[tuple[str, dict]] | None = None) -> list[tuple[str, dict]]:
    """因子库中每个因子的默认参数，加上指定的参数组合（如冠军用到的），去重。"""
    out: dict[tuple, tuple[str, dict]] = {}
    for name in FACTORS:
        if name == "amount_avg":
            continue
        p = normalize_params(name, {})
        out[(name, tuple(sorted(p.items())))] = (name, p)
    for name, params in extra or []:
        p = normalize_params(name, params)
        out[(name, tuple(sorted(p.items())))] = (name, p)
    return list(out.values())


def scorecard(panel: Panel, start: date, end: date, factors: list[tuple[str, dict]] | None = None,
              horizon: int = HORIZON, step: int = STEP, min_stocks: int = MIN_STOCKS) -> list[dict]:
    factors = [(n, normalize_params(n, p)) for n, p in (factors or default_factors())]
    key = (id(panel), panel.dates[-1], len(panel.symbols), start, end, horizon, step, min_stocks,
           tuple((n, tuple(sorted(p.items()))) for n, p in factors))
    if key in _cache:
        _cache.move_to_end(key)
        return _cache[key]
    i0, i1 = panel.locate(start, "after"), panel.locate(end, "before") - horizon
    idx = list(range(i0, i1 + 1, step))
    adj = panel.adj_close
    rows = []
    for name, params in factors:
        try:
            vals = compute(panel, name, params)
        except (KeyError, ValueError):          # 缺少数据（如没有财报）的因子跳过
            continue
        ics, years, groups = [], [], []
        for i in idx:
            fwd = adj[i + horizon] / adj[i] - 1
            v = vals[i]
            m = _universe(panel, i) & np.isfinite(v) & np.isfinite(fwd)
            if m.sum() < min_stocks:
                continue
            fv, fr = v[m], fwd[m]
            ic = np.corrcoef(_rank(fv), _rank(fr))[0, 1]
            if not np.isfinite(ic):
                continue
            ics.append(ic)
            years.append(panel.dates[i].year)
            q = pd.qcut(_rank(fv), 5, labels=False, duplicates="drop")
            if len(set(q)) == 5:
                groups.append([float(fr[q == g].mean()) for g in range(5)])
        if len(ics) < 6:
            continue
        ic_arr = np.array(ics)
        mean, std = float(ic_arr.mean()), float(ic_arr.std(ddof=1))
        per_year = pd.Series(ics).groupby(years).mean()
        g = np.array(groups) if groups else None
        quint = [round(float(_ann(x, horizon)), 4) for x in g.mean(axis=0)] if g is not None else []
        spread = g[:, 4] - g[:, 0] if g is not None else None
        rows.append({
            "name": name, "params": params, "label": factor_label(name, params), "title": FACTORS[name].title,
            "fundamental": FACTORS[name].fundamental,
            "periods": len(ics), "ic": round(mean, 4),
            "icir": round(mean / std * math.sqrt(ANNUAL / step), 2) if std > 0 else None,
            "t": round(mean / std * math.sqrt(len(ics)), 2) if std > 0 else None,
            "positive": round(float((ic_arr * np.sign(mean or 1) > 0).mean()), 3),
            "direction": "high" if mean >= 0 else "low",
            "years": {int(y): round(float(x), 4) for y, x in per_year.items()},
            "quintiles": quint,
            # 多空：按建议方向，最好一组减最差一组（年化）
            "long_short": (round(float(_ann(spread.mean() * (1 if mean >= 0 else -1), horizon)), 4)
                           if spread is not None else None),
        })
    rows.sort(key=lambda r: -abs(r["icir"] or 0))
    _cache[key] = rows
    while len(_cache) > 8:
        _cache.popitem(last=False)
    return rows


def scorecard_text(rows: list[dict], limit: int = 24) -> str:
    """给 AI 的因子成绩单（只含开发期）。"""
    lines = ["因子（参数）｜平均 IC｜ICIR｜t｜IC 同向比例｜建议方向｜逐年平均 IC｜五分组年化收益（低→高）｜多空年化"]
    for r in rows[:limit]:
        yrs = " ".join(f"{y}:{v:+.3f}" for y, v in r["years"].items())
        q = "/".join(f"{x:+.0%}" for x in r["quintiles"]) if r["quintiles"] else "-"
        ls = f"{r['long_short']:+.1%}" if r["long_short"] is not None else "-"
        lines.append(f"{r['label']}｜{r['ic']:+.3f}｜{r['icir']}｜{r['t']}｜{r['positive']:.0%}｜"
                     f"{'越高越好' if r['direction'] == 'high' else '越低越好'}｜{yrs}｜{q}｜{ls}")
    return "\n".join(lines)


def scorecard_for_prompt(panel: Panel, start: date, end: date, extra: list[tuple[str, dict]]) -> str:
    try:
        return scorecard_text(scorecard(panel, start, end, default_factors(extra)))
    except Exception as e:  # 成绩单只是辅助信息，失败不影响研究
        return f"（因子成绩单暂不可用：{e}）"
