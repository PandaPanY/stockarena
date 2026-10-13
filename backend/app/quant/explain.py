"""选股解释：某个调仓日为什么选中这些标的、为什么卖出原来的持仓。只用于展示，不影响回测结果。"""

from __future__ import annotations

import numpy as np

from app.quant.factors import FACTORS, compute, compute_bench
from app.quant.panel import Panel
from app.quant.signal import OPS, eligible, factor_pcts, industry_of, scores, timing_on
from app.quant.spec import StrategySpec

_OP_TEXT = {">": "大于", ">=": "不低于", "<": "小于", "<=": "不高于"}


def _num(v: float) -> float | None:
    return None if v is None or not np.isfinite(v) else round(float(v), 4)


def _ftitle(name: str, params: dict) -> str:
    fd = FACTORS[name]
    if not params:
        return fd.title
    return f"{fd.title}({','.join(f'{v:g}' for v in params.values())})"


def picks(spec: StrategySpec, panel: Panel, i: int, chosen: list[int], weights: np.ndarray,
          blocked: np.ndarray | None = None) -> list[dict]:
    """入选标的的排名、综合得分与各因子的数值和百分位（越接近 100% 越符合该因子的方向）。"""
    mask = eligible(spec, panel, i)
    if blocked is not None:
        mask &= ~blocked
    sc = scores(spec, panel, i, mask)
    valid = np.where(np.isfinite(sc))[0]
    order = valid[np.argsort(-sc[valid], kind="stable")]
    rank = {int(j): k + 1 for k, j in enumerate(order)}
    ok = np.isfinite(sc)
    pct = factor_pcts(spec, panel, i, ok)
    raw = [compute(panel, f.name, f.params)[i] for f in spec.factors]
    ind = industry_of(panel, i)
    out = []
    for j in chosen:
        out.append({
            "symbol": panel.symbols[j], "name": panel.names[j], "rank": rank.get(j), "of": len(order),
            "industry": ind[j] or None,
            "score": _num(sc[j]), "weight": _num(weights[j]),
            "factors": [{"name": _ftitle(f.name, f.params), "direction": f.direction, "value": _num(raw[k][j]),
                         "pct": _num(pct[k][j])} for k, f in enumerate(spec.factors)],
        })
    return out


def drop_reason(spec: StrategySpec, panel: Panel, i: int, j: int, blocked: bool = False,
                sc: np.ndarray | None = None) -> str:
    """原来持有、本次目标权重为 0 的原因。"""
    if blocked:
        return "触发止损，到下次调仓前不再买回"
    if not panel.has_bar[i, j]:
        return "当日停牌"
    if panel.member is not None and not panel.member[i, j]:
        return "当天不是指数成分（时点股票池）"
    u = spec.universe
    types = set(u.types) | ({"lof"} if "etf" in u.types else set())
    if panel.types[j] not in types or (u.symbols and panel.symbols[j] not in u.symbols) \
            or panel.symbols[j] in u.exclude:
        return "不在选股范围内"
    if u.exclude_st and panel.is_st[j]:
        return "ST 股票"
    if u.min_listed_days and panel.bars_count[i, j] < u.min_listed_days:
        return f"上市不足 {u.min_listed_days} 个交易日"
    if u.min_amount:
        amt = compute(panel, "amount_avg", {"window": 20})[i, j]
        if not np.isfinite(amt) or amt < u.min_amount:
            return f"20 日平均成交额 {amt:.2f} 亿元，低于 {u.min_amount:g} 亿元"
    for f in spec.filters:
        v = compute(panel, f.name, f.params)[i, j]
        if not np.isfinite(v):
            return f"{_ftitle(f.name, f.params)} 数据缺失"
        if not OPS[f.op](v, f.value):
            return f"不满足过滤条件：{_ftitle(f.name, f.params)} {_OP_TEXT[f.op]} {f.value:g}（当前 {v:.3g}）"
    if panel.types[j] == "stock" and (u.industries or u.exclude_industries):
        ind = industry_of(panel, i)[j]
        if (u.industries and ind not in u.industries) or ind in u.exclude_industries:
            return f"所属行业（{ind or '未知'}）不在选股范围内"
    for f in spec.factors:
        if not np.isfinite(compute(panel, f.name, f.params)[i, j]):
            return f"{_ftitle(f.name, f.params)} 数据缺失"
    if not timing_on(spec, panel, i) and spec.timing.exposure_off == 0:
        return "大盘择时条件不满足，清仓股票"
    if sc is None:
        sc = scores(spec, panel, i, eligible(spec, panel, i))
    if np.isfinite(sc[j]):
        r = int((sc > sc[j]).sum()) + 1
        need = spec.selection.top_n + spec.selection.buffer
        if r <= need and spec.selection.max_per_industry:
            return f"同行业（{industry_of(panel, i)[j] or '未知'}）已选满 {spec.selection.max_per_industry} 只"
        return f"综合排名降到第 {r} 名（继续持有需前 {need} 名）"
    return "不再入选"


def timing_text(spec: StrategySpec, panel: Panel, i: int) -> str | None:
    t = spec.timing
    if not t.enabled:
        return None
    v = compute_bench(panel, t.name, t.params)[i]
    on = timing_on(spec, panel, i)
    state = "满足，正常仓位" if on else f"不满足，股票仓位降到 {t.exposure_off:.0%}"
    return f"基准{_ftitle(t.name, t.params)} = {v:.3g}（条件 {_OP_TEXT[t.op]} {t.value:g}）：{state}"


def pick_text(p: dict) -> str:
    fs = "，".join(f"{f['name']} {f['value']:.3g}（前 {max(1, round((1 - (f['pct'] or 0)) * 100))}%）"
                  for f in p["factors"] if f["value"] is not None)
    ind = f"［{p['industry']}］" if p.get("industry") else ""
    return f"{p['name']}{ind}：第 {p['rank']}/{p['of']} 名，{fs}"
