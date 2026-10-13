"""面向界面的查询：排行榜、净值序列、账户详情、决策列表。返回可直接序列化的 dict。"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.analytics.metrics import (
    FillLike,
    TradeLot,
    ai_metrics,
    concentration,
    holding_days,
    leverage_stats,
    nav_metrics,
    radar_scores,
    trade_metrics,
)
from app.arena.accounts import load_state
from app.market.universe import BENCHMARK_NAMES
from app.models import (
    AccountRow,
    AgentRow,
    DailyBarRow,
    DecisionRow,
    EventRow,
    FillRow,
    InstrumentRow,
    LLMProviderRow,
    NavSnapshotRow,
    OrderRow,
    PositionRow,
    ReflectionRow,
    RoundRow,
    StrategyVersionRow,
    TrackRow,
)
from app.trading.params import RuleParams


def _s(x: Decimal | None) -> str | None:
    return None if x is None else format(x, "f")


def _nav_frame(s: Session, account_id: int) -> pd.DataFrame:
    rows = s.execute(select(NavSnapshotRow.date, NavSnapshotRow.nav, NavSnapshotRow.benchmark_close)
                     .where(NavSnapshotRow.account_id == account_id).order_by(NavSnapshotRow.date)).all()
    df = pd.DataFrame(rows, columns=["date", "nav", "bench"])
    if not df.empty:
        df["nav"] = df["nav"].astype(float)
        df["bench"] = df["bench"].map(lambda x: float(x) if x is not None else None)
        df = df.set_index("date")
    return df


def account_metrics(s: Session, acc: AccountRow, track: TrackRow) -> dict:
    navdf = _nav_frame(s, acc.id)
    nm = nav_metrics(navdf["nav"], navdf["bench"]) if not navdf.empty else {"days": 0, "reliable": False}
    fill_rows = list(s.scalars(select(FillRow).where(FillRow.account_id == acc.id)))
    fills = [FillLike(f.side, float(f.amount), float(f.total_fee),
                      float(f.realized_pnl) if f.realized_pnl is not None else None) for f in fill_rows]
    avg_nav = float(navdf["nav"].mean()) if not navdf.empty else float(track.initial_cash)
    tm = trade_metrics(fills, float(track.initial_cash), avg_nav, nm.get("days", 0))
    tm["avg_holding_days"] = holding_days([TradeLot(f.trade_date, f.symbol, f.side, f.qty) for f in fill_rows])
    lev_rows = s.execute(select(NavSnapshotRow.nav, NavSnapshotRow.long_value, NavSnapshotRow.short_value)
                         .where(NavSnapshotRow.account_id == acc.id)).all()
    tm |= leverage_stats([(float(n), float(lv) if lv is not None else None, float(sv) if sv is not None else None)
                          for n, lv, sv in lev_rows])
    tm |= concentration([w for w in (p["weight"] for p in positions(s, acc, track)) if w is not None])
    counts = dict(s.execute(select(EventRow.type, func.count()).where(
        EventRow.account_id == acc.id, EventRow.type.in_(["margin_call", "forced_liquidation"]))
        .group_by(EventRow.type)).all())
    tm["margin_calls"] = counts.get("margin_call", 0)
    tm["forced_liquidations"] = counts.get("forced_liquidation", 0)
    decisions = [{"status": d.status, "latency_ms": d.latency_ms, "tokens_in": d.tokens_in,
                  "tokens_out": d.tokens_out, "cost": d.cost}
                 for d in s.scalars(select(DecisionRow).where(DecisionRow.account_id == acc.id))]
    total = s.scalar(select(func.count()).select_from(OrderRow).where(OrderRow.account_id == acc.id)) or 0
    rejected = s.scalar(select(func.count()).select_from(OrderRow).where(
        OrderRow.account_id == acc.id, OrderRow.status == "rejected")) or 0
    am = ai_metrics(decisions, total, rejected)
    latest = navdf["nav"].iloc[-1] if not navdf.empty else float(track.initial_cash)
    # 成本收益比：超额收益金额 / AI 累计费用（本地模型费用为 0 时不计算）
    excess = nm.get("excess_return")
    cost = am.get("cost") or 0
    am["cost_benefit"] = excess * float(track.initial_cash) / cost if excess is not None and cost > 0 else None
    from app.arena.consistency import account_rate  # 避免循环导入

    cons = account_rate(s, acc.id)
    am["consistency_rate"] = cons["rate"] if cons else None
    am["consistency_checked"] = cons["checked"] if cons else 0
    out = {"nav": latest, **nm, **tm, "ai": am}
    out["radar"] = radar_scores(out)
    return out


def leaderboard(s: Session, track: TrackRow) -> list[dict]:
    rows = []
    for acc in s.scalars(select(AccountRow).where(AccountRow.track_id == track.id)):
        agent = s.get(AgentRow, acc.agent_id)
        m = account_metrics(s, acc, track)
        sv = s.scalar(select(func.max(StrategyVersionRow.version)).where(StrategyVersionRow.account_id == acc.id))
        rows.append({
            "account_id": acc.id, "agent_id": agent.id, "agent_name": agent.name, "kind": agent.kind,
            "baseline_type": agent.baseline_type, "model": agent.model, "reflect_enabled": agent.reflect_enabled,
            "strategy_version": sv, **m,
        })
    rows.sort(key=lambda r: r.get("cumulative_return") or 0, reverse=True)
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return rows


def nav_series(s: Session, track: TrackRow) -> dict:
    accounts = []
    bench_points: dict[date, float] = {}
    for acc in s.scalars(select(AccountRow).where(AccountRow.track_id == track.id)):
        agent = s.get(AgentRow, acc.agent_id)
        df = _nav_frame(s, acc.id)
        base = float(track.initial_cash)
        accounts.append({
            "account_id": acc.id, "name": agent.name, "kind": agent.kind,
            "points": [[str(d), round(v / base, 6), round(v, 2)] for d, v in df["nav"].items()] if not df.empty else [],
        })
        if not df.empty:
            for d, b in df["bench"].items():
                if b is not None:
                    bench_points[d] = b
    bench = []
    if bench_points:
        first = bench_points[min(bench_points)]
        bench = [[str(d), round(v / first, 6), v] for d, v in sorted(bench_points.items())]
    return {"benchmark": {"symbol": track.benchmark_symbol,
                          "name": BENCHMARK_NAMES.get(track.benchmark_symbol, track.benchmark_symbol),
                          "points": bench},
            "accounts": accounts}


def positions(s: Session, acc: AccountRow, track: TrackRow) -> list[dict]:
    rows = list(s.scalars(select(PositionRow).where(PositionRow.account_id == acc.id)))
    nav = s.scalars(select(NavSnapshotRow.nav).where(NavSnapshotRow.account_id == acc.id)
                    .order_by(NavSnapshotRow.date.desc()).limit(1)).first() or track.initial_cash
    out = []
    for p in rows:
        inst = s.get(InstrumentRow, p.symbol)
        bar = s.scalars(select(DailyBarRow).where(DailyBarRow.symbol == p.symbol)
                        .order_by(DailyBarRow.date.desc()).limit(1)).first()
        close = bar.close if bar else p.avg_cost
        mv = close * p.qty                       # 空头为负
        short = p.qty < 0
        pnl_pct = (float(p.avg_cost / close - 1) if short else float(close / p.avg_cost - 1)) \
            if p.avg_cost and close else None
        out.append({
            "symbol": p.symbol, "name": inst.name if inst else p.symbol, "qty": p.qty, "short": short,
            "type": inst.type if inst else None,
            "avg_cost": _s(p.avg_cost.quantize(Decimal("0.001"))), "close": _s(close), "market_value": _s(mv),
            "pnl": _s((close - p.avg_cost) * p.qty), "pnl_pct": pnl_pct,
            "weight": float(mv / nav) if nav else None, "price_date": str(bar.date) if bar else None,
        })
    out.sort(key=lambda x: abs(float(x["market_value"])), reverse=True)
    return out


def order_dict(o: OrderRow) -> dict:
    return {"id": o.id, "decision_id": o.decision_id, "symbol": o.symbol, "side": o.side, "qty": o.qty,
            "filled_qty": o.filled_qty, "status": o.status, "reason": o.reason, "note": o.note,
            "placed_date": str(o.placed_date), "target_date": str(o.target_date) if o.target_date else None,
            "order_type": o.order_type, "limit_price": _s(o.limit_price), "stop_price": _s(o.stop_price),
            "tif": o.tif, "expires_on": str(o.expires_on) if o.expires_on else None, "oco_group": o.oco_group,
            "attach_stop": _s(o.attach_stop), "attach_take_profit": _s(o.attach_take_profit),
            "active_from": o.active_from.isoformat() if o.active_from else None, "forced": o.forced}


def fill_dict(f: FillRow) -> dict:
    return {"id": f.id, "order_id": f.order_id, "trade_date": str(f.trade_date), "symbol": f.symbol, "side": f.side,
            "qty": f.qty, "price": _s(f.price), "amount": _s(f.amount), "fees": f.fees,
            "total_fee": _s(f.total_fee), "realized_pnl": _s(f.realized_pnl)}


def decision_summary(s: Session, d: DecisionRow, rnd: RoundRow, agent: AgentRow) -> dict:
    orders = list(s.scalars(select(OrderRow).where(OrderRow.decision_id == d.id)))
    return {
        "id": d.id, "round_id": rnd.id, "data_date": str(rnd.data_date), "fill_date": str(rnd.fill_date),
        "trigger": rnd.trigger, "account_id": d.account_id, "agent_name": agent.name, "agent_kind": agent.kind,
        "status": d.status, "model": d.model, "analysis": d.analysis, "error": d.error,
        "orders": len(orders), "accepted": sum(1 for o in orders if o.status != "rejected"),
        "filled": sum(1 for o in orders if o.status in ("filled", "partially_filled")),
        "rejected": sum(1 for o in orders if o.status == "rejected"),
        "latency_ms": d.latency_ms, "tokens_in": d.tokens_in, "tokens_out": d.tokens_out, "cost": _s(d.cost),
        "strategy_version": d.strategy_version, "created_at": d.created_at.isoformat(),
    }


def kline(s: Session, account_id: int, symbol: str, days: int = 180) -> dict:
    last = s.scalar(select(func.max(DailyBarRow.date)).where(DailyBarRow.symbol == symbol))
    if last is None:
        return {"symbol": symbol, "bars": [], "fills": []}
    rows = list(s.scalars(select(DailyBarRow).where(DailyBarRow.symbol == symbol,
                                                    DailyBarRow.date >= last - timedelta(days=days))
                          .order_by(DailyBarRow.date)))
    fills = s.scalars(select(FillRow).where(FillRow.account_id == account_id, FillRow.symbol == symbol)
                      .order_by(FillRow.trade_date))
    inst = s.get(InstrumentRow, symbol)
    return {
        "symbol": symbol, "name": inst.name if inst else symbol,
        "bars": [[str(r.date), float(r.open), float(r.close), float(r.low), float(r.high), r.volume] for r in rows],
        "fills": [{**fill_dict(f), "reason": (s.get(OrderRow, f.order_id).reason or "")} for f in fills],
    }


def strategy_version_dict(v: StrategyVersionRow) -> dict:
    return {"id": v.id, "version": v.version, "content": v.content, "source": v.source, "changes": v.changes,
            "data_date": str(v.data_date) if v.data_date else None, "reflection_id": v.reflection_id,
            "created_at": v.created_at.isoformat()}


def reflection_summary(r: ReflectionRow) -> dict:
    return {"id": r.id, "data_date": str(r.data_date), "period_start": str(r.period_start) if r.period_start else None,
            "trigger": r.trigger, "status": r.status, "model": r.model, "base_version": r.base_version,
            "new_version": r.new_version, "stats": r.stats, "review": r.review, "changes": r.changes,
            "error": r.error, "latency_ms": r.latency_ms, "tokens_in": r.tokens_in, "tokens_out": r.tokens_out,
            "cost": _s(r.cost), "created_at": r.created_at.isoformat()}


def reflection_detail(r: ReflectionRow) -> dict:
    return {**reflection_summary(r), "prompt_system": r.prompt_system, "prompt_user": r.prompt_user,
            "raw_response": r.raw_response, "reasoning": r.reasoning, "attempts": r.attempts,
            "template_version": r.template_version}


def _latest_prices(s: Session, symbols: list[str]) -> dict[str, Decimal]:
    out = {}
    for sym in symbols:
        bar = s.scalars(select(DailyBarRow).where(DailyBarRow.symbol == sym)
                        .order_by(DailyBarRow.date.desc()).limit(1)).first()
        if bar is not None:
            out[sym] = bar.close
    return out


def margin_info(s: Session, acc: AccountRow, track: TrackRow) -> dict | None:
    """做空 / 融资赛道的杠杆与保证金状态（只做多赛道返回 None）。"""
    p = RuleParams(**track.rule_params)
    if not p.leveraged:
        return None
    state = load_state(s, acc)
    prices = _latest_prices(s, list(state.positions))
    for sym, pos in state.positions.items():
        prices.setdefault(sym, pos.avg_cost)
    ratio = state.margin_ratio(prices, p)
    nav = state.nav(prices)
    return {
        "allow_short": p.allow_short, "allow_margin": p.allow_margin, "leverage_cap": float(p.leverage_cap),
        "method": p.margin_check_method, "maintenance": float(p.maintenance_margin),
        "liquidation": float(p.liquidation_margin),
        "long_value": float(state.long_value(prices)), "short_value": float(state.short_value(prices)),
        "debt": float(-state.cash) if state.cash < 0 else 0.0,
        "leverage": float(state.gross_exposure(prices) / nav) if nav > 0 else None,
        "ratio": float(ratio) if ratio is not None else None,
        "margin_call_since": str(acc.margin_call_since) if acc.margin_call_since else None,
    }


def compare(s: Session, accounts: list[tuple[AccountRow, TrackRow]]) -> dict:
    """2～5 个选手的深度对比（PRD US-6.4）：指标、雷达、日收益相关性、持仓重合度、持仓构成。"""
    items, returns, holdings, traded = [], {}, {}, {}
    for acc, track in accounts:
        agent = s.get(AgentRow, acc.agent_id)
        key = str(acc.id)
        m = account_metrics(s, acc, track)
        pos = positions(s, acc, track)
        df = _nav_frame(s, acc.id)
        if not df.empty:
            returns[key] = df["nav"].pct_change().dropna()
        holdings[key] = {p["symbol"]: p["weight"] or 0.0 for p in pos}
        traded[key] = set(s.scalars(select(FillRow.symbol).where(FillRow.account_id == acc.id)))
        gross = sum(abs(p["weight"] or 0) for p in pos)
        comp = {"stock_long": 0.0, "etf_long": 0.0, "short": 0.0}
        for p in pos:
            w = abs(p["weight"] or 0)
            if p["short"]:
                comp["short"] += w
            elif p["type"] == "etf":
                comp["etf_long"] += w
            else:
                comp["stock_long"] += w
        items.append({
            "account_id": acc.id, "agent_name": agent.name, "kind": agent.kind, "model": agent.model,
            "track_id": track.id, "track_name": track.name, "currency": track.currency,
            "initial_cash": float(track.initial_cash), "metrics": m, "mode": track.mode,
            "lookahead": next((r["level"] for r in lookahead_risks(s, track) if r["account_id"] == acc.id), None),
            "composition": {**comp, "cash": max(0.0, 1 - gross) if gross <= 1 else 0.0},
            "nav": [[str(d), round(v / float(track.initial_cash), 6), round(v, 2)] for d, v in df["nav"].items()]
            if not df.empty else [],
        })
    keys = [str(acc.id) for acc, _ in accounts]
    corr = [[None] * len(keys) for _ in keys]
    if len(returns) >= 2:
        frame = pd.DataFrame(returns).dropna()
        if len(frame) >= 3:
            c = frame.corr()
            corr = [[(round(float(c.loc[a, b]), 3) if a in c.index and b in c.columns and pd.notna(c.loc[a, b])
                      else None) for b in keys] for a in keys]
    overlap = [[_overlap(holdings[a], holdings[b]) for b in keys] for a in keys]
    traded_overlap = [[(len(traded[a] & traded[b]) / len(traded[a] | traded[b])) if traded[a] | traded[b] else None
                       for b in keys] for a in keys]
    return {"accounts": items, "correlation": corr, "holding_overlap": overlap, "traded_overlap": traded_overlap,
            "repeats": _repeats(accounts, items)}


def _repeats(accounts: list[tuple[AccountRow, TrackRow]], items: list[dict]) -> list[dict]:
    """结果稳定性（PRD 3.6）：同一选手在多个赛道中的收益分布（均值、标准差、最好、最差）。"""
    groups: dict[int, list[dict]] = {}
    for (acc, _), item in zip(accounts, items, strict=True):
        groups.setdefault(acc.agent_id, []).append(item)
    out = []
    for agent_id, group in groups.items():
        if len(group) < 2:
            continue
        stats = {}
        for key in ("cumulative_return", "excess_return", "max_drawdown"):
            vals = [g["metrics"].get(key) for g in group if g["metrics"].get(key) is not None]
            if vals:
                mean = sum(vals) / len(vals)
                std = (sum((v - mean) ** 2 for v in vals) / (len(vals) - 1)) ** 0.5 if len(vals) > 1 else 0.0
                stats[key] = {"mean": mean, "std": std, "min": min(vals), "max": max(vals)}
        out.append({"agent_id": agent_id, "agent_name": group[0]["agent_name"], "runs": len(group),
                    "tracks": [g["track_name"] for g in group], **stats})
    return out


def _overlap(a: dict[str, float], b: dict[str, float]) -> float | None:
    """持仓重合度：同方向持仓取两者权重的较小值求和（0 表示完全不同，1 表示完全相同）。"""
    if not a or not b:
        return None
    total = 0.0
    for sym in set(a) & set(b):
        if a[sym] * b[sym] > 0:
            total += min(abs(a[sym]), abs(b[sym]))
    return round(total / max(sum(abs(x) for x in a.values()), sum(abs(x) for x in b.values()), 1e-9), 4)


def lookahead_risks(s: Session, track: TrackRow) -> list[dict]:
    """历史回放的前视偏差风险（PRD 3.5）：模型知识截止日期晚于回放开始日期时，模型可能在训练中见过这段行情。

    level：high（截止日期 ≥ 回放开始）、unknown（供应商未填写知识截止日期）、low（截止日期早于回放开始）。
    实时模拟没有前视偏差，返回空列表。
    """
    if track.mode != "replay" or track.start_date is None:
        return []
    out = []
    for acc in s.scalars(select(AccountRow).where(AccountRow.track_id == track.id)):
        agent = s.get(AgentRow, acc.agent_id)
        if agent.kind != "llm":
            continue
        provider = s.get(LLMProviderRow, agent.provider_id) if agent.provider_id else None
        cutoff = provider.knowledge_cutoff if provider else None
        level = "unknown" if cutoff is None else ("high" if cutoff >= track.start_date else "low")
        out.append({
            "account_id": acc.id, "agent_name": agent.name,
            "model": agent.model or (provider.default_model if provider else None),
            "cutoff": str(cutoff) if cutoff else None, "level": level, "replay_start": str(track.start_date),
            "replay_until": str(track.replay_until) if track.replay_until else None, "anonymized": track.anonymize,
        })
    return out
