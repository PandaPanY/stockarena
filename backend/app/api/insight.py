"""AI 洞察接口：问答助手、每周点评与偏差分析的列表与手动生成、设置。只能访问自己的数据。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import current_user_id
from app.db.session import get_sessionmaker
from app.insight import assistant, data, llm, notes
from app.models.insight import AiNoteRow

router = APIRouter(prefix="/api/insight", tags=["insight"])


def _err(e: Exception) -> HTTPException:
    return HTTPException(400, str(e))


def _note(n: AiNoteRow, full: bool = False) -> dict:
    out = {"id": n.id, "kind": n.kind, "ref_kind": n.ref_kind, "ref_id": n.ref_id, "ref_name": n.ref_name,
           "account_id": n.account_id, "period_start": n.period_start.isoformat() if n.period_start else None,
           "period_end": n.period_end.isoformat() if n.period_end else None, "content": n.content,
           "status": n.status, "error": n.error, "model": n.model, "tokens_in": n.tokens_in,
           "tokens_out": n.tokens_out, "created_at": n.created_at.isoformat()}
    if full:
        out["material"] = n.material
    return out


@router.get("")
def overview(uid: int = Depends(current_user_id)) -> dict:
    """跟踪中的策略 / 组合、设置、最近的点评与分析。"""
    with get_sessionmaker()() as s:
        rows = s.scalars(select(AiNoteRow).where(AiNoteRow.user_id == uid).order_by(AiNoteRow.id.desc()).limit(60))
        return {"followed": data.followed(s, uid), "settings": llm.settings(s, uid), "notes": [_note(n) for n in rows],
                "running": uid in notes._running}


@router.get("/notes/{note_id}")
def note(note_id: int, uid: int = Depends(current_user_id)) -> dict:
    """一篇点评或分析，含交给模型的数据（核对用）。"""
    with get_sessionmaker()() as s:
        n = s.get(AiNoteRow, note_id)
        if n is None or n.user_id != uid:
            raise HTTPException(404, "没有这篇")
        return _note(n, full=True)


class RunIn(BaseModel):
    kind: str = Field(pattern="^(weekly|deviation)$")
    ref_kind: str | None = Field(default=None, pattern="^(strategy|portfolio)$")
    ref_id: int | None = None


@router.post("/notes/run")
async def run(body: RunIn, uid: int = Depends(current_user_id)) -> dict:
    """立即生成（后台运行）：每周点评可指定一个策略 / 组合，偏差分析不指定时只分析健康度为「提醒」的。"""
    with get_sessionmaker()() as s:
        try:
            llm.client_for(s, uid)
        except data.InsightError as e:
            raise _err(e) from None
        refs = {(r["kind"], r["id"]) for r in data.followed(s, uid)}
    ref = (body.ref_kind, body.ref_id) if body.ref_kind and body.ref_id else None
    if ref is not None and ref not in refs:
        raise HTTPException(400, "这不是你正在跟踪的策略或组合")
    if not refs:
        raise HTTPException(400, "还没有实时跟踪的策略或组合")
    return notes.run_in_background(uid, body.kind, ref)


class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    history: list[dict] = Field(default_factory=list, max_length=20)


@router.post("/ask")
async def ask(body: AskIn, uid: int = Depends(current_user_id)) -> dict:
    """问答助手：回答一个问题（可能需要一两分钟，取决于模型）。"""
    try:
        return await assistant.ask(uid, body.question, body.history)
    except data.InsightError as e:
        raise _err(e) from None


class SettingsIn(BaseModel):
    provider_id: int | None = None
    model: str | None = None
    weekly: bool | None = None
    deviation: bool | None = None
    use_default_model: bool = False


@router.put("/settings")
def put_settings(body: SettingsIn, uid: int = Depends(current_user_id)) -> dict:
    """provider_id 为空且 use_default_model 时改回沿用自选股监控的模型。"""
    kw = body.model_dump(exclude={"use_default_model"}, exclude_none=True)
    if body.use_default_model:
        kw["provider_id"] = None
    with get_sessionmaker()() as s:
        try:
            cfg = llm.set_settings(s, uid, **kw)
        except data.InsightError as e:
            raise _err(e) from None
        s.commit()
        return cfg
