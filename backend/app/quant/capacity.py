"""资金容量分析（PRD 3.9 G4 / G6）：同一策略在不同资金规模下回测，看收益随资金增加衰减多少。

资金太小：股价高的股票买不起一手，实际持仓偏离目标；资金太大：冲击成本上升、单笔成交受当日成交额 10% 限制。
衡量「有没有按目标持仓」用调仓后的持仓偏离度（½Σ|实际权重−目标权重|）。即使资金充足也有少量偏离
（预留现金、涨跌停、停牌），所以以各档中最小的偏离为基准，看资金规模带来的「额外偏离」：
- 最低资金：额外偏离 ≤ 3 个百分点的最小资金规模；
- 可承载资金：开发期年化收益比最低资金（不低于 100 万）时下降不超过 1 个百分点、且额外偏离 ≤ 5 个百分点的最大资金规模。

（早期版本用「实际买入额 / 计划买入额」判断，但组合会反复尝试几百元的补仓，
买不够一手也计入，严重夸大了资金不足的影响。）
"""

from __future__ import annotations

from collections import OrderedDict

from app.quant.backtest import run_backtest
from app.quant.evaluate import window_report
from app.quant.panel import Panel
from app.quant.service import LabSettings
from app.quant.spec import StrategySpec

LEVELS = (2e5, 5e5, 1e6, 2e6, 3e6, 1e7, 3e7, 1e8, 3e8, 1e9)
BASE = 1e6
MIN_EXTRA, CAP_EXTRA, CAP_DROP = 0.03, 0.05, 0.01

_cache: OrderedDict[tuple, dict] = OrderedDict()


def capacity(members: list[tuple[StrategySpec, float]], panel: Panel, cfg: LabSettings, key=None,
             alloc=None) -> dict:
    """members：[(规格, 资金权重)]；单个策略传一个元素、权重 1。alloc：组合的资金配置（默认固定权重）。"""
    from app.quant.costs import current as current_costs
    from app.quant.portfolio import run_portfolio

    ck = (key, panel.dates[-1], len(panel.symbols), current_costs().signature())   # 不同用户的交易成本设置不同
    if key is not None and ck in _cache:
        return _cache[ck]
    w = cfg.windows(panel.dates[-1])
    rows = []
    for level in LEVELS:
        if len(members) > 1:
            res = run_portfolio(members, panel, cfg.train_start, w.end, level, alloc=alloc)
        else:
            res = run_backtest(members[0][0], panel, cfg.train_start, w.end, level)
        dev = window_report(res, *w.dev)
        ho = window_report(res, *w.holdout) if w.holdout else {}
        days = max(dev.get("days") or 1, 1)
        avg_nav = float(res.nav.mean())
        rows.append({
            "capital": level,
            "dev_annual": dev.get("annualized_return"), "dev_sharpe": dev.get("sharpe"),
            "holdout_return": ho.get("cumulative_return"),
            "cost_ratio": dev.get("cost_ratio"),
            "impact_ratio": round(res.impact_cost / avg_nav * 252 / len(res.nav), 5) if avg_nav else None,
            "fill": round(res.buy_filled / res.buy_want, 4) if res.buy_want else None,
            "deviation": round(res.deviation, 4), "exposure": round(float(res.exposure.mean()), 4),
            "lot_blocked": res.lot_blocked, "capped": sum(res.blocked.values()), "days": days,
        })
    floor = min(r["deviation"] for r in rows)
    for r in rows:
        r["extra_deviation"] = round(r["deviation"] - floor, 4)
    min_capital = next((r["capital"] for r in rows if r["extra_deviation"] <= MIN_EXTRA), None)
    # 以最低资金时的表现为基准，看资金继续增加后收益下降多少；最低资金不足 100 万时以 100 万为基准
    start = max(min_capital or BASE, BASE)
    base = next((r for r in rows if r["capital"] == start), None)
    cap = None
    for r in rows:
        if base is None or r["capital"] < start:
            continue
        ok = r["dev_annual"] is not None and base["dev_annual"] is not None
        if ok and r["extra_deviation"] <= CAP_EXTRA and r["dev_annual"] >= base["dev_annual"] - CAP_DROP:
            cap = r["capital"]
        else:
            break
    out = {"levels": rows, "min_capital": min_capital, "capacity": cap,
           "rule": f"持仓偏离 = 调仓后实际持仓与目标持仓的差异（没按目标配置的资金比例），"
                   f"额外偏离 = 比各档最小偏离多出的部分。"
                   f"最低资金：额外偏离 ≤ {MIN_EXTRA:.0%}；可承载资金：开发期年化比最低资金（不低于 100 万）时"
                   f"下降不超过 {CAP_DROP:.0%}，且额外偏离 ≤ {CAP_EXTRA:.0%}"}
    if key is not None:
        _cache[ck] = out
        while len(_cache) > 16:
            _cache.popitem(last=False)
    return out
