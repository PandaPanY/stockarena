"""自选倾斜账户：组合规则账户的决策按自选权重调整后执行，与原组合在同一个赛道中并行对比。

- 只调整策略已经选中的股票：目标权重 × 自选权重（非自选股为 1），再按比例缩放回原来的总仓位；
  权重 0 = 不买也不持有；大于 1 = 多配，单只不超过原目标的 WEIGHT_MAX 倍、也不超过 MAX_SINGLE。
  超出上限的部分留作现金。不会买入策略没有选中的股票。
- 只在规则账户调仓的日子调整（与规则同一节奏），权重改动在下一次调仓时生效。
- 第一次决策复制规则账户当前的目标（按当时的权重调整）。
- 现有的规则账户、AI 复核账户不受任何影响（AI 复核的有效性评估照常进行）。

自选权重来自用户当下的偏好，无法做历史回测（拿今天的偏好去回测历史就是用了后见之明），只能实时对比。
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.context import MarketSnapshot
from app.baselines import BaselineDecision
from app.db.session import get_sessionmaker
from app.models import AccountRow, AgentRow, TrackRow
from app.models.quant import QuantPortfolioRow
from app.trading.portfolio import AccountState

logger = logging.getLogger(__name__)

WEIGHT_MAX = 3.0
MAX_SINGLE = 0.10


def is_tilt_agent(agent: AgentRow) -> bool:
    return bool((agent.params or {}).get("watch_tilt"))


def settings(pf: QuantPortfolioRow) -> dict:
    return dict((pf.settings or {}).get("tilt") or {})


def enable(s: Session, pf: QuantPortfolioRow) -> AccountRow:
    """为跟踪中的组合开启自选倾斜：在规则账户所在赛道中加入一个倾斜账户（本金相同）。"""
    from app.arena.lifecycle import ArenaError
    from app.core.timeutils import utcnow

    if not pf.follow_account_id:
        raise ArenaError("请先开始实时跟踪（倾斜账户与规则账户在同一个赛道中并行）")
    rule_acc = s.get(AccountRow, pf.follow_account_id)
    track = s.get(TrackRow, rule_acc.track_id) if rule_acc else None
    if track is None or track.status not in ("running", "paused"):
        raise ArenaError("规则账户的实时跟踪没有在运行")
    cur = settings(pf)
    if cur.get("enabled") and cur.get("account_id"):
        acc = s.get(AccountRow, cur["account_id"])
        if acc is not None and acc.status == "active":
            raise ArenaError("已开启自选倾斜")
    agent = next((a for a in s.scalars(select(AgentRow).where(AgentRow.kind == "quant",
                                                              AgentRow.quant_portfolio_id == pf.id))
                  if is_tilt_agent(a)), None)
    if agent is None:
        agent = AgentRow(name=f"组合·{pf.name}·自选倾斜", kind="quant", quant_portfolio_id=pf.id, user_id=pf.user_id,
                         params={"watch_tilt": True},
                         description=f"多风格组合「{pf.name}」按自选权重调整已选中股票的仓位，与规则账户同步调仓")
        s.add(agent)
        s.flush()
    # 与 AI 复核账户相同：有意设计的孪生账户对照，由这里直接创建
    acc = s.scalars(select(AccountRow).where(AccountRow.track_id == track.id, AccountRow.agent_id == agent.id)).first()
    if acc is None:
        acc = AccountRow(track_id=track.id, agent_id=agent.id, cash=track.initial_cash, status="active")
        s.add(acc)
        s.flush()
    else:
        acc.status = "active"
    pf.settings = {**(pf.settings or {}), "tilt": {"enabled": True, "account_id": acc.id, "agent_id": agent.id,
                                                   "since": cur.get("since") or utcnow().date().isoformat()}}
    return acc


def disable(s: Session, pf: QuantPortfolioRow) -> None:
    cur = settings(pf)
    acc = s.get(AccountRow, cur["account_id"]) if cur.get("account_id") else None
    if acc is not None:
        acc.status = "stopped"
    pf.settings = {**(pf.settings or {}), "tilt": {**cur, "enabled": False}}


def apply_tilt(targets: dict[str, Decimal], weights: dict[str, float]) -> tuple[dict[str, Decimal], list[dict]]:
    """按自选权重调整目标权重。返回（新目标，调整明细）。"""
    base = {k: float(v) for k, v in targets.items()}
    total = sum(v for v in base.values() if v > 0)
    raw = {k: v * min(max(weights.get(k, 1.0), 0.0), WEIGHT_MAX) if v > 0 else v for k, v in base.items()}
    raw_total = sum(v for v in raw.values() if v > 0)
    scale = total / raw_total if raw_total > 0 else 0.0
    out: dict[str, Decimal] = {}
    changes = []
    for k, v in base.items():
        if v <= 0:
            out[k] = targets[k]
            continue
        new = raw[k] * scale
        new = min(new, v * WEIGHT_MAX, max(MAX_SINGLE, v))      # 原目标已超过单只上限时不再压低
        out[k] = Decimal(str(round(new, 4)))
        if k in weights or abs(new - v) > 1e-4:
            changes.append({"symbol": k, "weight": weights.get(k, 1.0), "rule": round(v, 4), "tilted": round(new, 4)})
    return out, changes


async def decide_tilted(portfolio_id: int, account_id: int, state: AccountState,
                        snap: MarketSnapshot) -> BaselineDecision:
    """自选倾斜账户的决策：规则账户的决策按自选权重调整。"""
    from app.quant.live import decide_portfolio
    from app.quant.review import _rule_context
    from app.watch import service as watch

    rule_id, rule_state, last_targets, first = await asyncio.to_thread(_rule_context, portfolio_id, account_id)
    if rule_id is None:
        return BaselineDecision(None, "组合没有在实时跟踪，倾斜账户本轮不调仓")
    rule = await asyncio.to_thread(decide_portfolio, portfolio_id, rule_id, rule_state, snap)
    targets = rule.targets
    sync = False
    if targets is None and first and not state.positions and last_targets:
        targets = {k: Decimal(str(v)) for k, v in last_targets.items()}
        sync = True
    if targets is None:
        return BaselineDecision(None, rule.analysis, extra={**rule.extra, "tilt": {"skipped": "非调仓日"}})
    with get_sessionmaker()() as s:
        pf = s.get(QuantPortfolioRow, portfolio_id)
        weights = watch.weights(s, pf.user_id if pf else None)     # 组合所有者的自选权重
    tilted, changes = apply_tilt(targets, weights)
    moved = [c for c in changes if abs(c["tilted"] - c["rule"]) > 1e-4]
    head = (f"自选倾斜账户首次建仓：复制规则账户当前的 {len(targets)} 个目标持仓。" if sync else rule.analysis)
    note = (f"自选倾斜：目标中有 {sum(1 for c in changes if c['symbol'] in weights)} 只自选股，"
            f"调整了 {len(moved)} 只的仓位" + ("；" + "；".join(
                f"{c['symbol']} {c['rule']:.2%}→{c['tilted']:.2%}（权重 {c['weight']:g}）" for c in moved[:10])
                if moved else ""))
    return BaselineDecision(tilted, f"{head}\n{note}", param_overrides=rule.param_overrides,
                            extra={**rule.extra, "tilt": {"sync": sync, "weights": weights, "changes": changes}})


def report(s: Session, pf: QuantPortfolioRow) -> dict:
    """倾斜账户与规则账户的净值对比、当前自选权重对目标的影响。"""
    from app.models import NavSnapshotRow
    from app.watch import service as watch

    cfg = settings(pf)
    out: dict = {"enabled": bool(cfg.get("enabled")), "settings": cfg,
                 "weights": [{"symbol": r.symbol, "name": r.name, "weight": r.weight}
                             for r in watch.items(s, pf.user_id) if r.weight != 1]}
    if not cfg.get("account_id") or not pf.follow_account_id:
        return out

    def navs(acc_id: int) -> dict[str, float]:
        return {d.isoformat(): float(n) for d, n in s.execute(
            select(NavSnapshotRow.date, NavSnapshotRow.nav).where(NavSnapshotRow.account_id == acc_id)
            .order_by(NavSnapshotRow.date))}

    rn, tn = navs(pf.follow_account_id), navs(cfg["account_id"])
    common = sorted(set(rn) & set(tn))
    if common:
        r0, t0 = rn[common[0]], tn[common[0]]
        out["curve"] = [{"date": d, "rule": round(rn[d] / r0, 6), "tilt": round(tn[d] / t0, 6)} for d in common]
        out["rule_return"] = round(rn[common[-1]] / r0 - 1, 6)
        out["tilt_return"] = round(tn[common[-1]] / t0 - 1, 6)
        out["since"] = common[0]
    from app.models import DecisionRow, RoundRow

    last = s.execute(select(DecisionRow, RoundRow).join(RoundRow, DecisionRow.round_id == RoundRow.id)
                     .where(DecisionRow.account_id == cfg["account_id"])
                     .order_by(RoundRow.data_date.desc()).limit(30)).all()
    for dec, rnd in last:
        t = (dec.parsed or {}).get("tilt") or {}
        if t.get("changes") is not None:
            out["last_changes"] = {"date": rnd.data_date.isoformat(), "changes": t["changes"]}
            break
    return out
