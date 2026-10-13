"""指标计算（PRD 附录 6.4）。分析用途，使用 float 计算；账务计算不在这里。"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

MIN_DAYS_FOR_ANNUALIZED = 60
MIN_DAYS_RELIABLE = 20


@dataclass
class FillLike:
    side: str
    amount: float
    total_fee: float
    realized_pnl: float | None


def _f(x) -> float | None:
    if x is None:
        return None
    x = float(x)
    return None if math.isnan(x) or math.isinf(x) else x


def nav_metrics(nav: pd.Series, bench: pd.Series | None, rf_annual: float = 0.02, periods: int = 252) -> dict:
    """nav / bench：以日期为索引、按日期升序的序列。"""
    nav = nav.astype(float).dropna()
    out: dict = {"days": max(len(nav) - 1, 0), "reliable": len(nav) - 1 >= MIN_DAYS_RELIABLE}
    if len(nav) < 2:
        return out | {"cumulative_return": 0.0 if len(nav) else None}
    r = nav.pct_change().dropna()
    n = len(r)
    cum = nav.iloc[-1] / nav.iloc[0] - 1
    peak = nav.cummax()
    dd = nav / peak - 1
    # 最长回撤期：处于回撤状态的最长连续交易日数
    longest = current = 0
    for v in dd:
        current = current + 1 if v < 0 else 0
        longest = max(longest, current)
    rf_d = rf_annual / periods
    std = r.std(ddof=1) if n > 1 else 0.0
    downside = r[r < 0]
    dstd = math.sqrt((downside ** 2).sum() / n) if n else 0.0
    annual = (1 + cum) ** (periods / n) - 1 if n >= MIN_DAYS_FOR_ANNUALIZED else None
    mdd = -dd.min()
    out |= {
        "cumulative_return": _f(cum),
        "annualized_return": _f(annual),
        "volatility": _f(std * math.sqrt(periods)) if n > 1 else None,
        "max_drawdown": _f(mdd),
        "max_drawdown_days": int(longest),
        "sharpe": _f((r.mean() - rf_d) / std * math.sqrt(periods)) if n > 1 and std > 0 else None,
        "sortino": _f((r.mean() - rf_d) / dstd * math.sqrt(periods)) if dstd > 0 else None,
        "calmar": _f(annual / mdd) if annual is not None and mdd > 0 else None,
    }
    if bench is not None and len(bench.dropna()) >= 2:
        b = bench.astype(float).reindex(nav.index).ffill().dropna()
        if len(b) >= 2:
            br = b.pct_change().dropna()
            joined = pd.concat([r, br], axis=1, join="inner").dropna()
            joined.columns = ["r", "b"]
            bcum = b.iloc[-1] / b.iloc[0] - 1
            out["benchmark_return"] = _f(bcum)
            out["excess_return"] = _f(cum - bcum)
            if len(joined) > 2 and joined["b"].var() > 0:
                beta = joined["r"].cov(joined["b"]) / joined["b"].var()
                alpha = (joined["r"].mean() - beta * joined["b"].mean()) * periods
                active = joined["r"] - joined["b"]
                out["beta"] = _f(beta)
                out["alpha"] = _f(alpha)
                out["information_ratio"] = (_f(active.mean() / active.std(ddof=1) * math.sqrt(periods))
                                            if active.std(ddof=1) > 0 else None)
    return out


def trade_metrics(fills: list[FillLike], initial_cash: float, avg_nav: float, days: int, periods: int = 252) -> dict:
    sells = [f for f in fills if f.side in ("sell", "cover") and f.realized_pnl is not None]   # 平仓成交
    wins = [f.realized_pnl for f in sells if f.realized_pnl > 0]
    losses = [-f.realized_pnl for f in sells if f.realized_pnl < 0]
    traded = sum(f.amount for f in fills)
    fees = sum(f.total_fee for f in fills)
    return {
        "trades": len(fills),
        "closed_trades": len(sells),
        "win_rate": len(wins) / len(sells) if sells else None,
        "profit_factor": (float(np.mean(wins)) / float(np.mean(losses))) if wins and losses else None,
        "turnover": (traded / avg_nav * periods / days) if avg_nav and days > 0 else None,
        "total_fees": fees,
        "fee_ratio": fees / initial_cash if initial_cash else None,
        "realized_pnl": sum(f.realized_pnl for f in sells),
    }


def ai_metrics(decisions: list[dict], orders_total: int, orders_rejected: int) -> dict:
    """decisions: [{status, latency_ms, tokens_in, tokens_out, cost}]，只统计 AI 决策（不含基线）。"""
    llm = [d for d in decisions if d["status"] != "baseline"]
    if not llm:
        return {"rounds": len(decisions), "llm": False}
    lat = [d["latency_ms"] for d in llm if d["latency_ms"]]
    return {
        "llm": True,
        "rounds": len(llm),
        "valid_rate": sum(1 for d in llm if d["status"] == "ok") / len(llm),
        "avg_latency_s": float(np.mean(lat)) / 1000 if lat else None,
        "p95_latency_s": float(np.percentile(lat, 95)) / 1000 if lat else None,
        "tokens_in": sum(d["tokens_in"] for d in llm),
        "tokens_out": sum(d["tokens_out"] for d in llm),
        "cost": sum(float(d["cost"]) for d in llm),
        "reject_rate": orders_rejected / orders_total if orders_total else None,
    }


@dataclass
class TradeLot:
    trade_date: date
    symbol: str
    side: str
    qty: int


def holding_days(trades: list[TradeLot]) -> float | None:
    """按 FIFO 配对开平仓，返回按数量加权的平均持仓天数（自然日；未平仓部分不计）。"""
    queues: dict[str, deque] = {}
    total_qty = total_days = 0
    for t in sorted(trades, key=lambda x: x.trade_date):
        q = queues.setdefault(t.symbol, deque())
        if t.side in ("buy", "short"):
            q.append([t.trade_date, t.qty])
            continue
        left = t.qty
        while left > 0 and q:
            lot = q[0]
            used = min(left, lot[1])
            total_qty += used
            total_days += used * (t.trade_date - lot[0]).days
            lot[1] -= used
            left -= used
            if lot[1] == 0:
                q.popleft()
    return total_days / total_qty if total_qty else None


def leverage_stats(rows: list[tuple[float, float | None, float | None]]) -> dict:
    """rows：(净值, 多头市值, 空头市值)，旧记录没有多空市值时跳过。"""
    lev = [(lv + sv) / nav for nav, lv, sv in rows if lv is not None and sv is not None and nav > 0]
    return {"avg_leverage": float(np.mean(lev)) if lev else None, "max_leverage": max(lev) if lev else None}


def concentration(weights: list[float]) -> dict:
    """weights：各持仓市值占净值比例（空头取绝对值）。HHI 按占总敞口的比例计算，1 表示全部集中在一只。"""
    w = [abs(x) for x in weights if x]
    total = sum(w)
    return {"max_weight": max(w) if w else None,
            "hhi": sum((x / total) ** 2 for x in w) if total else None}


def radar_scores(m: dict) -> dict:
    """五维雷达（0～100）：按固定区间映射，不依赖参与对比的选手，结果可跨赛道比较。"""
    def clip(x: float) -> float:
        return round(max(0.0, min(100.0, x)), 1)

    def val(key: str) -> float | None:
        return _f(m.get(key))

    excess = val("excess_return")
    mdd = val("max_drawdown")
    sharpe = val("sharpe")
    win = val("win_rate")
    pf = val("profit_factor")
    fee = val("fee_ratio")
    return {
        # 收益：超额收益 ±10% 对应 0～100
        "return": clip(50 + excess * 500) if excess is not None else None,
        # 风险控制：最大回撤 0% → 100，20% → 0
        "risk": clip(100 - mdd * 500) if mdd is not None else None,
        # 稳定性：夏普 −2 → 0，+2 → 100
        "stability": clip(50 + sharpe * 25) if sharpe is not None else None,
        # 交易效率：胜率与盈亏比各占一半（盈亏比 2 以上记满分）
        "efficiency": clip(50 * win + 50 * min(pf / 2, 1)) if win is not None and pf is not None else
        (clip(100 * win) if win is not None else None),
        # 成本效率：费用占本金 0% → 100，2% → 0
        "cost": clip(100 - fee * 5000) if fee is not None else None,
    }
