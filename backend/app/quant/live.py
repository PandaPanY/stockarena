"""每日信号与实时跟踪（PRD 3.8）：策略的冠军版本作为「量化选手」在实时赛道中由交易引擎按真实规则执行。

调仓节奏与回测一致：每 every_days 个交易日调仓一次；冠军版本更新后的下一轮立即按新版本调仓；
非调仓日只检查止损 / 移动止损（收盘判断，次日开盘卖出）。
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agent.context import MarketSnapshot
from app.arena.lifecycle import ArenaError, create_track, enroll, start_track
from app.baselines import BaselineDecision
from app.core.timeutils import utcnow
from app.db.session import get_sessionmaker
from app.models import AccountRow, AgentRow, DailyBarRow, DecisionRow, RuleSetRow, TrackRow
from app.models.quant import QuantPortfolioRow, QuantStrategyRow, QuantVersionRow
from app.quant.backtest import portfolio_min_trade
from app.quant.data import get_panel
from app.quant.explain import drop_reason, pick_text, timing_text
from app.quant.explain import picks as explain_picks
from app.quant.panel import Panel
from app.quant.signal import target_weights
from app.quant.spec import StrategySpec, describe, parse_spec
from app.trading.portfolio import AccountState

logger = logging.getLogger(__name__)

# 全量行情矩阵的起点：与研究使用的默认训练开始日一致，共用缓存
_LIVE_START = date(2018, 1, 1)


def live_panel(s: Session, universe_name: str, d: date) -> tuple[Panel, int]:
    """与研究共用同一个全量行情矩阵（按数据最新日期缓存），返回 (矩阵, d 所在行)。

    因子第 t 行只用 t 日及以前的数据，所以在全量矩阵上取第 t 行与只读取到 t 日完全等价，
    这样历史回放每天不必重新读取，也与快速回测的计算方式一致。
    """
    panel = get_panel(s, universe_name, _LIVE_START, max(d, date.today()))
    return panel, panel.locate(d, "before")


def _w(x: float) -> Decimal:
    return Decimal(str(round(float(x), 4)))


def _exits(spec: StrategySpec, panel: Panel, i: int, state: AccountState) -> dict[str, str]:
    """止损 / 移动止损触发的标的 → 原因。成本价取持仓均价（含费用），高点从最近一次买入日算起。"""
    out: dict[str, str] = {}
    rk = spec.risk
    if not (rk.stop_loss or rk.trailing_stop):
        return out
    for sym, pos in state.positions.items():
        j = panel.col.get(sym)
        if j is None or pos.qty <= 0:
            continue
        close = panel.close_ff[i, j]
        if rk.stop_loss and pos.avg_cost > 0 and close / float(pos.avg_cost) - 1 <= -rk.stop_loss:
            out[sym] = f"亏损 {1 - close / float(pos.avg_cost):.1%} 触发止损"
            continue
        if rk.trailing_stop:
            k = panel.locate(pos.last_buy_date, "after") if pos.last_buy_date else max(i - 60, 0)
            peak = np.nanmax(panel.adj_close[min(k, i):i + 1, j])
            dd = panel.adj_close[i, j] / peak - 1
            if dd <= -rk.trailing_stop:
                out[sym] = f"从高点回撤 {-dd:.1%} 触发移动止损"
    return out


def decide_quant(strategy_id: int, account_id: int, state: AccountState, snap: MarketSnapshot) -> BaselineDecision:
    with get_sessionmaker()() as s:
        st = s.get(QuantStrategyRow, strategy_id)
        if st is None or not st.champion_id:
            return BaselineDecision(None, "策略不存在或没有冠军版本，本轮不调仓")
        v = s.get(QuantVersionRow, st.champion_id)
        spec = parse_spec(v.spec)
        prev = s.scalars(select(DecisionRow).where(DecisionRow.account_id == account_id)
                         .order_by(DecisionRow.id.desc()).limit(1)).first()
        n = s.scalar(select(func.count(DecisionRow.id)).where(DecisionRow.account_id == account_id)) or 0
        last_version = (prev.parsed or {}).get("quant_version") if prev else None
        last_rebalance = (prev.parsed or {}).get("since_rebalance", -1) if prev else -1
        panel, i = live_panel(s, st.universe_name, snap.data_date)
    if i < 0 or panel.dates[i] != snap.data_date:
        return BaselineDecision(None, f"缺少 {snap.data_date} 的行情，本轮不调仓")
    every = spec.rebalance.every_days
    since = last_rebalance + 1 if last_rebalance >= 0 else every
    changed = last_version is not None and last_version != v.number
    rebalance = since >= every or changed or n == 0
    exits = _exits(spec, panel, i, state)
    extra = {"quant_strategy_id": strategy_id, "quant_version": v.number}
    head = f"量化策略「{st.name}」v{v.number}（{v.title}）"

    if rebalance:
        held = np.array([state.positions.get(x) is not None and state.positions[x].qty > 0 for x in panel.symbols])
        blocked = np.array([x in exits for x in panel.symbols])
        w, info = target_weights(spec, panel, i, held=held, blocked=blocked)
        targets = {panel.symbols[j]: _w(w[j]) for j in np.where(w > 1e-4)[0]}
        names = dict(zip(panel.symbols, panel.names, strict=True))
        picks = "、".join(f"{names[x]} {float(t):.1%}" for x, t in sorted(targets.items(), key=lambda kv: -kv[1]))
        why = "冠军版本更新，按新版本调仓" if changed else ("首次建仓" if n == 0 else f"调仓日（每 {every} 个交易日）")
        lines = [f"{head}：{why}。",
                 f"可选标的 {info['eligible']} 个，选中 {len(targets)} 个：{picks or '无（空仓）'}。"]
        tt = timing_text(spec, panel, i)
        if tt:
            lines.append(f"大盘择时：{tt}。")
        # 选股理由：排名与各因子的数值、百分位；卖出原因
        chosen = [int(j) for j in np.argsort(-w) if w[j] > 1e-4]
        lines += ["选股理由："] + [f"- {pick_text(p)}" for p in explain_picks(spec, panel, i, chosen, w, blocked)
                                   if p["rank"] is not None]
        sells = [j for j in np.where(held & (w <= 1e-4))[0]]
        if sells:
            lines.append("卖出：" + "；".join(
                f"{names[panel.symbols[j]]} {exits.get(panel.symbols[j]) or drop_reason(spec, panel, i, int(j))}"
                for j in sells) + "。")
        lines.append("规则：" + "；".join(describe(spec)))
        max_w = max((float(t) for t in targets.values()), default=0.0)
        overrides = {"max_position_pct": _w(min(1.0, max_w + 0.01)) if targets else Decimal("0.3"),
                     "max_orders_per_round": len(targets) + len(state.positions) + 5,
                     "rebalance_threshold_pct": _w(spec.rebalance.min_trade_pct), "rebalance_new_free": True}
        return BaselineDecision(targets, "\n".join(lines), param_overrides=overrides,
                                extra=extra | {"since_rebalance": 0, "rebalance": True})

    extra |= {"since_rebalance": since, "rebalance": False}
    if exits:
        # 只卖出触发止损的标的：其余持仓按当前权重保持不变（不预留现金，避免微调）
        prices = {x: b.close for x, b in snap.ref_bars.items()}
        nav = state.nav(prices)
        targets = {}
        for sym, pos in state.positions.items():
            if pos.qty > 0 and sym not in exits and sym in prices:
                targets[sym] = (pos.qty * prices[sym] / nav).quantize(Decimal("0.0001"))
        text = f"{head}：非调仓日，" + "；".join(f"{k} {r}" for k, r in exits.items()) + "，次日开盘卖出。"
        return BaselineDecision(targets, text, extra=extra,
                                param_overrides={"cash_buffer_pct": Decimal("0"), "max_position_pct": Decimal("1"),
                                                 "max_orders_per_round": len(state.positions) + 5})
    text = f"{head}：非调仓日（每 {every} 个交易日调仓，距下次 {every - since} 个交易日），持仓不变。"
    return BaselineDecision(None, text, extra=extra)


# ---------------------------------------------------------------- 跟踪


def _start_follow(s: Session, user_id: int, existing_account: int | None, name: str, universe: str, market: str,
                  agent: AgentRow, initial_cash: Decimal, description: str,
                  replay_start: date | None = None) -> AccountRow:
    """创建实时跟踪赛道（量化选手 + 买入持有基准）；replay_start 给出时为回放校验赛道（历史回放后转实时）。"""
    if existing_account and replay_start is None:
        acc = s.get(AccountRow, existing_account)
        track = s.get(TrackRow, acc.track_id) if acc else None
        if track is not None and track.status in ("running", "paused"):
            raise ArenaError("已在实时跟踪中")
    rs = s.scalars(select(RuleSetRow).where(RuleSetRow.name == f"{market.lower()}_long_only")).first()
    if rs is None:
        raise ArenaError("缺少只做多规则集")
    from app.arena.deps import universe_resolver

    spec = universe_resolver().specs.get(universe)
    universe = (spec.live_universe if spec and spec.live_universe else universe)
    if replay_start is None:
        track = create_track(s, user_id, name=f"实时跟踪·{name}", universe_name=universe, rule_set_id=rs.id,
                             initial_cash=initial_cash, description=description)
    else:
        # 回放校验只重放到最近一个已收盘的交易日，之后自动结束（不转为实时运行）
        from app.arena.deps import market_service
        from app.arena.lifecycle import latest_complete_date
        from app.core.timeutils import to_market_time

        cal = market_service(s, market).calendar()
        end = latest_complete_date(cal, to_market_time(utcnow(), market))
        track = create_track(s, user_id, name=f"{REPLAY_PREFIX}{name}", universe_name=universe, rule_set_id=rs.id,
                             initial_cash=initial_cash, description=description, mode="replay",
                             replay_start=replay_start, end_date=end)
    acc = enroll(s, track, agent.id)
    bench = s.scalars(select(AgentRow).where(AgentRow.kind == "baseline", AgentRow.baseline_type == "buy_hold_index",
                                             AgentRow.archived.is_(False))).first()
    if bench is not None:
        enroll(s, track, bench.id)
    start_track(s, track, utcnow())
    from app.quant import costs

    costs.apply_to_track(track)       # 佣金与红利税按「交易成本」设置，与回测一致
    return acc


def follow(s: Session, st: QuantStrategyRow, user_id: int, initial_cash: Decimal = Decimal("1000000")) -> AccountRow:
    """为策略创建实时跟踪赛道（策略 + 买入持有基准）。"""
    agent = s.scalars(select(AgentRow).where(AgentRow.kind == "quant", AgentRow.quant_strategy_id == st.id)).first()
    if agent is None:
        agent = AgentRow(name=f"量化·{st.name}", kind="quant", quant_strategy_id=st.id, user_id=st.user_id,
                         description=f"策略工坊「{st.name}」的冠军版本（规则化策略，不调用 AI）")
        s.add(agent)
        s.flush()
    acc = _start_follow(s, user_id, st.follow_account_id, st.name, st.universe_name, st.market, agent, initial_cash,
                        f"策略工坊「{st.name}」的冠军版本实时模拟，冠军更新后自动切换。")
    st.follow_account_id = acc.id
    return acc


REPLAY_PREFIX = "回放校验·"


def replay_check(s: Session, user_id: int, *, strategy: QuantStrategyRow | None = None,
                 portfolio: QuantPortfolioRow | None = None, start: date,
                 initial_cash: Decimal = Decimal("1000000")) -> AccountRow:
    """回放校验：用交易引擎把历史行情重放一遍（与实时跟踪完全相同的执行路径），之后与快速回测对账。"""
    if strategy is not None:
        agent = s.scalars(select(AgentRow).where(AgentRow.kind == "quant",
                                                 AgentRow.quant_strategy_id == strategy.id)).first()
        if agent is None:
            agent = AgentRow(name=f"量化·{strategy.name}", kind="quant", quant_strategy_id=strategy.id,
                             user_id=strategy.user_id)
            s.add(agent)
            s.flush()
        name, universe, market = strategy.name, strategy.universe_name, strategy.market
    else:
        from app.quant.portfolio import members_of

        st0 = members_of(s, portfolio)[0].strategy
        from app.quant.review import rule_agent

        agent = rule_agent(s, portfolio.id)
        if agent is None:
            agent = AgentRow(name=f"组合·{portfolio.name}", kind="quant", quant_portfolio_id=portfolio.id,
                             user_id=portfolio.user_id)
            s.add(agent)
            s.flush()
        name, universe, market = f"组合·{portfolio.name}", st0.universe_name, st0.market
    return _start_follow(s, user_id, None, name, universe, market, agent, initial_cash,
                         f"回放校验：从 {start} 起用交易引擎重放，与快速回测对账", replay_start=start)


def follow_portfolio(s: Session, pf: QuantPortfolioRow, user_id: int,
                     initial_cash: Decimal = Decimal("1000000")) -> AccountRow:
    from app.quant.portfolio import members_of

    members = members_of(s, pf)
    if len(members) < 2:
        raise ArenaError("组合中可用的策略少于 2 个")
    st0 = members[0].strategy
    from app.quant.review import rule_agent

    agent = rule_agent(s, pf.id)
    if agent is None:
        agent = AgentRow(name=f"组合·{pf.name}", kind="quant", quant_portfolio_id=pf.id, user_id=pf.user_id,
                         description=f"多风格组合「{pf.name}」：各成员策略的冠军版本按权重分仓")
        s.add(agent)
        s.flush()
    acc = _start_follow(s, user_id, pf.follow_account_id, f"组合·{pf.name}", st0.universe_name, st0.market, agent,
                        initial_cash, f"多风格组合「{pf.name}」实时模拟，成员冠军更新后自动切换。")
    pf.follow_account_id = acc.id
    return acc


def decide_portfolio(portfolio_id: int, account_id: int, state: AccountState,
                     snap: MarketSnapshot) -> BaselineDecision:
    """组合选手：每个成员按自己的调仓周期更新「成员目标权重」，组合目标 = Σ 成员权重 × 成员目标。

    成员状态（版本、距上次调仓天数、当前目标）记录在决策里，下一轮接着用；成员规则中的止损按组合持仓的成本价检查。
    """
    from app.quant.allocation import allocate, member_returns
    from app.quant.portfolio import alloc_of, members_of

    with get_sessionmaker()() as s:
        pf = s.get(QuantPortfolioRow, portfolio_id)
        if pf is None:
            return BaselineDecision(None, "组合不存在，本轮不调仓")
        members = members_of(s, pf)
        alloc = alloc_of(pf)
        prev = s.scalars(select(DecisionRow).where(DecisionRow.account_id == account_id)
                         .order_by(DecisionRow.id.desc()).limit(1)).first()
        prev_members = (prev.parsed or {}).get("members", {}) if prev else {}
        panel, i = live_panel(s, members[0].strategy.universe_name, snap.data_date) if members else (None, -1)
    if not members or panel is None or i < 0 or panel.dates[i] != snap.data_date:
        return BaselineDecision(None, f"缺少 {snap.data_date} 的行情或组合成员，本轮不调仓")
    held = np.array([state.positions.get(x) is not None and state.positions[x].qty > 0 for x in panel.symbols])
    names = dict(zip(panel.symbols, panel.names, strict=True))
    lines = [f"多风格组合「{pf.name}」："]
    states: dict[str, dict] = {}
    changed = False
    for m in members:
        key = str(m.strategy.id)
        pm = prev_members.get(key)
        every = m.spec.rebalance.every_days
        since = pm["since"] + 1 if pm else every
        weights = dict(pm["weights"]) if pm else {}
        exits = {k: v for k, v in _exits(m.spec, panel, i, state).items() if k in weights}
        if pm is None or pm.get("version") != m.version.number or since >= every:
            blocked = np.array([x in exits for x in panel.symbols])
            w, info = target_weights(m.spec, panel, i, held=held, blocked=blocked)
            weights = {panel.symbols[j]: round(float(w[j]), 4) for j in np.where(w > 1e-4)[0]}
            since, changed = 0, True
            why = "版本更新" if pm and pm.get("version") != m.version.number else "调仓日"
            lines.append(f"- {m.strategy.name}（{m.weight:.0%}，v{m.version.number}）{why}：选中 {len(weights)} 个"
                         + ("，择时降仓" if not info["timing_on"] else ""))
        elif exits:
            for k in exits:
                weights.pop(k, None)
            changed = True
            lines.append(f"- {m.strategy.name}：" + "；".join(f"{names.get(k, k)} {r}" for k, r in exits.items()))
        states[key] = {"version": m.version.number, "since": since, "weights": weights}
    # 成员资金配置（与 backtest.PortfolioTargeter._update_alloc 相同的规则）
    budget = np.array([m.weight for m in members])
    alloc_w, scale, alloc_state = budget, 1.0, None
    if alloc.dynamic:
        ids = [m.strategy.id for m in members]
        pa = (prev.parsed or {}).get("alloc") if prev else None
        if pa and pa.get("members") == ids:
            alloc_w, scale, a_since = np.array(pa["weights"]), float(pa["scale"]), pa["since"] + 1
        else:
            a_since = alloc.every
        if a_since >= alloc.every:
            w, sc, note = allocate(member_returns([m.spec for m in members], panel), i, budget, alloc)
            if not (pa and np.allclose(w, alloc_w, atol=1e-4) and abs(sc - scale) < 1e-4):
                changed = True
            alloc_w, scale, a_since = w, sc, 0
            lines.append(f"资金配置（{alloc.describe()}）：" + "、".join(
                f"{m.strategy.name} {x:.0%}" for m, x in zip(members, alloc_w, strict=True))
                + (f"；总仓位系数 {scale:.0%}" if alloc.vol_target else "") + (f"（{note}）" if note else ""))
        alloc_state = {"members": ids, "weights": [round(float(x), 6) for x in alloc_w], "scale": round(scale, 6),
                       "since": a_since}
    extra = {"quant_portfolio_id": portfolio_id, "members": states, "rebalance": changed}
    if alloc_state:
        extra["alloc"] = alloc_state
    if not changed:
        return BaselineDecision(None, f"组合「{pf.name}」：各成员均非调仓日，持仓不变。", extra=extra)
    combined: dict[str, float] = {}
    for m, aw in zip(members, alloc_w, strict=True):
        for sym, w in states[str(m.strategy.id)]["weights"].items():
            combined[sym] = combined.get(sym, 0.0) + aw * w * scale
    targets = {k: _w(v) for k, v in combined.items() if v > 1e-4}
    max_w = max((float(t) for t in targets.values()), default=0.0)
    # 加减仓门槛与回测一致（backtest.portfolio_min_trade）；新建仓不受门槛限制
    min_trade = portfolio_min_trade([(m.spec, m.weight) for m in members])
    overrides = {"max_position_pct": _w(min(1.0, max_w + 0.01)) if targets else Decimal("0.3"),
                 "max_orders_per_round": len(targets) + len(state.positions) + 5,
                 "rebalance_threshold_pct": _w(min_trade), "rebalance_new_free": True}
    lines.append(f"组合目标：{len(targets)} 个标的，总仓位 {sum(combined.values()):.0%}。")
    return BaselineDecision(targets, "\n".join(lines), param_overrides=overrides, extra=extra)


def signal(s: Session, st: QuantStrategyRow, capital: float | None = None) -> dict:
    """按最新收盘数据计算冠军版本的目标持仓（不依赖跟踪账户）；capital 给出时换算为股数（整手）。"""
    v = s.get(QuantVersionRow, st.champion_id)
    spec = parse_spec(v.spec)
    d = s.scalar(select(func.max(DailyBarRow.date)).where(DailyBarRow.symbol == st.benchmark))
    panel, i = live_panel(s, st.universe_name, d)
    w, info = target_weights(spec, panel, i)
    rows = []
    for j in np.argsort(-w):
        if w[j] <= 1e-4:
            break
        price = float(panel.close_ff[i, j])
        row = {"symbol": panel.symbols[j], "name": panel.names[j], "weight": round(float(w[j]), 4),
               "close": price}
        if capital:
            lot = panel.lot[j]
            row["shares"] = int(capital * w[j] / price // lot * lot)
            row["amount"] = round(row["shares"] * price, 2)
        rows.append(row)
    return {"date": panel.dates[i].isoformat(), "version": v.number, "version_title": v.title,
            "timing_on": info["timing_on"], "eligible": info["eligible"], "holdings": rows,
            "cash_weight": round(1 - float(w.sum()), 4), "every_days": spec.rebalance.every_days,
            "rules": describe(spec)}
