"""回测评估：分区间指标、分年收益、诊断信息、稳健性检验与升级规则（PRD 3.8）。

升级检验（v1.6）：在「开发期」（训练开始 ～ 验证结束，默认 2018～2024）上与冠军逐年对比，
并对按相同波动换算后的月度收益差做配对 bootstrap 检验；同一轮多个候选时按候选数收紧显著性门槛。
留出区间（2025 至今）不参与任何决策。
"""

from __future__ import annotations

import copy
import math
import random
from dataclasses import dataclass
from datetime import date
from statistics import NormalDist

import numpy as np
import pandas as pd

from app.analytics.metrics import nav_metrics
from app.quant.backtest import BacktestResult, CostModel, run_backtest
from app.quant.panel import Panel
from app.quant.spec import StrategySpec, numeric_knobs, parse_spec

ANNUAL = 252


@dataclass
class Windows:
    train: tuple[date, date]
    valid: tuple[date, date]
    holdout: tuple[date, date] | None = None

    @property
    def end(self) -> date:
        return (self.holdout or self.valid)[1]

    @property
    def dev(self) -> tuple[date, date]:
        """开发期：训练开始 ～ 验证结束，升级检验在这段上逐年进行。"""
        return self.train[0], self.valid[1]


@dataclass
class Gates:
    """升级门槛（用户 2026-10-03 确定：风险调整收益更高且最大回撤不超上限；v1.6 改为逐年 + 显著性检验）。"""

    max_drawdown: float = 0.25
    max_turnover: float = 20.0
    robust_keep: float = 0.7
    robust_samples: int = 6
    fold_win_rate: float = 0.5        # 开发期逐年对比中至少跑赢冠军的年份比例
    p_max: float = 0.05               # 月度配对检验的显著性门槛（同轮 n 个候选时为 p_max / n；v1.6 实测后由 0.2 收紧）
    min_holdings: int = 0             # 股票策略的最少持仓数（0 表示不限）


def _r(x, n: int = 4):
    return None if x is None or (isinstance(x, float) and not math.isfinite(x)) else round(float(x), n)


def window_report(res: BacktestResult, start: date, end: date, monthly: bool = False) -> dict:
    """某个区间的指标（区间起点重新归一）。monthly=True 时附带月度收益（升级检验用）。"""
    nav, bench = res.nav_series(), res.bench_series()
    mask = (nav.index >= start) & (nav.index <= end)
    nav_w, bench_w = nav[mask], bench[mask]
    if len(nav_w) < 2:
        return {"days": 0}
    m = nav_metrics(nav_w, bench_w)
    days = len(nav_w) - 1
    avg_nav = float(nav_w.mean())
    trades = [t for t in res.trades if start <= t.date <= end]
    traded = sum(t.amount for t in trades)
    fees = sum(t.fee for t in trades)
    closed = [r for d, r in res.closed if start <= d <= end]
    exp = res.exposure[mask]
    out = {k: _r(v) for k, v in m.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
    out |= {
        "start": nav_w.index[0].isoformat(), "end": nav_w.index[-1].isoformat(), "days": days,
        "turnover": _r(traded / 2 / avg_nav * ANNUAL / max(days, 1), 2),
        "cost_ratio": _r(fees / avg_nav * ANNUAL / max(days, 1)),
        "trades": len(trades),
        "avg_exposure": _r(float(exp.mean())) if len(exp) else None,
        "avg_holdings": _r(float(res.holdings[mask].mean()), 1),
        "win_rate": _r(sum(r > 0 for r in closed) / len(closed)) if closed else None,
        "closed": len(closed),
        "yearly": yearly(nav_w, bench_w),
        "segments": segments(nav_w, bench_w),
    }
    r = nav_w.pct_change().dropna().to_numpy()
    if len(r) > 2 and r.std() > 0:
        z = (r - r.mean()) / r.std()
        # 日频夏普、偏度、峰度：用于按尝试次数折算的夏普（DSR）
        out |= {"sr_daily": _r(float(r.mean() / r.std()), 6), "skew": _r(float((z ** 3).mean())),
                "kurt": _r(float((z ** 4).mean()))}
    if monthly:
        out["monthly"] = monthly_returns(nav_w)
    return out


def monthly_returns(nav: pd.Series) -> list[list]:
    """[[YYYY-MM, 月收益], ...]，与上月末衔接。"""
    out, prev = [], None
    keys = [f"{d.year}-{d.month:02d}" for d in nav.index]
    last: dict[str, float] = {}
    for k, v in zip(keys, nav.to_numpy(), strict=True):
        last[k] = v
    first = nav.iloc[0]
    for k, v in last.items():
        base = prev if prev is not None else first
        out.append([k, _r(v / base - 1, 6)])
        prev = v
    return out


def segments(nav: pd.Series, bench: pd.Series) -> list[dict]:
    """按自然半年切段的收益（与上一段末衔接）。用于检验改进是否在多段行情中都成立，而不是靠一段运气。"""
    out = []
    prev_n = prev_b = None
    keys = [(d.year, 1 if d.month <= 6 else 2) for d in nav.index]
    for key in dict.fromkeys(keys):
        sel = [d for d, k in zip(nav.index, keys, strict=True) if k == key]
        n, b = nav[sel], bench[sel]
        base_n = prev_n if prev_n is not None else n.iloc[0]
        base_b = prev_b if prev_b is not None else b.iloc[0]
        out.append({"period": f"{key[0]}H{key[1]}", "return": _r(n.iloc[-1] / base_n - 1),
                    "benchmark": _r(b.iloc[-1] / base_b - 1), "days": len(sel)})
        prev_n, prev_b = n.iloc[-1], b.iloc[-1]
    return out


def yearly(nav: pd.Series, bench: pd.Series) -> list[dict]:
    out = []
    years = sorted({d.year for d in nav.index})
    prev_nav = prev_bench = None
    for y in years:
        sel = [d for d in nav.index if d.year == y]
        n, b = nav[sel], bench[sel]
        base_n = prev_nav if prev_nav is not None else n.iloc[0]
        base_b = prev_bench if prev_bench is not None else b.iloc[0]
        ret, bret = n.iloc[-1] / base_n - 1, b.iloc[-1] / base_b - 1
        mdd = float((n / n.cummax() - 1).min())
        out.append({"year": y, "return": _r(ret), "benchmark": _r(bret), "excess": _r(ret - bret),
                    "max_drawdown": _r(-mdd), "partial": len(sel) < 200})
        prev_nav, prev_bench = n.iloc[-1], b.iloc[-1]
    return out


def drawdown_periods(nav: pd.Series, top: int = 3) -> list[dict]:
    """最深的几段回撤：开始（前高）、谷底、恢复日期与深度。"""
    peak = nav.cummax()
    dd = nav / peak - 1
    periods, i, n = [], 0, len(nav)
    vals = dd.to_numpy()
    while i < n:
        if vals[i] < 0:
            s = i
            while i < n and vals[i] < 0:
                i += 1
            seg = vals[s:i]
            t = s + int(np.argmin(seg))
            periods.append({"start": nav.index[s - 1 if s > 0 else s].isoformat(), "trough": nav.index[t].isoformat(),
                            "recovered": nav.index[i].isoformat() if i < n else None, "depth": _r(-seg.min())})
        i += 1
    return sorted(periods, key=lambda p: -(p["depth"] or 0))[:top]


def diagnostics(res: BacktestResult, panel: Panel, top: int = 5) -> dict:
    """给 AI 的诊断：回撤区间、贡献最大 / 最小的标的、被涨跌停阻挡的情况。"""
    pnl = sorted(res.pnl_by_symbol.items(), key=lambda kv: -kv[1])
    name = dict(zip(panel.symbols, panel.names, strict=True))

    def fmt(items):
        return [{"symbol": s, "name": name.get(s, s), "pnl": round(v)} for s, v in items]

    return {
        "drawdowns": drawdown_periods(res.nav_series()),
        "best": fmt(pnl[:top]), "worst": fmt(pnl[-top:][::-1]),
        "blocked": sum(res.blocked.values()),
        "exposure_low_days": int((res.exposure < 0.5).sum()),
    }


def evaluate(spec: StrategySpec, panel: Panel, windows: Windows, initial_cash: float = 1_000_000,
             cost: CostModel | None = None) -> tuple[BacktestResult, dict]:
    """从训练区间起点连续回测到最后，按区间切片统计；dev 为开发期（升级检验用，含月度收益）。"""
    res = run_backtest(spec, panel, windows.train[0], windows.end, initial_cash, cost)
    rep = {"train": window_report(res, *windows.train), "valid": window_report(res, *windows.valid),
           "dev": window_report(res, *windows.dev, monthly=True)}
    if windows.holdout:
        rep["holdout"] = window_report(res, *windows.holdout, monthly=True)
    return res, rep


def curves(res: BacktestResult, step: int = 5) -> dict:
    """降采样的净值与基准曲线（归一到 1）。"""
    idx = list(range(0, len(res.dates), step))
    if idx[-1] != len(res.dates) - 1:
        idx.append(len(res.dates) - 1)
    return {"dates": [res.dates[i].isoformat() for i in idx],
            "nav": [round(res.nav[i] / res.nav[0], 4) for i in idx],
            "bench": [round(res.bench[i] / res.bench[0], 4) for i in idx]}


# ---------------------------------------------------------------- 稳健性与升级


def _set(d: dict, path: tuple, value) -> None:
    for k in path[:-1]:
        d = d[k]
    d[path[-1]] = value


def robustness(spec: StrategySpec, panel: Panel, windows: Windows, base_sharpe: float | None, samples: int = 6,
               seed: int = 7, cost: CostModel | None = None) -> dict:
    """所有数值参数同时随机浮动 ±20%，在开发期重新回测，看夏普是否稳定。"""
    knobs = numeric_knobs(spec)
    rng = random.Random(seed)
    sharpes: list[float] = []
    for _ in range(samples):
        data = copy.deepcopy(spec.model_dump())
        for path, v, integer in knobs:
            nv = v * rng.uniform(0.8, 1.2)
            _set(data, path, max(1, round(nv)) if integer else nv)
        try:
            variant = parse_spec(data)
        except ValueError:
            continue
        res = run_backtest(variant, panel, windows.dev[0], windows.dev[1], cost=cost)
        sh = window_report(res, *windows.dev).get("sharpe")
        sharpes.append(sh if sh is not None else 0.0)
    if not sharpes:
        return {"samples": 0}
    med = float(np.median(sharpes))
    ratio = med / base_sharpe if base_sharpe and base_sharpe > 0 else None
    return {"samples": len(sharpes), "median_sharpe": _r(med, 3), "min_sharpe": _r(min(sharpes), 3),
            "max_sharpe": _r(max(sharpes), 3), "ratio": _r(ratio, 3)}


def _v(rep: dict, key: str) -> float | None:
    v = rep.get(key)
    return v if isinstance(v, (int, float)) else None


# ---------------------------------------------------------------- 统计检验


def paired_test(cand: dict, champ: dict, n_boot: int = 2000, seed: int = 0) -> dict | None:
    """候选与冠军开发期月度收益的配对比较。

    先把候选的月收益按两者的波动之比换算到冠军的波动水平（比较的是风险调整后的收益），
    再对月度差值做 bootstrap：p = 重抽样均值 ≤ 0 的比例（单侧）。同时统计逐年的胜负。
    """
    mine = {m: r for m, r in cand.get("monthly") or [] if r is not None}
    theirs = {m: r for m, r in champ.get("monthly") or [] if r is not None}
    months = [m for m in mine if m in theirs]
    if len(months) < 12:
        return None
    a = np.array([mine[m] for m in months])
    b = np.array([theirs[m] for m in months])
    scale = b.std() / a.std() if a.std() > 0 else 1.0
    d = a * scale - b
    rng = np.random.default_rng(seed)
    boot = rng.choice(d, size=(n_boot, len(d)), replace=True).mean(axis=1)
    years: dict[str, list[float]] = {}
    for m, x in zip(months, d, strict=True):
        years.setdefault(m[:4], []).append(float(x))
    full = {y: v for y, v in years.items() if len(v) >= 3}
    return {
        "months": len(d), "scale": round(float(scale), 3),
        "excess_annual": round(float(d.mean() * 12), 4),            # 等波动下的年化超额
        "te_annual": round(float(d.std(ddof=1) * math.sqrt(12)), 4),
        "t": round(float(d.mean() / d.std(ddof=1) * math.sqrt(len(d))), 2) if d.std(ddof=1) > 0 else None,
        "p": round(float((boot <= 0).mean()), 4),
        "year_wins": sum(1 for v in full.values() if sum(v) > 0), "years": len(full),
        "by_year": {y: round(sum(v), 4) for y, v in sorted(full.items())},
    }


_EULER = 0.5772156649


def pbo_cscv(returns: np.ndarray, blocks: int = 12) -> float | None:
    """回测过拟合概率（Probability of Backtest Overfitting，Bailey 等，CSCV 组合对称交叉验证）。

    returns：(月份, 版本) 收益矩阵。把月份切成 blocks 段，每次取一半作为「样本内」、另一半作为「样本外」，
    在样本内挑夏普最高的版本，看它在样本外排在所有版本的什么位置；落在中位数以下的比例即 PBO。
    PBO ≈ 0.5：挑「样本内最好」和随便挑没有区别（研究在拟合噪音）；越低说明样本内的优势越能延续。
    """
    from itertools import combinations

    T, N = returns.shape
    if N < 4 or blocks * 2 > T:
        return None
    T = T // blocks * blocks
    parts = np.array_split(np.arange(returns.shape[0] - T, returns.shape[0]), blocks)
    below = total = 0
    for chosen in combinations(range(blocks), blocks // 2):
        is_rows = np.concatenate([parts[k] for k in chosen])
        oos_rows = np.concatenate([parts[k] for k in range(blocks) if k not in chosen])

        def sharpe(rows):
            r = returns[rows]
            sd = r.std(axis=0, ddof=1)
            return np.where(sd > 0, r.mean(axis=0) / np.where(sd > 0, sd, 1), 0.0)

        best = int(np.argmax(sharpe(is_rows)))
        oos = sharpe(oos_rows)
        rank = (oos < oos[best]).sum() + 0.5 * ((oos == oos[best]).sum() - 1)
        w = (rank + 1) / (N + 1)                   # 样本外相对排名 ∈ (0, 1)
        below += w <= 0.5
        total += 1
    return round(below / total, 3)


def walk_forward(series: dict[int, dict[str, float]], seed: int, min_months: int = 24,
                 holdout_year: int | None = None) -> dict | None:
    """滚动选择检验（walk-forward）：每年年初只用此前的月度收益，按夏普挑出「当时看起来最好」的版本，
    记录它在这一年的收益，并与「一直用初始版本」「所有版本平均」比较。

    这是在检验「按历史表现挑版本」这件事本身有没有用：如果挑出来的版本下一年并不比初始版本或平均水平好，
    说明版本之间的差异主要是噪音。注意候选版本都是 AI 看过开发期之后生成的，这里只检验排名能否延续，
    不是对研究过程的完整模拟。series：{版本号: {"YYYY-MM": 月收益}}；seed：初始版本号。
    """
    df = pd.DataFrame(series).sort_index().dropna()
    if df.shape[1] < 3 or seed not in df.columns:
        return None
    rows = []
    for y in sorted({m[:4] for m in df.index}):
        past, cur = df[df.index < f"{y}-01"], df[df.index.str.startswith(y)]
        if len(past) < min_months or len(cur) < 6:
            continue
        sd = past.std(ddof=1).replace(0, np.nan)
        score = (past.mean() / sd).dropna()
        if score.empty:
            continue
        pick = int(score.idxmax())
        ret = (1 + cur).prod() - 1
        rows.append({"year": int(y), "months": len(cur), "holdout": bool(holdout_year and int(y) >= holdout_year),
                     "chosen": pick, "chosen_return": _r(ret[pick]), "seed_return": _r(ret[seed]),
                     "avg_return": _r(ret.mean()), "best_return": _r(ret.max()),
                     "percentile": _r(float((ret < ret[pick]).sum()) / max(len(ret) - 1, 1), 3)})
    if not rows:
        return None

    def total(key: str) -> float:
        return _r(float(np.prod([1 + r[key] for r in rows]) - 1))

    return {"years": rows, "n": len(rows), "versions": int(df.shape[1]),
            "beat_seed": sum(r["chosen_return"] > r["seed_return"] for r in rows),
            "beat_avg": sum(r["chosen_return"] > r["avg_return"] for r in rows),
            "mean_percentile": _r(float(np.mean([r["percentile"] for r in rows])), 3),
            "chosen_total": total("chosen_return"), "seed_total": total("seed_return"),
            "avg_total": total("avg_return")}


def deflated_sharpe(rep: dict, n_trials: int, sharpe_var: float | None) -> float | None:
    """按尝试次数折算的夏普（Bailey & López de Prado）：扣除「试了 n 次挑最好」的影响后，真实夏普大于 0 的概率。

    rep：开发期报告（含日频夏普、偏度、峰度、天数）；sharpe_var：各次尝试的年化夏普的方差。
    """
    sr, days = _v(rep, "sr_daily"), _v(rep, "days")
    if sr is None or not days or days < 30:
        return None
    skew, kurt = _v(rep, "skew") or 0.0, _v(rep, "kurt") or 3.0
    sr0 = 0.0
    if n_trials > 1:
        var = (sharpe_var or 0.0) / ANNUAL                    # 年化 → 日频
        if var <= 0:
            var = 1 / days
        z = NormalDist().inv_cdf
        sr0 = math.sqrt(var) * ((1 - _EULER) * z(1 - 1 / n_trials) + _EULER * z(1 - 1 / (n_trials * math.e)))
    denom = 1 - skew * sr + (kurt - 1) / 4 * sr ** 2
    if denom <= 0:
        return None
    return round(NormalDist().cdf((sr - sr0) * math.sqrt(days - 1) / math.sqrt(denom)), 4)


def check_gates(cand: dict, champ: dict | None, gates: Gates, n_tests: int = 1,
                holdings: int | None = None) -> tuple[bool, list[str], dict | None]:
    """候选是否可以替换冠军。cand / champ：{"dev": 开发期报告, "robustness": {...}}。

    返回 (是否通过, 原因列表, 配对检验结果)。条件：
    1. 开发期最大回撤不超过上限（冠军本身超过时不比冠军更差）；年化换手不超过上限；股票策略持仓不少于下限；
    2. 开发期夏普高于冠军；
    3. 逐年对比（等波动换算）至少 fold_win_rate 的年份跑赢冠军；
    4. 月度配对检验 p ≤ p_max / n_tests；
    5. 参数 ±20% 扰动后开发期夏普中位数不低于原值的 robust_keep。
    """
    reasons: list[str] = []
    ok = True
    cd = cand["dev"]
    pd_ = champ["dev"] if champ else {}
    mdd, champ_mdd = _v(cd, "max_drawdown"), _v(pd_, "max_drawdown")
    limit = max(gates.max_drawdown, champ_mdd or 0)
    if mdd is not None and mdd > limit:
        ok = False
        extra = f"（冠军为 {champ_mdd:.1%}）" if champ_mdd and champ_mdd > gates.max_drawdown else ""
        reasons.append(f"开发期最大回撤 {mdd:.1%} 超过上限 {gates.max_drawdown:.0%}{extra}")
    turnover = _v(cd, "turnover")
    if turnover is not None and turnover > gates.max_turnover:
        ok = False
        reasons.append(f"年化换手 {turnover:.1f} 倍超过上限 {gates.max_turnover:g} 倍")
    if gates.min_holdings and holdings is not None and holdings < gates.min_holdings:
        ok = False
        reasons.append(f"只持有 {holdings} 只股票，少于下限 {gates.min_holdings} 只（过于集中，个股运气影响太大）")
    sv = _v(cd, "sharpe")
    if sv is None:
        return False, [*reasons, "开发期无法计算夏普比率"], None
    test = None
    if champ:
        psv = _v(pd_, "sharpe")
        if psv is not None and sv <= psv:
            ok = False
            reasons.append(f"开发期夏普 {sv:.2f} 不高于冠军 {psv:.2f}")
        test = paired_test(cd, pd_)
        if test is None:
            ok = False
            reasons.append("与冠军可比较的月份不足 12 个")
        else:
            need = math.ceil(test["years"] * gates.fold_win_rate)
            if test["year_wins"] < need:
                ok = False
                reasons.append(f"{test['years']} 年中只有 {test['year_wins']} 年跑赢冠军"
                               f"（至少需要 {need} 年），改进不够一致")
            p_need = gates.p_max / max(n_tests, 1)
            if test["p"] > p_need:
                ok = False
                reasons.append(f"月度超额 {test['excess_annual']:+.1%}/年 不显著"
                               f"（p={test['p']:.3g}，需 ≤ {p_need:.2g}，按累计 {n_tests} 个候选校正）")
    rb = cand.get("robustness") or {}
    ratio = rb.get("ratio")
    if rb.get("samples") and sv > 0 and ratio is not None and ratio < gates.robust_keep:
        ok = False
        reasons.append(f"参数浮动 ±20% 后开发期夏普中位数只有原来的 {ratio:.0%}"
                       f"（低于 {gates.robust_keep:.0%}），不够稳健")
    if ok:
        base = f"开发期夏普 {sv:.2f}"
        if champ and test:
            base += (f"（冠军 {_v(pd_, 'sharpe'):.2f}）；等波动月度超额 {test['excess_annual']:+.1%}/年，"
                     f"p={test['p']:.2f}；{test['years']} 年中 {test['year_wins']} 年跑赢冠军")
        reasons.insert(0, base + "；回撤、换手与稳健性均达标")
    return ok, reasons, test
