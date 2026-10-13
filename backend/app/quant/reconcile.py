"""回测—跟踪对账与跟踪期健康度（PRD 3.9 G7 / G9）。

对账：用快速回测重算跟踪账户同一时期、同一版本（组合为同一组成员版本）的结果，与交易引擎的实际记录比较。
两者是两套实现（回测用 float、目标权重直接成交；引擎用 Decimal、订单、风控、真实分红送转），
偏差说明「模拟实现」本身有差异；偏差小，跟踪结果的好坏才能归因于策略。
健康度：把跟踪期的收益、回撤、换手放到该策略回测历史的分布中看，明显偏离时提醒（可能失效或环境变化）。
"""

from __future__ import annotations

import math
from datetime import date

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AccountRow, AgentRow, DecisionRow, FillRow, NavSnapshotRow, PositionRow, RoundRow, TrackRow
from app.models.quant import QuantPortfolioRow, QuantStrategyRow, QuantVersionRow
from app.quant.backtest import BacktestResult, run_backtest
from app.quant.data import get_panel
from app.quant.evaluate import window_report
from app.quant.service import LabSettings, settings_of
from app.quant.spec import parse_spec

TE_OK, GAP_OK = 0.03, 0.01           # 跟踪误差（年化）与年化收益差：一致（累计差随时间累积，按年化判断）
TE_WARN, GAP_WARN = 0.08, 0.03       # 有偏差；超过为偏差较大
MIN_DAYS = 5
_STATUS_TEXT = {"一致": "一致", "有偏差": "有一定偏差", "偏差较大": "偏差较大，需要排查",
                "数据不足": "需要更多交易日才能判断"}


def _members_for(s: Session, agent: AgentRow, decisions: list[DecisionRow]) -> tuple[list, str, int | None]:
    """跟踪账户使用的规格：[(规格, 资金权重, 名称)]、股票池、开始对账的决策下标（版本最后一次变化处）。"""
    if agent.quant_portfolio_id:
        pf = s.get(QuantPortfolioRow, agent.quant_portfolio_id)
        weights = {int(m["strategy_id"]): float(m["weight"]) for m in pf.members}
        key = [tuple(sorted((k, v.get("version")) for k, v in (d.parsed or {}).get("members", {}).items()))
               for d in decisions]
        start = max((k for k in range(1, len(key)) if key[k] != key[k - 1]), default=0)
        versions = dict(key[start]) if key else {}
        out, universe = [], None
        for sid, num in versions.items():
            st = s.get(QuantStrategyRow, int(sid))
            v = s.scalars(select(QuantVersionRow).where(QuantVersionRow.strategy_id == st.id,
                                                        QuantVersionRow.number == num)).first()
            if v is not None:
                out.append((parse_spec(v.spec), weights.get(int(sid), 0.0), f"{st.name} v{num}"))
                universe = st.universe_name
        total = sum(w for _, w, _ in out) or 1.0
        return [(sp, w / total, n) for sp, w, n in out], universe, start
    st = s.get(QuantStrategyRow, agent.quant_strategy_id)
    nums = [(d.parsed or {}).get("quant_version") for d in decisions]
    start = max((k for k in range(1, len(nums)) if nums[k] != nums[k - 1]), default=0)
    v = s.scalars(select(QuantVersionRow).where(QuantVersionRow.strategy_id == st.id,
                                                QuantVersionRow.number == nums[start])).first() if nums else None
    return ([(parse_spec(v.spec), 1.0, f"{st.name} v{v.number}")] if v else []), st.universe_name, start


def _alloc_for(s: Session, agent: AgentRow):
    """组合选手的资金配置设置（单个策略为 None）。"""
    from app.quant.portfolio import alloc_of

    if not agent.quant_portfolio_id:
        return None
    pf = s.get(QuantPortfolioRow, agent.quant_portfolio_id)
    return alloc_of(pf) if pf else None


def _backtest(members, panel, start: date, end: date, cash: float, alloc=None) -> BacktestResult:
    from app.quant.portfolio import run_portfolio

    if len(members) > 1:
        return run_portfolio([(sp, w) for sp, w, _ in members], panel, start, end, cash, alloc=alloc)
    return run_backtest(members[0][0], panel, start, end, cash)


def reconcile(s: Session, account_id: int) -> dict:
    acc = s.get(AccountRow, account_id)
    track = s.get(TrackRow, acc.track_id) if acc else None
    agent = s.get(AgentRow, acc.agent_id) if acc else None
    if acc is None or agent is None or agent.kind != "quant":
        return {"status": "不适用", "message": "只有量化选手的账户可以对账"}
    rows = s.execute(select(DecisionRow, RoundRow).join(RoundRow, DecisionRow.round_id == RoundRow.id)
                     .where(DecisionRow.account_id == acc.id).order_by(RoundRow.data_date)).all()
    decisions = [d for d, _ in rows]
    members, universe, k = _members_for(s, agent, decisions)
    if not members:
        return {"status": "数据不足", "message": "还没有决策记录"}
    d0 = rows[k][1].data_date
    navs = s.execute(select(NavSnapshotRow.date, NavSnapshotRow.nav).where(
        NavSnapshotRow.account_id == acc.id, NavSnapshotRow.date >= d0).order_by(NavSnapshotRow.date)).all()
    base = {"track_id": track.id, "mode": track.mode, "start": d0.isoformat(),
            "members": [{"name": n, "weight": round(w, 4)} for _, w, n in members],
            "version_changed": k > 0}
    if len(navs) < 2:
        return base | {"status": "数据不足", "days": 0, "message": "跟踪开始后至少需要两个交易日的净值"}
    live = pd.Series([float(n) for _, n in navs], index=pd.Index([d for d, _ in navs]))
    panel = get_panel(s, universe, LabSettings().train_start, max(live.index[-1], date.today()))
    bt = _backtest(members, panel, live.index[0], live.index[-1], float(live.iloc[0]), _alloc_for(s, agent))
    btn = bt.nav_series().reindex(live.index).ffill()
    lr, br = live.pct_change().dropna(), btn.pct_change().dropna()
    diff = lr - br
    days = len(lr)
    te = float(diff.std(ddof=1) * math.sqrt(252)) if days > 1 else 0.0
    live_ret, bt_ret = float(live.iloc[-1] / live.iloc[0] - 1), float(btn.iloc[-1] / btn.iloc[0] - 1)
    gap = live_ret - bt_ret
    cum_gap = (live / live.iloc[0]) - (btn / btn.iloc[0])

    # 成交对照：同一天、同一标的、同方向的成交价偏差（基点，买入为正表示实际更贵）
    fills = list(s.scalars(select(FillRow).where(FillRow.account_id == acc.id, FillRow.trade_date >= d0)))
    bt_px = {}
    for t in bt.trades:
        bt_px.setdefault((t.date, t.symbol, t.side), []).append(t.price)
    devs = []
    for f in fills:
        side = "buy" if f.side in ("buy", "cover") else "sell"
        ref = bt_px.get((f.trade_date, f.symbol, side))
        if ref:
            dv = float(f.price) / float(np.mean(ref)) - 1
            devs.append(dv if side == "buy" else -dv)
    # 期末持仓权重对照
    nav_last = float(live.iloc[-1])
    live_w: dict[str, float] = {}
    for p in s.scalars(select(PositionRow).where(PositionRow.account_id == acc.id)):
        j = panel.col.get(p.symbol)
        if j is not None and p.qty:
            i = panel.locate(live.index[-1], "before")
            live_w[p.symbol] = p.qty * float(panel.close_ff[i, j]) / nav_last
    bt_w = bt.final_weights
    syms = set(live_w) | set(bt_w)
    overlap = sum(min(live_w.get(x, 0), bt_w.get(x, 0)) for x in syms)
    names = dict(zip(panel.symbols, panel.names, strict=True))
    top = sorted(syms, key=lambda x: -abs(live_w.get(x, 0) - bt_w.get(x, 0)))[:8]
    gap_annual = gap * 252 / max(days, 1) if days >= 60 else gap      # 短期内直接看累计差
    if days < MIN_DAYS:
        status = "数据不足"
    elif te <= TE_OK and abs(gap_annual) <= GAP_OK:
        status = "一致"
    elif te <= TE_WARN and abs(gap_annual) <= GAP_WARN:
        status = "有偏差"
    else:
        status = "偏差较大"
    return base | {
        "status": status, "days": days, "end": live.index[-1].isoformat(),
        "live_return": round(live_ret, 4), "backtest_return": round(bt_ret, 4), "gap": round(gap, 4),
        "gap_annual": round(gap_annual, 4),
        "max_gap": round(float(cum_gap.abs().max()), 4), "tracking_error": round(te, 4),
        "correlation": round(float(lr.corr(br)), 3) if days > 2 else None,
        "trades_live": len(fills), "trades_backtest": len([t for t in bt.trades if t.date >= d0]),
        "price_dev_bps": round(float(np.mean(devs)) * 1e4, 1) if devs else None, "matched_fills": len(devs),
        "holdings_overlap": round(overlap, 4),
        "weight_diffs": [{"symbol": x, "name": names.get(x, x), "live": round(live_w.get(x, 0), 4),
                          "backtest": round(bt_w.get(x, 0), 4)} for x in top],
        "curve": {"dates": [d.isoformat() for d in live.index], "live": [round(v / live.iloc[0], 5) for v in live],
                  "backtest": [round(v / btn.iloc[0], 5) for v in btn]},
        "message": "交易引擎与快速回测的结果" + _STATUS_TEXT[status],
    }


def health(s: Session, account_id: int, recon: dict | None = None) -> dict:
    """跟踪期健康度：把跟踪期表现放到同一组版本回测历史的分布里比较。"""
    recon = recon or reconcile(s, account_id)
    acc = s.get(AccountRow, account_id)
    agent = s.get(AgentRow, acc.agent_id)
    rows = s.execute(select(DecisionRow, RoundRow).join(RoundRow, DecisionRow.round_id == RoundRow.id)
                     .where(DecisionRow.account_id == acc.id).order_by(RoundRow.data_date)).all()
    members, universe, _ = _members_for(s, agent, [d for d, _ in rows])
    navs = s.execute(select(NavSnapshotRow.date, NavSnapshotRow.nav).where(NavSnapshotRow.account_id == acc.id)
                     .order_by(NavSnapshotRow.date)).all()
    days = len(navs) - 1
    if not members or days < MIN_DAYS:
        return {"status": "数据不足", "days": max(days, 0), "checks": [],
                "message": f"跟踪满 {MIN_DAYS} 个交易日后开始检查"}
    st = s.get(QuantStrategyRow, agent.quant_strategy_id) if agent.quant_strategy_id else None
    cfg = settings_of(st) if st else LabSettings()
    live = pd.Series([float(n) for _, n in navs], index=pd.Index([d for d, _ in navs]))
    panel = get_panel(s, universe, cfg.train_start, max(live.index[-1], date.today()))
    w = cfg.windows(panel.dates[-1])
    hist = _backtest(members, panel, cfg.train_start, min(w.end, live.index[0]), float(live.iloc[0]),
                     _alloc_for(s, agent))
    hn = hist.nav_series()
    n = min(days, 60)
    roll = (hn / hn.shift(n) - 1).dropna()
    live_n = float(live.iloc[-1] / live.iloc[-1 - n] - 1)
    pct = float((roll < live_n).mean()) if len(roll) else None
    live_dd = float(-(live / live.cummax() - 1).min())
    hist_dd = float(window_report(hist, cfg.train_start, hist.dates[-1]).get("max_drawdown") or 0)
    checks = []
    status = "正常"
    if pct is not None:
        level = "提醒" if pct < 0.05 else ("关注" if pct < 0.15 else "正常")
        checks.append({"item": f"最近 {n} 个交易日收益", "value": round(live_n, 4),
                       "detail": f"在回测历史同长度收益中排第 {pct:.0%} 分位", "level": level})
    if hist_dd > 0:
        level = "提醒" if live_dd > hist_dd * 1.2 else ("关注" if live_dd > hist_dd * 0.8 else "正常")
        checks.append({"item": "跟踪期最大回撤", "value": round(live_dd, 4),
                       "detail": f"回测历史最大回撤 {hist_dd:.1%}", "level": level})
    if recon.get("status") in ("有偏差", "偏差较大"):
        checks.append({"item": "回测—跟踪对账", "value": recon.get("tracking_error"),
                       "detail": f"跟踪误差 {recon.get('tracking_error', 0):.1%}，累计差 {recon.get('gap', 0):+.1%}",
                       "level": "提醒" if recon["status"] == "偏差较大" else "关注"})
    for c in checks:
        if c["level"] == "提醒":
            status = "提醒"
        elif c["level"] == "关注" and status == "正常":
            status = "关注"
    msg = {"正常": "跟踪期表现处于回测历史的正常范围",
           "关注": "部分指标处于回测历史的偏弱区间，继续观察",
           "提醒": "跟踪期表现明显差于回测历史，策略可能失效或市场环境变化，建议复查"}[status]
    return {"status": status, "days": days, "checks": checks, "message": msg}
