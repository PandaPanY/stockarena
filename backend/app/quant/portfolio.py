"""多风格组合（PRD 3.9 G9）：把资金按权重分给若干策略的冠军版本，在同一个账户中运行。

不同风格的策略相关性较低，组合后回撤与波动通常小于单个策略，更容易在实盘中坚持并复现。
回测与实时跟踪规则相同：组合目标权重 = Σ 成员权重 × 成员目标权重，各成员按自己的调仓周期更新
（见 backtest.PortfolioTargeter 与 live.decide_portfolio）；combine 仅用于把分仓结果相加的场景。
"""

from __future__ import annotations

import json
from collections import OrderedDict
from dataclasses import dataclass

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from app.models.quant import QuantPortfolioRow, QuantStrategyRow, QuantVersionRow
from app.quant.backtest import BacktestResult, run_backtest
from app.quant.evaluate import curves, window_report
from app.quant.panel import Panel
from app.quant.service import LabError, LabSettings, panel_for, settings_of
from app.quant.spec import StrategySpec, parse_spec

_cache: OrderedDict[tuple, dict] = OrderedDict()


@dataclass
class Member:
    strategy: QuantStrategyRow
    version: QuantVersionRow
    spec: StrategySpec
    weight: float


def members_of(s: Session, pf: QuantPortfolioRow) -> list[Member]:
    out = []
    total = sum(float(m.get("weight") or 0) for m in pf.members) or 1.0
    for m in pf.members:
        st = s.get(QuantStrategyRow, int(m["strategy_id"]))
        if st is None or not st.champion_id:
            continue
        v = s.get(QuantVersionRow, st.champion_id)
        out.append(Member(st, v, parse_spec(v.spec), float(m.get("weight") or 0) / total))
    return out


def validate_members(s: Session, members: list[dict], user_id: int | None = None) -> list[dict]:
    """校验组合成员；给出 user_id 时成员必须是该用户自己的策略（公共策略需先另存）。"""
    if not 2 <= len(members) <= 10:
        raise LabError("组合需要 2～10 个策略")
    seen, out, universe = set(), [], None
    for m in members:
        sid, w = int(m["strategy_id"]), float(m.get("weight") or 0)
        st = s.get(QuantStrategyRow, sid)
        if st is None or not st.champion_id or (user_id is not None and st.user_id != user_id):
            raise LabError(f"策略 {sid} 不存在或没有冠军版本（组合只能使用自己的策略，公共策略请先另存）")
        if sid in seen:
            raise LabError(f"策略「{st.name}」重复")
        if w <= 0:
            raise LabError("权重必须大于 0")
        if universe and st.universe_name != universe:
            raise LabError("组合内的策略需使用同一个股票池")
        universe = st.universe_name
        seen.add(sid)
        out.append({"strategy_id": sid, "weight": w})
    total = sum(m["weight"] for m in out)
    return [{"strategy_id": m["strategy_id"], "weight": round(m["weight"] / total, 4)} for m in out]


def combine(results: list[BacktestResult]) -> BacktestResult:
    """把按资金分仓的成员回测逐日相加（成员回测的日期必须一致）。"""
    nav = np.sum([r.nav for r in results], axis=0)
    value = np.sum([r.exposure * r.nav for r in results], axis=0)
    pnl: dict[str, float] = {}
    for r in results:
        for k, v in r.pnl_by_symbol.items():
            pnl[k] = pnl.get(k, 0.0) + v
    final: dict[str, float] = {}
    for r in results:
        for k, v in r.final_weights.items():
            final[k] = final.get(k, 0.0) + v * r.nav[-1] / nav[-1]
    return BacktestResult(
        dates=results[0].dates, nav=nav, bench=results[0].bench, exposure=value / nav,
        holdings=np.sum([r.holdings for r in results], axis=0),
        trades=sorted((t for r in results for t in r.trades), key=lambda t: t.date),
        initial_cash=sum(r.initial_cash for r in results), final_weights=final, pnl_by_symbol=pnl,
        closed=sorted((c for r in results for c in r.closed), key=lambda c: c[0]),
        episodes=[e for r in results for e in r.episodes],
        buy_want=sum(r.buy_want for r in results), buy_filled=sum(r.buy_filled for r in results),
        lot_blocked=sum(r.lot_blocked for r in results), impact_cost=sum(r.impact_cost for r in results),
        dividend_tax=sum(r.dividend_tax for r in results),
        deviation=float(np.average([r.deviation for r in results], weights=[r.initial_cash for r in results])),
        blocked={k: sum(r.blocked.get(k, 0) for r in results) for r in results for k in r.blocked},
    )


def run_portfolio(members, panel: Panel, start, end, cash: float, alloc=None, **kw) -> BacktestResult:
    """组合在单个账户中的回测。members：Member 列表或 [(规格, 权重)]；alloc：AllocSettings（默认固定权重）。"""
    from app.quant.backtest import PortfolioTargeter

    pairs = [(m.spec, m.weight) if isinstance(m, Member) else (m[0], m[1]) for m in members]
    return run_backtest(None, panel, start, end, cash, targeter=PortfolioTargeter(pairs, alloc), **kw)


def alloc_of(pf: QuantPortfolioRow):
    from app.quant.allocation import AllocSettings

    return AllocSettings.from_dict((pf.settings or {}).get("alloc"))


def _monthly(nav: pd.Series) -> pd.Series:
    m = nav.groupby([d.strftime("%Y-%m") for d in nav.index]).last()
    return m.pct_change().dropna()


def evaluate_portfolio(s: Session, pf: QuantPortfolioRow) -> dict:
    members = members_of(s, pf)
    if len(members) < 2:
        raise LabError("组合中可用的策略少于 2 个")
    cfg: LabSettings = settings_of(members[0].strategy)
    panel: Panel = panel_for(s, members[0].strategy)
    alloc = alloc_of(pf)
    from app.quant.costs import current as current_costs

    key = (pf.id, tuple((m.version.id, m.weight) for m in members), panel.dates[-1],
           json.dumps(alloc.to_dict(), sort_keys=True), current_costs().signature())
    if key in _cache:
        return _cache[key]
    w = cfg.windows(panel.dates[-1])
    # 组合本身：在同一个账户中按与实时跟踪相同的规则运行（成员间定期再平衡；动态配置时按成员收益调整资金）
    from app.quant.backtest import PortfolioTargeter

    targeter = PortfolioTargeter([(m.spec, m.weight) for m in members], alloc)
    res = run_backtest(None, panel, cfg.train_start, w.end, cfg.initial_cash, targeter=targeter)
    # 成员各自独立回测：只用于展示成员表现与相关性
    results = [run_backtest(m.spec, panel, cfg.train_start, w.end, cfg.initial_cash * m.weight) for m in members]
    rep = {"train": window_report(res, *w.train), "valid": window_report(res, *w.valid),
           "dev": window_report(res, *w.dev), "holdout": window_report(res, *w.holdout) if w.holdout else {}}
    # 成员各自的表现与开发期月度收益相关性（越低分散效果越好）
    navs = {m.strategy.id: r.nav_series() for m, r in zip(members, results, strict=True)}
    dev_mask = lambda sr: sr[(sr.index >= w.dev[0]) & (sr.index <= w.dev[1])]  # noqa: E731
    monthly = pd.DataFrame({k: _monthly(dev_mask(v)) for k, v in navs.items()})
    corr = monthly.corr().round(2)
    member_rows = []
    for m, r in zip(members, results, strict=True):
        mrep = {k: window_report(r, *rng) for k, rng in (("dev", w.dev), ("holdout", w.holdout)) if rng}
        member_rows.append({
            "strategy_id": m.strategy.id, "name": m.strategy.name, "style": m.strategy.style,
            "version": m.version.number, "weight": m.weight,
            "dev": {k: mrep["dev"].get(k) for k in ("annualized_return", "sharpe", "max_drawdown")},
            "holdout": {k: mrep.get("holdout", {}).get(k) for k in ("cumulative_return", "sharpe", "max_drawdown")},
            "curve": [round(x / r.nav[0], 4) for x in r.nav[::5]],
        })
    out = {
        "train": rep["train"], "valid": rep["valid"], "dev": rep["dev"], "holdout": rep["holdout"],
        "curves": curves(res) | {"train_end": cfg.train_end.isoformat(), "valid_end": cfg.valid_end.isoformat()},
        "members": member_rows,
        "correlation": {"names": [m.strategy.name for m in members], "matrix": corr.fillna(0).values.tolist()},
        "avg_correlation": (round(float(corr.values[np.triu_indices(len(corr), 1)].mean()), 3)
                            if len(corr) > 1 else None),
        "alloc": alloc.to_dict() | {"text": alloc.describe(), "names": [m.strategy.name for m in members],
                                    "log": targeter.alloc_log[-36:]},
    }
    _cache[key] = out
    while len(_cache) > 6:
        _cache.popitem(last=False)
    return out


def signal(s: Session, pf: QuantPortfolioRow, capital: float | None = None) -> dict:
    """组合的最新目标持仓：Σ 成员权重 × 成员冠军的目标权重。"""
    from app.quant import live
    from app.quant.allocation import allocate, member_returns

    members = members_of(s, pf)
    alloc = alloc_of(pf)
    alloc_w = [m.weight for m in members]
    if alloc.dynamic and members:
        panel = panel_for(s, members[0].strategy)
        w_, scale, _ = allocate(member_returns([m.spec for m in members], panel), len(panel.dates) - 1,
                                np.array(alloc_w), alloc)
        alloc_w = [float(x) * scale for x in w_]
    combined: dict[str, dict] = {}
    parts = []
    date_ = None
    for m, aw in zip(members, alloc_w, strict=True):
        sig = live.signal(s, m.strategy)
        date_ = sig["date"]
        parts.append({"name": m.strategy.name, "weight": round(aw, 4), "version": sig["version"],
                      "timing_on": sig["timing_on"], "holdings": len(sig["holdings"])})
        for h in sig["holdings"]:
            row = combined.setdefault(h["symbol"], {"symbol": h["symbol"], "name": h["name"], "weight": 0.0,
                                                    "close": h["close"], "from": []})
            row["weight"] += aw * h["weight"]
            row["from"].append(m.strategy.name)
    rows = sorted(combined.values(), key=lambda r: -r["weight"])
    for r in rows:
        r["weight"] = round(r["weight"], 4)
        if capital:
            lot = 100
            r["shares"] = int(capital * r["weight"] / r["close"] // lot * lot)
            r["amount"] = round(r["shares"] * r["close"], 2)
    return {"date": date_, "members": parts, "holdings": rows,
            "cash_weight": round(1 - sum(r["weight"] for r in rows), 4)}
