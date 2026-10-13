"""程序调参（PRD 3.8）：AI 给出策略结构后，只在训练区间对数值参数做小范围坐标搜索。

- 只用训练区间，验证 / 留出区间的信息不进入调参；
- 窗口类参数只取常用值，避免「57 日均线」这类精确拟合；
- 目标为训练夏普，回撤超过上限、换手超过上限时扣分；每次改进需超过最小幅度，并有回测次数上限。
调参后的规格仍要经过验证区间、分段一致性与稳健性检验才能升级。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field

from app.quant.backtest import run_backtest
from app.quant.evaluate import Gates, Windows, window_report
from app.quant.factors import FACTORS
from app.quant.panel import Panel
from app.quant.spec import StrategySpec, numeric_knobs, parse_spec

WINDOWS = (5, 10, 20, 40, 60, 90, 120, 180, 250)
REBALANCE = (1, 3, 5, 10, 15, 20, 30, 40, 60)
MIN_GAIN = 0.02


@dataclass
class TuneResult:
    spec: StrategySpec
    changes: list[str] = field(default_factory=list)       # 「动量·窗口 60 → 90」
    evals: int = 0
    base_score: float | None = None
    score: float | None = None


def _neighbors(seq: tuple[int, ...], v: float) -> list[float]:
    """常用值序列中与 v 相邻的值（v 不在序列中时取最近的上下两个）。"""
    lower = [x for x in seq if x < v]
    upper = [x for x in seq if x > v]
    return [*lower[-1:], *upper[:1]]


def _nice(x: float) -> float:
    """阈值取整到两位有效数字左右，避免 -0.2344 这类精确值。"""
    a = abs(x)
    return round(x) if a >= 10 else round(x, 1) if a >= 1 else round(x, 2)


def _options(path: tuple, v: float, integer: bool) -> list[float]:
    key = path[-1]
    if path[0] == "rebalance":
        return _neighbors(REBALANCE, v)
    if path[0] == "selection":
        return sorted({max(1, round(v * k)) for k in (0.6, 0.8, 1.25, 1.5)} - {v})
    if path[0] == "risk":
        return [round(x, 3) for x in (v - 0.03, v + 0.03) if 0.02 <= x <= 0.5]
    if key == "value":
        return [_nice(v * k) for k in (0.75, 1.25)]
    if integer:
        return _neighbors(WINDOWS, v)
    return [_nice(v * k) for k in (0.8, 1.25)]


def knob_label(spec: StrategySpec, path: tuple) -> str:
    if path[0] in ("factors", "filters"):
        items = getattr(spec, path[0])
        item = items[path[1]]
        fd = FACTORS[item.name]
        same = [i for i, x in enumerate(items) if x.name == item.name]
        # 同一个因子出现多次时加序号区分（如两个不同窗口的动量）
        prefix = ("过滤·" if path[0] == "filters" else "") + fd.title + (
            f"{same.index(path[1]) + 1}" if len(same) > 1 else "")
        if path[2] == "value":
            return f"{prefix}·阈值"
        return f"{prefix}·{fd.params[path[3]].title or path[3]}"
    if path[0] == "timing":
        fd = FACTORS[spec.timing.name]
        return f"择时·{fd.title}·{fd.params[path[2]].title or path[2]}"
    return {"top_n": "持仓数量", "every_days": "调仓间隔", "stop_loss": "止损", "trailing_stop": "移动止损"}.get(
        path[-1], ".".join(map(str, path)))


def objective(rep: dict, gates: Gates) -> float:
    sharpe = rep.get("sharpe")
    if sharpe is None:
        return -9.0
    mdd = rep.get("max_drawdown") or 0.0
    turnover = rep.get("turnover") or 0.0
    return sharpe - 3 * max(0.0, mdd - gates.max_drawdown) - 0.05 * max(0.0, turnover - gates.max_turnover)


def _set(d: dict, path: tuple, value) -> None:
    for k in path[:-1]:
        d = d[k]
    d[path[-1]] = value


def tune(spec: StrategySpec, panel: Panel, windows: Windows, gates: Gates, budget: int = 30,
         initial_cash: float = 1_000_000) -> TuneResult:
    """坐标搜索：逐个参数尝试相邻的常用值，有改进就采纳；最多两遍、最多 budget 次回测。"""
    start, end = windows.train

    def score(sp: StrategySpec) -> float:
        res = run_backtest(sp, panel, start, end, initial_cash)
        return objective(window_report(res, start, end), gates)

    best = spec
    best_score = score(spec)
    out = TuneResult(spec=spec, evals=1, base_score=best_score, score=best_score)
    original = {path: v for path, v, _ in numeric_knobs(spec)}
    for _ in range(2):
        improved = False
        for path, v, integer in numeric_knobs(best):
            for nv in _options(path, v, integer):
                if out.evals >= budget:
                    break
                data = copy.deepcopy(best.model_dump())
                _set(data, path, int(nv) if integer else nv)
                try:
                    cand = parse_spec(data)
                except ValueError:
                    continue
                s = score(cand)
                out.evals += 1
                if s > best_score + MIN_GAIN:
                    best, best_score, improved = cand, s, True
                    break                       # 该参数已改进，换下一个参数
        if not improved or out.evals >= budget:
            break
    if best is not spec:
        for path, v, _ in numeric_knobs(best):
            old = original.get(path)
            if old is not None and old != v:
                out.changes.append(f"{knob_label(best, path)} {old:g} → {v:g}")
    out.spec, out.score = best, best_score
    return out
