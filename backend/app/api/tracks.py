"""赛道接口。"""

from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_user_id
from app.arena import cycle, export, lifecycle, queries, tasks
from app.core.timeutils import utcnow
from app.db.session import get_db, get_sessionmaker
from app.market.universe import BENCHMARK_NAMES
from app.models import AccountRow, AgentRow, EventRow, LLMProviderRow, RoundRow, RuleSetRow, TrackRow

router = APIRouter(prefix="/api/tracks", tags=["tracks"])

STATUS_LABELS = {"draft": "未启动", "running": "运行中", "paused": "已暂停", "finished": "已结束"}


class TrackIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    universe_name: str
    rule_set_id: int
    initial_cash: Decimal = Field(gt=0)
    output_mode: str = "targets"
    decision_timeout_s: int = Field(900, ge=30, le=7200)
    end_date: date | None = None
    description: str = ""
    agent_ids: list[int] = Field(default_factory=list)
    mode: str = "live"
    replay_start: date | None = None
    frequency: str = "daily"
    interval_min: int = 15
    event_move_pct: Decimal | None = None
    event_max_per_day: int = Field(5, ge=0, le=20)
    decision_mode: str = "snapshot"
    max_tool_calls: int = Field(10, ge=1, le=30)
    data_options: dict = Field(default_factory=dict)
    anonymize: bool = False


class EnrollIn(BaseModel):
    agent_id: int


class RunIn(BaseModel):
    force_round: bool = False


def _get(db: Session, track_id: int, user_id: int) -> TrackRow:
    t = db.get(TrackRow, track_id)
    if t is None or t.user_id != user_id:
        raise HTTPException(404, "赛道不存在")
    return t


def track_dict(db: Session, t: TrackRow, with_accounts: bool = False) -> dict:
    rs = db.get(RuleSetRow, t.rule_set_id)
    accounts = list(db.scalars(select(AccountRow).where(AccountRow.track_id == t.id)))
    out = {
        "id": t.id, "name": t.name, "market": t.market, "currency": t.currency, "universe_name": t.universe_name,
        "symbol_count": len(t.symbols), "benchmark_symbol": t.benchmark_symbol,
        "benchmark_name": BENCHMARK_NAMES.get(t.benchmark_symbol, t.benchmark_symbol),
        "rule_set_id": t.rule_set_id, "rule_set_title": rs.title if rs else None,
        "initial_cash": format(t.initial_cash, "f"), "output_mode": t.output_mode,
        "decision_timeout_s": t.decision_timeout_s, "status": t.status, "status_label": STATUS_LABELS[t.status],
        "start_date": str(t.start_date) if t.start_date else None, "end_date": str(t.end_date) if t.end_date else None,
        "last_settled_date": str(t.last_settled_date) if t.last_settled_date else None,
        "description": t.description, "account_count": len(accounts), "busy": tasks.is_busy(t.id),
        "mode": t.mode, "replay_until": str(t.replay_until) if t.replay_until else None,
        "frequency": t.frequency, "interval_min": t.interval_min,
        "event_move_pct": float(t.event_move_pct) if t.event_move_pct is not None else None,
        "event_max_per_day": t.event_max_per_day,
        "decision_mode": t.decision_mode, "max_tool_calls": t.max_tool_calls, "data_options": t.data_options or {},
        "anonymize": t.anonymize, "lookahead": queries.lookahead_risks(db, t),
        "replay_pending": cycle.replay_pending(t),
        "last_run": tasks.last_result.get(t.id), "created_at": t.created_at.isoformat(),
    }
    if with_accounts:
        out["accounts"] = [{"id": a.id, "agent_id": a.agent_id, "agent_name": db.get(AgentRow, a.agent_id).name,
                            "agent_kind": db.get(AgentRow, a.agent_id).kind} for a in accounts]
        out["rule_params"] = t.rule_params or (rs.params if rs else {})
        out["symbols"] = t.symbols
    return out


@router.get("")
def list_tracks(db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> list[dict]:
    rows = db.scalars(select(TrackRow).where(TrackRow.user_id == user_id).order_by(TrackRow.id.desc()))
    out = []
    for t in rows:
        d = track_dict(db, t)
        board = queries.leaderboard(db, t) if t.status != "draft" else []
        d["leader"] = board[0] if board else None
        d["ranking"] = [{"name": r["agent_name"], "kind": r["kind"], "cumulative_return": r.get("cumulative_return")}
                        for r in board]
        out.append(d)
    return out


@router.post("")
def create_track(body: TrackIn, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> dict:
    track = lifecycle.create_track(db, user_id, name=body.name, universe_name=body.universe_name,
                                   rule_set_id=body.rule_set_id, initial_cash=body.initial_cash,
                                   output_mode=body.output_mode, decision_timeout_s=body.decision_timeout_s,
                                   end_date=body.end_date, description=body.description, mode=body.mode,
                                   replay_start=body.replay_start, frequency=body.frequency,
                                   interval_min=body.interval_min, event_move_pct=body.event_move_pct,
                                   event_max_per_day=body.event_max_per_day, decision_mode=body.decision_mode,
                                   max_tool_calls=body.max_tool_calls, data_options=body.data_options,
                                   anonymize=body.anonymize)
    for agent_id in body.agent_ids:
        lifecycle.enroll(db, track, agent_id)
    db.commit()
    return track_dict(db, track, with_accounts=True)


@router.get("/{track_id}")
def get_track(track_id: int, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> dict:
    return track_dict(db, _get(db, track_id, user_id), with_accounts=True)


@router.delete("/{track_id}")
def delete_track(track_id: int, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> dict:
    t = _get(db, track_id, user_id)
    if t.status != "draft":
        raise HTTPException(400, "只能删除未启动的赛道；已运行的赛道请使用「结束」")
    for a in db.scalars(select(AccountRow).where(AccountRow.track_id == t.id)):
        db.delete(a)
    db.delete(t)
    db.commit()
    return {"ok": True}


@router.post("/{track_id}/accounts")
def enroll(track_id: int, body: EnrollIn, db: Session = Depends(get_db),
           user_id: int = Depends(current_user_id)) -> dict:
    t = _get(db, track_id, user_id)
    lifecycle.enroll(db, t, body.agent_id)
    db.commit()
    return track_dict(db, t, with_accounts=True)


@router.delete("/{track_id}/accounts/{account_id}")
def withdraw(track_id: int, account_id: int, db: Session = Depends(get_db),
             user_id: int = Depends(current_user_id)) -> dict:
    t = _get(db, track_id, user_id)
    lifecycle.withdraw(db, t, account_id)
    db.commit()
    return track_dict(db, t, with_accounts=True)


def _start(track_id: int) -> None:
    with get_sessionmaker()() as s:
        lifecycle.start_track(s, s.get(TrackRow, track_id), utcnow())
        s.commit()


@router.post("/{track_id}/start")
async def start(track_id: int, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> dict:
    _get(db, track_id, user_id)
    await asyncio.to_thread(_start, track_id)   # 需要访问数据源（股票池成分、交易日历），放到线程中执行
    db.expire_all()
    if _get(db, track_id, user_id).mode == "replay":
        tasks.spawn_replay(track_id)
    else:
        tasks.spawn_cycle(track_id, trigger="schedule")
    db.expire_all()
    return track_dict(db, _get(db, track_id, user_id), with_accounts=True)


def _change(track_id: int, action: str, db: Session, user_id: int) -> dict:
    t = _get(db, track_id, user_id)
    lifecycle.set_status(t, action)
    db.commit()
    return track_dict(db, t, with_accounts=True)


@router.post("/{track_id}/pause")
def pause(track_id: int, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> dict:
    return _change(track_id, "pause", db, user_id)


@router.post("/{track_id}/resume")
def resume(track_id: int, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> dict:
    return _change(track_id, "resume", db, user_id)


@router.post("/{track_id}/stop")
def stop(track_id: int, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> dict:
    return _change(track_id, "stop", db, user_id)


@router.post("/{track_id}/run")
async def run(track_id: int, body: RunIn | None = None, db: Session = Depends(get_db),
              user_id: int = Depends(current_user_id)) -> dict:
    t = _get(db, track_id, user_id)
    if t.status not in ("running", "paused"):
        raise HTTPException(400, "赛道未在运行")
    force = bool(body and body.force_round)
    if cycle.replay_pending(t):
        if force:
            raise HTTPException(400, "历史回放尚未完成，不能强制新一轮决策")
        if not tasks.spawn_replay(track_id):
            raise HTTPException(409, "赛道正在执行中，请稍后")
        return {"started": True}
    if not tasks.spawn_cycle(track_id, trigger="manual" if force else "schedule", force_round=force):
        raise HTTPException(409, "赛道正在执行中，请稍后")
    return {"started": True}


@router.get("/{track_id}/leaderboard")
def leaderboard(track_id: int, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> list[dict]:
    return queries.leaderboard(db, _get(db, track_id, user_id))


@router.get("/{track_id}/nav")
def nav(track_id: int, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> dict:
    return queries.nav_series(db, _get(db, track_id, user_id))


@router.get("/{track_id}/rounds")
def rounds(track_id: int, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> list[dict]:
    _get(db, track_id, user_id)
    return [{"id": r.id, "data_date": str(r.data_date), "fill_date": str(r.fill_date) if r.fill_date else None,
             "trigger": r.trigger, "status": r.status, "note": r.note, "snapshot_hash": r.snapshot_hash,
             "snapshot_at": r.snapshot_at.isoformat() if r.snapshot_at else None,
             "started_at": r.started_at.isoformat(),
             "finished_at": r.finished_at.isoformat() if r.finished_at else None}
            for r in db.scalars(select(RoundRow).where(RoundRow.track_id == track_id).order_by(RoundRow.id.desc()))]


@router.get("/{track_id}/rounds/{round_id}/snapshot")
def round_snapshot(track_id: int, round_id: int, db: Session = Depends(get_db),
                   user_id: int = Depends(current_user_id)) -> dict:
    _get(db, track_id, user_id)
    r = db.get(RoundRow, round_id)
    if r is None or r.track_id != track_id:
        raise HTTPException(404, "轮次不存在")
    return {"id": r.id, "data_date": str(r.data_date), "snapshot_hash": r.snapshot_hash, "table": r.market_snapshot}


@router.get("/{track_id}/events")
def events(track_id: int, limit: int = 100, db: Session = Depends(get_db),
           user_id: int = Depends(current_user_id)) -> list[dict]:
    _get(db, track_id, user_id)
    return [{"id": e.id, "trade_date": str(e.trade_date) if e.trade_date else None, "type": e.type, "level": e.level,
             "message": e.message, "account_id": e.account_id, "created_at": e.created_at.isoformat()}
            for e in db.scalars(select(EventRow).where(EventRow.track_id == track_id)
                                .order_by(EventRow.id.desc()).limit(min(limit, 500)))]


@router.get("/{track_id}/export")
def export_data(track_id: int, kind: str, fmt: str = "csv", db: Session = Depends(get_db),
                user_id: int = Depends(current_user_id)) -> Response:
    """导出订单 / 成交 / 净值 / 决策（kind：orders、fills、nav、decisions；fmt：csv、json）。"""
    track = _get(db, track_id, user_id)
    if kind not in export.KINDS or fmt not in ("csv", "json"):
        raise HTTPException(400, "kind 只能是 orders / fills / nav / decisions，fmt 只能是 csv / json")
    data = export.rows(db, track, kind)
    name = export.filename(track, f"{kind}.{fmt}")
    headers = {"Content-Disposition": f'attachment; filename="{name}"'}
    if fmt == "json":
        return JSONResponse(data, headers=headers)
    return Response(export.to_csv(data), media_type="text/csv; charset=utf-8", headers=headers)


@router.get("/{track_id}/report", response_class=HTMLResponse)
def report(track_id: int, download: bool = False, db: Session = Depends(get_db),
           user_id: int = Depends(current_user_id)) -> HTMLResponse:
    """单赛道静态 HTML 报告（图表为内联 SVG，可离线打开）。"""
    track = _get(db, track_id, user_id)
    name = export.filename(track, "report.html")
    headers = {"Content-Disposition": f'attachment; filename="{name}"'} if download else {}
    return HTMLResponse(export.report_html(db, track), headers=headers)


class ConsistencyIn(BaseModel):
    provider_id: int
    model: str | None = None
    sample_size: int = Field(20, ge=1, le=200)


@router.post("/{track_id}/consistency")
async def run_consistency(track_id: int, body: ConsistencyIn, db: Session = Depends(get_db),
                          user_id: int = Depends(current_user_id)) -> dict:
    """用评审模型抽样评估本赛道 AI 决策的推理-行为一致性（后台执行）。"""
    _get(db, track_id, user_id)
    provider = db.get(LLMProviderRow, body.provider_id)
    if provider is None or provider.user_id != user_id:           # 只能用自己配置的模型
        raise HTTPException(400, "模型供应商不存在")
    if not tasks.spawn_consistency(track_id, body.provider_id, body.model, body.sample_size):
        raise HTTPException(409, "评估正在进行中")
    return {"ok": True, "message": f"已开始评估（抽样 {body.sample_size} 条）"}


@router.get("/{track_id}/consistency")
def get_consistency(track_id: int, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> dict:
    from app.arena import consistency

    _get(db, track_id, user_id)
    status = tasks.consistency_status.get(track_id, {})
    return {**consistency.summary(db, track_id), "running": bool(status.get("running")),
            "message": status.get("message")}


@router.post("/{track_id}/clone")
def clone(track_id: int, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> dict:
    """复制为新的草稿赛道（相同股票池、规则、选手与设置），用于重复实验或修改设置后重跑。"""
    track = lifecycle.clone_track(db, _get(db, track_id, user_id))
    db.commit()
    return track_dict(db, track, with_accounts=True)
