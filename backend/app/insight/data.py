"""AI 洞察用到的数据：跟踪账户的表现、持仓与收益贡献、交易，以及某只股票为什么在 / 不在目标持仓里。

只读，按用户隔离：账户必须属于该用户正在跟踪的策略或组合（followed）。金额单位为元，收益为小数。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    AccountRow,
    AgentRow,
    CorporateActionRow,
    DailyBarRow,
    DecisionRow,
    FillRow,
    InstrumentRow,
    NavSnapshotRow,
    OrderRow,
    PositionRow,
    RoundRow,
    TrackRow,
)
from app.models.quant import QuantPortfolioRow, QuantStrategyRow, QuantVersionRow

SIDE = {"buy": "买入", "sell": "卖出", "short": "卖空", "cover": "平空"}


class InsightError(ValueError):
    pass


def _r(x: float | None, n: int = 4) -> float | None:
    return None if x is None else round(float(x), n)


def followed(s: Session, uid: int) -> list[dict]:
    """该用户正在实时跟踪的策略与组合。"""
    out = []
    for kind, model in (("portfolio", QuantPortfolioRow), ("strategy", QuantStrategyRow)):
        for row in s.scalars(select(model).where(model.user_id == uid, model.status == "active",
                                                 model.follow_account_id.is_not(None)).order_by(model.id)):
            acc = s.get(AccountRow, row.follow_account_id)
            track = s.get(TrackRow, acc.track_id) if acc else None
            if acc is None or track is None or track.status not in ("running", "paused"):
                continue
            rd = row.readiness or {}
            out.append({"kind": kind, "id": row.id, "name": row.name, "account_id": acc.id,
                        "readiness": rd.get("status"), "health": (rd.get("health") or {}).get("status")})
    return out


def owned_account(s: Session, uid: int, account_id: int) -> dict:
    """账户必须是该用户跟踪中的策略或组合的账户；返回 followed 里的那一项。"""
    for f in followed(s, uid):
        if f["account_id"] == account_id:
            return f
    raise InsightError(f"账户 {account_id} 不是你正在跟踪的策略或组合")


def _names(s: Session, symbols) -> dict[str, str]:
    symbols = list(set(symbols))
    if not symbols:
        return {}
    return {r.symbol: r.name for r in s.scalars(select(InstrumentRow).where(InstrumentRow.symbol.in_(symbols)))}


def _close_on(s: Session, symbol: str, d: date) -> Decimal | None:
    """d（含）之前最后一个收盘价（不复权）。"""
    return s.scalar(select(DailyBarRow.close).where(DailyBarRow.symbol == symbol, DailyBarRow.date <= d)
                    .order_by(DailyBarRow.date.desc()).limit(1))


def navs(s: Session, account_id: int) -> list[tuple[date, float, float | None]]:
    rows = s.execute(select(NavSnapshotRow.date, NavSnapshotRow.nav, NavSnapshotRow.benchmark_close)
                     .where(NavSnapshotRow.account_id == account_id).order_by(NavSnapshotRow.date)).all()
    return [(d, float(n), float(b) if b is not None else None) for d, n, b in rows]


def performance(s: Session, account_id: int, days: int | None = None) -> dict:
    """最近 days 个交易日（None 为跟踪以来）的收益、同期基准收益、期间最大回撤。"""
    series = navs(s, account_id)
    if len(series) < 2:
        return {"days": 0, "message": "跟踪还不到两个交易日，没有收益数据"}
    seg = series[-(days + 1):] if days else series
    (d0, n0, b0), (d1, n1, b1) = seg[0], seg[-1]
    peak, mdd = 0.0, 0.0
    for _, n, _ in seg:
        peak = max(peak, n)
        mdd = max(mdd, 1 - n / peak)
    return {"start": d0.isoformat(), "end": d1.isoformat(), "days": len(seg) - 1, "nav": round(n1, 2),
            "return": _r(n1 / n0 - 1), "benchmark_return": _r(b1 / b0 - 1) if b0 and b1 else None,
            "max_drawdown": _r(mdd), "since_start_return": _r(series[-1][1] / series[0][1] - 1),
            "tracking_start": series[0][0].isoformat()}


def _target_owners(s: Session, account_id: int) -> dict[str, list[str]]:
    """最近一次决策中，每个标的由哪些成员策略选中（组合）。"""
    d = s.scalars(select(DecisionRow).where(DecisionRow.account_id == account_id)
                  .order_by(DecisionRow.id.desc())).first()
    members = ((d.parsed or {}).get("members") or {}) if d else {}
    out: dict[str, list[str]] = {}
    for sid, m in members.items():
        st = s.get(QuantStrategyRow, int(sid))
        for sym, w in (m.get("weights") or {}).items():
            if w and st is not None:
                out.setdefault(sym, []).append(st.name)
    return out


def holdings(s: Session, account_id: int, limit: int = 40) -> dict:
    """当前持仓：数量、成本、最新收盘、市值、占净值比例、浮动盈亏、由哪些成员选中。"""
    acc = s.get(AccountRow, account_id)
    pos = list(s.scalars(select(PositionRow).where(PositionRow.account_id == account_id, PositionRow.qty != 0)))
    series = navs(s, account_id)
    nav = series[-1][1] if series else float(acc.cash)
    names = _names(s, [p.symbol for p in pos])
    owners = _target_owners(s, account_id)
    rows = []
    for p in pos:
        close = _close_on(s, p.symbol, date.today())
        mv = float(close or 0) * p.qty
        rows.append({"symbol": p.symbol, "name": names.get(p.symbol, ""), "qty": p.qty,
                     "avg_cost": _r(p.avg_cost, 3), "close": _r(close, 3), "value": round(mv, 2),
                     "weight": _r(mv / nav if nav else None),
                     "pnl": _r(float(close) / float(p.avg_cost) - 1) if close and p.avg_cost else None,
                     "since": p.opened_date.isoformat() if p.opened_date else None,
                     "chosen_by": owners.get(p.symbol, [])})
    rows.sort(key=lambda r: -r["value"])
    return {"as_of": series[-1][0].isoformat() if series else None, "nav": round(nav, 2),
            "cash": round(float(acc.cash), 2), "cash_weight": _r(float(acc.cash) / nav if nav else None),
            "count": len(rows), "positions": rows[:limit]}


def trades(s: Session, account_id: int, start: date, end: date, limit: int = 30) -> dict:
    """期间（成交日在 (start, end]）的成交，按金额从大到小。"""
    fills = list(s.scalars(select(FillRow).where(FillRow.account_id == account_id, FillRow.trade_date > start,
                                                 FillRow.trade_date <= end)))
    names = _names(s, [f.symbol for f in fills])
    rows = sorted(({"date": f.trade_date.isoformat(), "symbol": f.symbol, "name": names.get(f.symbol, ""),
                    "side": SIDE.get(f.side, f.side), "qty": f.qty, "price": _r(f.price, 3),
                    "amount": round(float(f.amount), 2)} for f in fills), key=lambda r: -r["amount"])
    return {"count": len(rows), "buy_amount": round(sum(r["amount"] for r in rows if r["side"] == "买入"), 2),
            "sell_amount": round(sum(r["amount"] for r in rows if r["side"] == "卖出"), 2), "fills": rows[:limit]}


def contributions(s: Session, account_id: int, start: date, end: date, top: int = 5) -> dict:
    """期间各标的对净值的贡献（近似）：期末市值 − 期初市值 − 买入金额 + 卖出金额，除以期初净值。

    期初持仓由当前持仓倒推（减去期间的成交），送转股按除权事件折算；不含分红，忽略手续费。"""
    series = [x for x in navs(s, account_id) if x[0] <= end]
    base = next((n for d, n, _ in reversed(series) if d <= start), None)
    if not base:
        return {"top": [], "bottom": []}
    qty_end = {p.symbol: p.qty for p in s.scalars(select(PositionRow).where(PositionRow.account_id == account_id))}
    later = s.scalars(select(FillRow).where(FillRow.account_id == account_id, FillRow.trade_date > end))
    for f in later:
        qty_end[f.symbol] = qty_end.get(f.symbol, 0) + (-f.qty if f.side == "buy" else f.qty)
    qty_start = dict(qty_end)
    flow: dict[str, float] = {}
    for f in s.scalars(select(FillRow).where(FillRow.account_id == account_id, FillRow.trade_date > start,
                                             FillRow.trade_date <= end)):
        buy = f.side == "buy"
        qty_start[f.symbol] = qty_start.get(f.symbol, 0) + (-f.qty if buy else f.qty)
        flow[f.symbol] = flow.get(f.symbol, 0.0) + (-float(f.amount) if buy else float(f.amount))
    for a in s.scalars(select(CorporateActionRow).where(CorporateActionRow.symbol.in_(list(qty_start)),
                                                        CorporateActionRow.ex_date > start,
                                                        CorporateActionRow.ex_date <= end)):
        if a.shares_per_share and qty_start.get(a.symbol):
            qty_start[a.symbol] = round(qty_start[a.symbol] / (1 + float(a.shares_per_share)))
    pnl = {}
    for sym in set(qty_start) | set(flow):
        q0, q1 = qty_start.get(sym, 0), qty_end.get(sym, 0)
        if not (q0 or q1 or flow.get(sym)):
            continue
        p0, p1 = _close_on(s, sym, start), _close_on(s, sym, end)
        v = q1 * float(p1 or 0) - q0 * float(p0 or 0) + flow.get(sym, 0.0)
        pnl[sym] = v / base
    names = _names(s, pnl)
    ranked = sorted(pnl.items(), key=lambda kv: -kv[1])
    fmt = [{"symbol": k, "name": names.get(k, ""), "contribution": _r(v)} for k, v in ranked]
    return {"top": [x for x in fmt[:top] if x["contribution"] > 0],
            "bottom": [x for x in reversed(fmt[-top:]) if x["contribution"] < 0]}


def recent_orders(s: Session, account_id: int, limit: int = 30) -> list[dict]:
    rows = list(s.scalars(select(OrderRow).where(OrderRow.account_id == account_id)
                          .order_by(OrderRow.id.desc()).limit(limit)))
    names = _names(s, [o.symbol for o in rows])
    return [{"placed": o.placed_date.isoformat(), "symbol": o.symbol, "name": names.get(o.symbol, ""),
             "side": SIDE.get(o.side, o.side), "qty": o.qty, "filled": o.filled_qty, "status": o.status,
             "reason": o.reason, "note": o.note} for o in rows]


def members(s: Session, ref: dict) -> list[dict]:
    """组合的成员与资金权重；单个策略返回它自己。"""
    if ref["kind"] == "portfolio":
        pf = s.get(QuantPortfolioRow, ref["id"])
        out = []
        for m in pf.members:
            st = s.get(QuantStrategyRow, int(m["strategy_id"]))
            if st is not None:
                out.append({"strategy_id": st.id, "name": st.name, "style": st.style, "weight": m["weight"]})
        return out
    st = s.get(QuantStrategyRow, ref["id"])
    return [{"strategy_id": st.id, "name": st.name, "style": st.style, "weight": 1.0}]


def explain_symbol(s: Session, account_id: int, symbol: str) -> dict:
    """某只股票为什么在 / 不在目标持仓里：最近一次决策时各成员策略对它的判断（排名与因子，或没入选的原因），
    以及最近与它有关的订单。"""
    from app.quant.explain import drop_reason, pick_text, picks
    from app.quant.live import live_panel
    from app.quant.signal import target_weights
    from app.quant.spec import parse_spec

    acc = s.get(AccountRow, account_id)
    agent = s.get(AgentRow, acc.agent_id)
    row = s.execute(select(DecisionRow, RoundRow).join(RoundRow, DecisionRow.round_id == RoundRow.id)
                    .where(DecisionRow.account_id == account_id).order_by(RoundRow.data_date.desc())).first()
    if row is None:
        raise InsightError("这个账户还没有决策记录")
    decision, rnd = row
    parsed = decision.parsed or {}
    if agent.quant_portfolio_id:
        versions = {int(k): v.get("version") for k, v in (parsed.get("members") or {}).items()}
    else:
        versions = {agent.quant_strategy_id: parsed.get("quant_version")}
    held = s.scalar(select(func.sum(PositionRow.qty)).where(PositionRow.account_id == account_id,
                                                            PositionRow.symbol == symbol)) or 0
    out = {"symbol": symbol, "name": _names(s, [symbol]).get(symbol, ""), "decision_date": rnd.data_date.isoformat(),
           "held_qty": int(held), "members": [],
           "orders": [o for o in recent_orders(s, account_id, 200) if o["symbol"] == symbol][:5]}
    for sid, num in versions.items():
        st = s.get(QuantStrategyRow, sid)
        v = s.scalars(select(QuantVersionRow).where(QuantVersionRow.strategy_id == sid,
                                                    QuantVersionRow.number == num)).first() if st else None
        if v is None:
            continue
        spec = parse_spec(v.spec)
        panel, i = live_panel(s, st.universe_name, rnd.data_date)
        if symbol not in panel.symbols:
            out["members"].append({"strategy": st.name, "verdict": "不在这个策略的股票池里"})
            continue
        j = panel.symbols.index(symbol)
        w, _ = target_weights(spec, panel, i)
        if w[j] > 1e-4:
            p = picks(spec, panel, i, [j], w)[0]
            out["members"].append({"strategy": f"{st.name} v{num}", "verdict": "入选",
                                   "detail": pick_text(p), "weight_in_member": _r(w[j])})
        else:
            out["members"].append({"strategy": f"{st.name} v{num}", "verdict": "没有入选",
                                   "detail": drop_reason(spec, panel, i, j)})
    return out
