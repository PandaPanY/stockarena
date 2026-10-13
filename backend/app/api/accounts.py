"""参赛账户与决策记录接口。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.reflection import MAX_STRATEGY_CHARS
from app.api.deps import current_user_id
from app.arena import queries, strategy, tasks
from app.db.session import get_db
from app.models import (
    AccountRow,
    AgentRow,
    CashLedgerRow,
    DecisionRow,
    FillRow,
    NavSnapshotRow,
    OrderRow,
    ReflectionRow,
    RoundRow,
    StrategyVersionRow,
    TrackRow,
)

router = APIRouter(tags=["accounts"])


def _account(db: Session, account_id: int, user_id: int) -> tuple[AccountRow, TrackRow]:
    acc = db.get(AccountRow, account_id)
    track = db.get(TrackRow, acc.track_id) if acc else None
    if acc is None or track is None or track.user_id != user_id:
        raise HTTPException(404, "账户不存在")
    return acc, track


@router.get("/api/accounts/{account_id}")
def account(account_id: int, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> dict:
    acc, track = _account(db, account_id, user_id)
    agent = db.get(AgentRow, acc.agent_id)
    return {
        "id": acc.id, "track_id": track.id, "track_name": track.name, "currency": track.currency,
        "initial_cash": format(track.initial_cash, "f"), "cash": format(acc.cash, "f"),
        "agent": {"id": agent.id, "name": agent.name, "kind": agent.kind, "model": agent.model,
                  "baseline_type": agent.baseline_type, "description": agent.description},
        "metrics": queries.account_metrics(db, acc, track),
        "margin": queries.margin_info(db, acc, track),
    }


@router.get("/api/accounts/{account_id}/positions")
def positions(account_id: int, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> list[dict]:
    acc, track = _account(db, account_id, user_id)
    return queries.positions(db, acc, track)


@router.get("/api/accounts/{account_id}/orders")
def orders(account_id: int, limit: int = 200, db: Session = Depends(get_db),
           user_id: int = Depends(current_user_id)) -> list[dict]:
    _account(db, account_id, user_id)
    return [queries.order_dict(o) for o in db.scalars(
        select(OrderRow).where(OrderRow.account_id == account_id).order_by(OrderRow.id.desc()).limit(min(limit, 1000)))]


@router.get("/api/accounts/{account_id}/fills")
def fills(account_id: int, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> list[dict]:
    _account(db, account_id, user_id)
    return [queries.fill_dict(f) for f in db.scalars(
        select(FillRow).where(FillRow.account_id == account_id).order_by(FillRow.id.desc()))]


@router.get("/api/accounts/{account_id}/nav")
def nav(account_id: int, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> list[dict]:
    _account(db, account_id, user_id)
    return [{"date": str(n.date), "nav": format(n.nav, "f"), "cash": format(n.cash, "f"),
             "market_value": format(n.market_value, "f"),
             "benchmark_close": format(n.benchmark_close, "f") if n.benchmark_close is not None else None}
            for n in db.scalars(select(NavSnapshotRow).where(NavSnapshotRow.account_id == account_id)
                                .order_by(NavSnapshotRow.date))]


@router.get("/api/accounts/{account_id}/ledger")
def ledger(account_id: int, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> list[dict]:
    _account(db, account_id, user_id)
    return [{"id": e.id, "trade_date": str(e.trade_date), "type": e.type, "amount": format(e.amount, "f"),
             "balance_after": format(e.balance_after, "f"), "symbol": e.symbol, "note": e.note}
            for e in db.scalars(select(CashLedgerRow).where(CashLedgerRow.account_id == account_id)
                                .order_by(CashLedgerRow.id.desc()))]


@router.get("/api/accounts/{account_id}/kline")
def kline(account_id: int, symbol: str, days: int = 180, db: Session = Depends(get_db),
          user_id: int = Depends(current_user_id)) -> dict:
    _account(db, account_id, user_id)
    return queries.kline(db, account_id, symbol, min(days, 1000))


@router.get("/api/accounts/{account_id}/symbols")
def traded_symbols(account_id: int, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> list:
    """该账户交易过的标的（K 线页的下拉选项）。"""
    _account(db, account_id, user_id)
    return sorted(set(db.scalars(select(FillRow.symbol).where(FillRow.account_id == account_id))))


@router.get("/api/decisions")
def list_decisions(track_id: int | None = None, account_id: int | None = None, status: str | None = None,
                   limit: int = 50, offset: int = 0, db: Session = Depends(get_db),
                   user_id: int = Depends(current_user_id)) -> dict:
    q = (select(DecisionRow, RoundRow, AgentRow)
         .join(RoundRow, DecisionRow.round_id == RoundRow.id)
         .join(AccountRow, DecisionRow.account_id == AccountRow.id)
         .join(AgentRow, AccountRow.agent_id == AgentRow.id)
         .join(TrackRow, RoundRow.track_id == TrackRow.id)
         .where(TrackRow.user_id == user_id))
    if track_id:
        q = q.where(RoundRow.track_id == track_id)
    if account_id:
        q = q.where(DecisionRow.account_id == account_id)
    if status:
        q = q.where(DecisionRow.status == status)
    rows = db.execute(q.order_by(DecisionRow.id.desc()).offset(offset).limit(min(limit, 200) + 1)).all()
    more = len(rows) > min(limit, 200)
    return {"items": [queries.decision_summary(db, d, r, a) for d, r, a in rows[:min(limit, 200)]], "more": more}


@router.get("/api/decisions/{decision_id}")
def get_decision(decision_id: int, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> dict:
    d = db.get(DecisionRow, decision_id)
    if d is None:
        raise HTTPException(404, "决策不存在")
    acc, track = _account(db, d.account_id, user_id)
    rnd = db.get(RoundRow, d.round_id)
    agent = db.get(AgentRow, acc.agent_id)
    orders = list(db.scalars(select(OrderRow).where(OrderRow.decision_id == d.id)))
    order_items = []
    for o in orders:
        item = queries.order_dict(o)
        item["fills"] = [queries.fill_dict(f) for f in db.scalars(select(FillRow).where(FillRow.order_id == o.id))]
        order_items.append(item)
    return {
        **queries.decision_summary(db, d, rnd, agent), "track_id": track.id, "track_name": track.name,
        "template": f"{d.template_name} v{d.template_version}" if d.template_name else None,
        "strategy_version": d.strategy_version,
        "prompt_system": d.prompt_system, "prompt_user": d.prompt_user, "raw_response": d.raw_response,
        "reasoning": d.reasoning, "parsed": d.parsed, "notes": d.notes, "attempts": d.attempts,
        "snapshot_hash": rnd.snapshot_hash, "order_items": order_items, "tool_trace": d.tool_trace or [],
    }


# ---------------------------------------------------------------- 策略笔记与复盘


class StrategyIn(BaseModel):
    content: str = Field(min_length=1, max_length=MAX_STRATEGY_CHARS)


class RollbackIn(BaseModel):
    version: int


def _llm_account(db: Session, account_id: int, user_id: int) -> tuple[AccountRow, TrackRow, AgentRow]:
    acc, track = _account(db, account_id, user_id)
    agent = db.get(AgentRow, acc.agent_id)
    if agent.kind != "llm":
        raise HTTPException(400, "基线选手没有策略笔记")
    return acc, track, agent


@router.get("/api/accounts/{account_id}/strategy")
def account_strategy(account_id: int, db: Session = Depends(get_db),
                     user_id: int = Depends(current_user_id)) -> dict:
    acc, _ = _account(db, account_id, user_id)
    agent = db.get(AgentRow, acc.agent_id)
    versions = db.scalars(select(StrategyVersionRow).where(StrategyVersionRow.account_id == acc.id)
                          .order_by(StrategyVersionRow.version.desc()))
    reflections = db.scalars(select(ReflectionRow).where(ReflectionRow.account_id == acc.id)
                             .order_by(ReflectionRow.id.desc()))
    status = tasks.reflect_status.get(acc.id, {})
    return {
        "kind": agent.kind, "reflect_enabled": agent.reflect_enabled, "reflect_every": agent.reflect_every,
        "max_chars": MAX_STRATEGY_CHARS, "reflecting": bool(status.get("running")),
        "last_message": status.get("message"),
        "versions": [queries.strategy_version_dict(v) for v in versions],
        "reflections": [queries.reflection_summary(r) for r in reflections],
    }


@router.put("/api/accounts/{account_id}/strategy")
def edit_strategy(account_id: int, body: StrategyIn, db: Session = Depends(get_db),
                  user_id: int = Depends(current_user_id)) -> dict:
    acc, _, _ = _llm_account(db, account_id, user_id)
    try:
        v = strategy.add_version(db, acc.id, body.content, "manual", "手动编辑")
    except strategy.StrategyError as e:
        raise HTTPException(400, str(e)) from e
    db.commit()
    return queries.strategy_version_dict(v)


@router.post("/api/accounts/{account_id}/strategy/rollback")
def rollback_strategy(account_id: int, body: RollbackIn, db: Session = Depends(get_db),
                      user_id: int = Depends(current_user_id)) -> dict:
    acc, _, _ = _llm_account(db, account_id, user_id)
    try:
        v = strategy.rollback(db, acc.id, body.version)
    except strategy.StrategyError as e:
        raise HTTPException(400, str(e)) from e
    db.commit()
    return queries.strategy_version_dict(v)


@router.post("/api/accounts/{account_id}/reflect")
async def reflect(account_id: int, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> dict:
    acc, track, _ = _llm_account(db, account_id, user_id)
    if track.last_settled_date is None:
        raise HTTPException(400, "赛道还没有结算过的交易日，暂时无法复盘")
    if not tasks.spawn_reflect(track.id, acc.id):
        raise HTTPException(409, "该选手正在复盘")
    return {"ok": True, "message": "已开始复盘，完成后会生成新的策略版本"}


@router.get("/api/reflections/{reflection_id}")
def get_reflection(reflection_id: int, db: Session = Depends(get_db),
                   user_id: int = Depends(current_user_id)) -> dict:
    r = db.get(ReflectionRow, reflection_id)
    if r is None:
        raise HTTPException(404, "复盘记录不存在")
    _account(db, r.account_id, user_id)
    return queries.reflection_detail(r)


@router.get("/api/compare")
def compare(account_ids: str, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> dict:
    """account_ids：逗号分隔的 2～5 个账户编号（可以来自不同赛道）。"""
    try:
        ids = list(dict.fromkeys(int(x) for x in account_ids.split(",") if x.strip()))
    except ValueError as e:
        raise HTTPException(400, "账户编号格式错误") from e
    if not 2 <= len(ids) <= 5:
        raise HTTPException(400, "请选择 2～5 个选手进行对比")
    return queries.compare(db, [_account(db, i, user_id) for i in ids])
