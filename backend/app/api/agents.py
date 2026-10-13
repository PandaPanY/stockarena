"""选手接口（AI 选手可增删改；基线选手为系统预置，只读）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.reflection import MAX_STRATEGY_CHARS
from app.api.deps import current_user_id
from app.db.session import get_db
from app.models import AccountRow, AgentRow, LLMProviderRow, PromptTemplateRow

router = APIRouter(prefix="/api/agents", tags=["agents"])


class AgentIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    provider_id: int
    model: str | None = None
    params: dict = Field(default_factory=dict)
    prompt_template_id: int | None = None
    persona: str = ""
    memory_rounds: int = Field(5, ge=0, le=20)
    description: str = ""
    reflect_enabled: bool = False
    reflect_every: int = Field(5, ge=1, le=60)
    initial_strategy: str = Field("", max_length=MAX_STRATEGY_CHARS)


def to_dict(db: Session, a: AgentRow) -> dict:
    provider = db.get(LLMProviderRow, a.provider_id) if a.provider_id else None
    tpl = db.get(PromptTemplateRow, a.prompt_template_id) if a.prompt_template_id else None
    tracks = db.scalars(select(AccountRow.track_id).where(AccountRow.agent_id == a.id)).all()
    return {
        "id": a.id, "name": a.name, "kind": a.kind, "baseline_type": a.baseline_type,
        "provider_id": a.provider_id, "provider_name": provider.name if provider else None,
        "model": a.model or (provider.default_model if provider else None), "params": a.params,
        "prompt_template_id": a.prompt_template_id,
        "prompt_template": (f"{tpl.title} v{tpl.version}" if tpl
                            else ("默认模板（最新版本）" if a.kind == "llm" else None)),
        "persona": a.persona, "memory_rounds": a.memory_rounds, "description": a.description,
        "archived": a.archived, "track_count": len(set(tracks)),
        "reflect_enabled": a.reflect_enabled, "reflect_every": a.reflect_every,
        "initial_strategy": a.initial_strategy,
    }


def _get(db: Session, agent_id: int, user_id: int) -> AgentRow:
    a = db.get(AgentRow, agent_id)
    if a is None or a.user_id != user_id:
        raise HTTPException(404, "选手不存在")
    return a


def _apply(db: Session, a: AgentRow, body: AgentIn, user_id: int) -> None:
    provider = db.get(LLMProviderRow, body.provider_id)
    if provider is None or provider.user_id != user_id:
        raise HTTPException(400, "供应商不存在")
    if body.prompt_template_id and db.get(PromptTemplateRow, body.prompt_template_id) is None:
        raise HTTPException(400, "prompt 模板不存在")
    a.name, a.provider_id, a.model, a.params = body.name, body.provider_id, body.model or None, body.params
    a.prompt_template_id, a.persona, a.memory_rounds = body.prompt_template_id, body.persona, body.memory_rounds
    a.description = body.description
    a.reflect_enabled, a.reflect_every = body.reflect_enabled, body.reflect_every
    a.initial_strategy = body.initial_strategy.strip()


@router.get("")
def list_agents(include_archived: bool = False, db: Session = Depends(get_db),
                user_id: int = Depends(current_user_id)) -> list[dict]:
    q = select(AgentRow).where(AgentRow.user_id == user_id).order_by(AgentRow.kind.desc(), AgentRow.id)
    if not include_archived:
        q = q.where(AgentRow.archived.is_(False))
    return [to_dict(db, a) for a in db.scalars(q)]


@router.post("")
def create_agent(body: AgentIn, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> dict:
    a = AgentRow(user_id=user_id, kind="llm")
    _apply(db, a, body, user_id)
    db.add(a)
    db.commit()
    return to_dict(db, a)


@router.put("/{agent_id}")
def update_agent(agent_id: int, body: AgentIn, db: Session = Depends(get_db),
                 user_id: int = Depends(current_user_id)) -> dict:
    a = _get(db, agent_id, user_id)
    if a.kind != "llm":
        raise HTTPException(400, "基线选手不可修改")
    _apply(db, a, body, user_id)
    db.commit()
    return to_dict(db, a)


@router.delete("/{agent_id}")
def archive_agent(agent_id: int, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> dict:
    a = _get(db, agent_id, user_id)
    if a.kind != "llm":
        raise HTTPException(400, "基线选手不可删除")
    a.archived = True   # 保留历史决策记录，只是不再出现在列表中
    db.commit()
    return {"ok": True}
