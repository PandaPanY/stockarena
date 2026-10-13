"""离线进化实验接口（PRD 3.7 第 3 层）。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_user_id
from app.arena import experiment, tasks
from app.db.session import get_db
from app.models import AgentRow, ExperimentRow, RuleSetRow

router = APIRouter(prefix="/api/experiments", tags=["experiments"])


class ExperimentIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    agent_id: int
    universe_name: str
    rule_set_id: int
    initial_cash: Decimal = Field(gt=0)
    output_mode: str = "targets"
    train_start: date
    train_end: date
    valid_start: date
    valid_end: date
    generations: int = Field(3, ge=1, le=10)
    reflect_every: int = Field(5, ge=1, le=60)
    anonymize: bool = False
    description: str = ""


def to_dict(db: Session, e: ExperimentRow) -> dict:
    agent = db.get(AgentRow, e.agent_id)
    rs = db.get(RuleSetRow, e.rule_set_id)
    return {
        "id": e.id, "name": e.name, "agent_id": e.agent_id, "agent_name": agent.name if agent else None,
        "universe_name": e.universe_name, "rule_set_id": e.rule_set_id, "rule_set_title": rs.title if rs else None,
        "initial_cash": format(e.initial_cash, "f"), "output_mode": e.output_mode,
        "train_start": str(e.train_start), "train_end": str(e.train_end), "valid_start": str(e.valid_start),
        "valid_end": str(e.valid_end), "generations": e.generations, "reflect_every": e.reflect_every,
        "anonymize": e.anonymize, "status": e.status, "progress": e.progress, "results": e.results or [],
        "description": e.description, "created_at": e.created_at.isoformat(),
    }


def _get(db: Session, exp_id: int, user_id: int) -> ExperimentRow:
    e = db.get(ExperimentRow, exp_id)
    if e is None or e.user_id != user_id:
        raise HTTPException(404, "实验不存在")
    return e


@router.get("")
def list_experiments(db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> list[dict]:
    rows = db.scalars(select(ExperimentRow).where(ExperimentRow.user_id == user_id).order_by(ExperimentRow.id.desc()))
    return [to_dict(db, e) for e in rows]


@router.get("/defaults")
def defaults() -> dict:
    return experiment.default_dates(date.today())


@router.post("")
async def create_experiment(body: ExperimentIn, db: Session = Depends(get_db),
                            user_id: int = Depends(current_user_id)) -> dict:
    """创建并立即开始（后台依次执行各代回放，耗时较长）。"""
    agent = db.get(AgentRow, body.agent_id)
    if agent is None or agent.user_id != user_id:             # 只能用自己的选手
        raise HTTPException(404, "选手不存在")
    e = ExperimentRow(user_id=user_id, **body.model_dump())
    experiment.validate(db, e)
    db.add(e)
    db.commit()
    tasks.spawn_experiment(e.id)
    return to_dict(db, e)


@router.get("/{exp_id}")
def get_experiment(exp_id: int, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> dict:
    return to_dict(db, _get(db, exp_id, user_id))


@router.post("/{exp_id}/cancel")
def cancel_experiment(exp_id: int, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> dict:
    e = _get(db, exp_id, user_id)
    if e.status not in ("pending", "running"):
        raise HTTPException(400, "实验已结束")
    experiment.cancel(db, e)
    db.commit()
    return to_dict(db, e)
