"""技能接口：沙箱状态与初始化、暂存（GitHub / 上传）、审阅、批准安装、启停、卸载、技能终端、调用记录。"""

from __future__ import annotations

import asyncio
import base64
import binascii

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_user_id, require_admin
from app.db.session import get_db
from app.models.skills import SkillCallRow, SkillRow
from app.skills import package, sandbox, service
from app.skills.gate import DISABLED_MESSAGE, skills_enabled


def require_skills_enabled() -> None:
    if not skills_enabled():
        raise HTTPException(409, DISABLED_MESSAGE)


# 技能只对管理员开放（安装、审批、使用都由管理员；技能用的是管理员个人的授权）；总开关见 app/skills/gate.py
router = APIRouter(prefix="/api/skills", tags=["skills"],
                   dependencies=[Depends(require_admin), Depends(require_skills_enabled)])

_tasks: set[asyncio.Task] = set()
_sandbox_task: dict = {}


def _spawn(coro) -> None:
    t = asyncio.get_running_loop().create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


def _get(db: Session, sid: int, user_id: int) -> SkillRow:
    row = db.get(SkillRow, sid)
    if row is None or row.user_id != user_id:
        raise HTTPException(404, "技能不存在")
    return row


@router.get("")
async def list_skills(db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> dict:
    rows = db.scalars(select(SkillRow).where(SkillRow.user_id == user_id, SkillRow.status.notin_(("removed",
                                                                                                   "rejected")))
                      .order_by(SkillRow.id.desc()))
    return {"skills": [service.to_dict(r) for r in rows], "sandbox": await sandbox.status(),
            "sandbox_task": _sandbox_task or None}


@router.post("/sandbox/setup")
async def setup_sandbox() -> dict:
    """初始化沙箱：构建运行镜像、创建内部网络、启动出网过滤代理（首次约几分钟）。"""
    if _sandbox_task.get("running"):
        raise HTTPException(409, "正在初始化")
    _sandbox_task.clear()
    _sandbox_task.update({"running": True, "message": "初始化中（首次需要下载基础镜像，约几分钟）…"})

    async def run():
        try:
            msg = await sandbox.setup()
            _sandbox_task.update({"running": False, "message": msg, "ok": True})
        except Exception as e:  # noqa: BLE001
            _sandbox_task.update({"running": False, "message": f"失败：{e}", "ok": False})

    _spawn(run())
    return _sandbox_task


class StageIn(BaseModel):
    source: str = Field(pattern="^(github|upload)$")
    url: str | None = Field(None, max_length=500)
    filename: str | None = Field(None, max_length=200)
    content_base64: str | None = None


@router.post("/stage")
async def stage(body: StageIn, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> dict:
    """获取技能包并生成审阅清单（只下载、解压、扫描，不执行任何文件）。"""
    try:
        if body.source == "github":
            if not body.url:
                raise HTTPException(400, "请填写 GitHub 地址")
            row = await asyncio.to_thread(service.stage_github, db, body.url, user_id)
        else:
            try:
                data = base64.b64decode(body.content_base64 or "", validate=True)
            except (binascii.Error, ValueError):
                raise HTTPException(400, "文件内容不是有效的 base64") from None
            row = await asyncio.to_thread(service.stage_upload, db, body.filename or "upload.zip", data, user_id)
    except package.PackageError as e:
        raise HTTPException(400, str(e)) from None
    except OSError as e:
        raise HTTPException(502, f"下载失败：{e}") from None
    db.commit()
    return service.to_dict(row, detail=True)


# ---------------------------------------------------------------- 技能市场（第 3 步；须定义在 /{sid} 之前）


@router.get("/markets")
def list_markets(db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> list[dict]:
    from app.models.skills import SkillMarketRow
    from app.skills import market

    market.ensure_defaults(db, user_id)
    db.commit()
    return [market.to_dict(m) for m in db.scalars(select(SkillMarketRow).order_by(SkillMarketRow.id))]


class MarketIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    kind: str = Field(pattern="^(github|skillsmp)$")
    url: str = Field(max_length=500)
    enabled: bool = True


@router.post("/markets")
def add_market(body: MarketIn, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> dict:
    from app.models.skills import SkillMarketRow
    from app.skills import market

    try:
        market.validate(body.kind, body.url)
    except (market.MarketError, package.PackageError) as e:
        raise HTTPException(400, str(e)) from None
    m = SkillMarketRow(user_id=user_id, name=body.name, kind=body.kind, url=body.url.strip(), enabled=body.enabled)
    db.add(m)
    db.commit()
    return market.to_dict(m)


@router.patch("/markets/{mid}")
def update_market(mid: int, body: MarketIn, db: Session = Depends(get_db),
                  user_id: int = Depends(current_user_id)) -> dict:
    from app.models.skills import SkillMarketRow
    from app.skills import market

    m = db.get(SkillMarketRow, mid)
    if m is None or m.user_id != user_id:
        raise HTTPException(404, "市场不存在")
    try:
        market.validate(body.kind, body.url)
    except (market.MarketError, package.PackageError) as e:
        raise HTTPException(400, str(e)) from None
    if body.url.strip() != m.url:
        m.index, m.indexed_at = [], None
    m.name, m.kind, m.url, m.enabled = body.name, body.kind, body.url.strip(), body.enabled
    db.commit()
    return market.to_dict(m)


@router.delete("/markets/{mid}")
def delete_market(mid: int, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> dict:
    from app.models.skills import SkillMarketRow

    m = db.get(SkillMarketRow, mid)
    if m is None or m.user_id != user_id:
        raise HTTPException(404, "市场不存在")
    db.delete(m)
    db.commit()
    return {"ok": True}


@router.post("/markets/{mid}/refresh")
async def refresh_market(mid: int, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> dict:
    from app.models.skills import SkillMarketRow
    from app.skills import market

    m = db.get(SkillMarketRow, mid)
    if m is None or m.user_id != user_id:
        raise HTTPException(404, "市场不存在")
    if m.kind != "github":
        return market.to_dict(m)
    try:
        await asyncio.to_thread(market.refresh_github, m, service.http_get)
    except Exception as e:  # noqa: BLE001
        m.last_error = str(e)[:500]
        db.commit()
        raise HTTPException(502, f"刷新失败：{e}") from None
    db.commit()
    return market.to_dict(m)


@router.get("/market-search")
async def market_search(q: str, market_id: int | None = None, db: Session = Depends(get_db),
                        user_id: int = Depends(current_user_id)) -> list[dict]:
    from app.skills import market

    market.ensure_defaults(db, user_id)
    try:
        res = await asyncio.to_thread(market.search, db, q, market_id)
    except market.MarketError as e:
        db.commit()
        raise HTTPException(400, str(e)) from None
    db.commit()
    installed = {r.source_url.rstrip("/") for r in db.scalars(select(SkillRow).where(
        SkillRow.status.in_(("pending", "installing", "installed", "failed"))))}
    return [{**r, "staged": r["url"].rstrip("/") in installed} for r in res]


class MarketStageIn(BaseModel):
    url: str = Field(max_length=500)
    market_id: int | None = None


@router.post("/markets/stage")
async def market_stage(body: MarketStageIn, db: Session = Depends(get_db),
                       user_id: int = Depends(current_user_id)) -> dict:
    """从市场获取技能并生成审阅清单（与 GitHub 安装相同：不执行任何文件，等待批准）。"""
    try:
        row = await asyncio.to_thread(service.stage_github, db, body.url, user_id, service.http_get, body.market_id)
    except package.PackageError as e:
        raise HTTPException(400, str(e)) from None
    except OSError as e:
        raise HTTPException(502, f"下载失败：{e}") from None
    db.commit()
    return service.to_dict(row, detail=True)


# ---------------------------------------------------------------- AI 安装申请（第 4 步）


@router.get("/requests")
def list_requests(db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> dict:
    from app.models.skills import SkillRequestRow
    from app.skills import requests

    rows = list(db.scalars(select(SkillRequestRow).order_by(SkillRequestRow.id.desc()).limit(50)))
    return {"settings": requests.settings(db),
            "requests": [requests.to_dict(r, db.get(SkillRow, r.skill_id) if r.skill_id else None) for r in rows]}


class SkillSettingsIn(BaseModel):
    ai_requests: bool


@router.put("/settings")
def put_settings(body: SkillSettingsIn, db: Session = Depends(get_db)) -> dict:
    """是否允许 AI 搜索技能市场并提交安装申请（安装始终需要用户批准）。"""
    from app.skills import requests

    out = requests.set_settings(db, ai_requests=body.ai_requests)
    db.commit()
    return out


@router.post("/requests/{rid}/reject")
def reject_request(rid: int, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> dict:
    """拒绝 AI 的申请（若技能包已暂存，一并拒绝）。"""
    from app.core.timeutils import utcnow
    from app.models.skills import SkillRequestRow
    from app.skills import requests

    req = db.get(SkillRequestRow, rid)
    if req is None:
        raise HTTPException(404, "申请不存在")
    if req.skill_id:
        row = db.get(SkillRow, req.skill_id)
        if row is not None and row.status in ("pending", "failed"):
            service.reject(db, row)
    if req.status in ("pending", "failed"):
        req.status, req.decided_at = "rejected", utcnow()
    db.commit()
    return requests.to_dict(req)


@router.get("/{sid}")
def get_skill(sid: int, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> dict:
    return service.to_dict(_get(db, sid, user_id), detail=True)


@router.get("/{sid}/file")
def get_file(sid: int, path: str, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> dict:
    """审阅用：查看包内文件内容（脚本也可以看）。"""
    try:
        return {"path": path, "content": service.preview(_get(db, sid, user_id), path)}
    except service.SkillError as e:
        raise HTTPException(404, str(e)) from None


class PolicyIn(BaseModel):
    allow_domains: list[str] | None = None
    commands: list[str] | None = None
    user_only: list[str] | None = None
    setup_command: str | None = Field(None, max_length=500)
    enabled: bool | None = None


@router.patch("/{sid}")
def update_skill(sid: int, body: PolicyIn, db: Session = Depends(get_db),
                 user_id: int = Depends(current_user_id)) -> dict:
    row = _get(db, sid, user_id)
    try:
        service.set_policy(row, allow_domains=body.allow_domains, commands=body.commands, user_only=body.user_only,
                           setup_command=body.setup_command)
    except service.SkillError as e:
        raise HTTPException(400, str(e)) from None
    if body.enabled is not None:
        if row.status != "installed" and body.enabled:
            raise HTTPException(400, "只有已安装的技能可以启用")
        row.enabled = body.enabled
    db.flush()
    service.sync_proxy(db)
    db.commit()
    return service.to_dict(row, detail=True)


@router.post("/{sid}/approve")
async def approve(sid: int, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> dict:
    """批准安装：在沙箱中运行安装命令（若有），完成后 AI 即可按需使用。"""
    row = _get(db, sid, user_id)
    st = await sandbox.status()
    if not st["ready"]:
        raise HTTPException(400, f"沙箱未就绪：{st['message']}")
    try:
        service.approve(db, row)
    except service.SkillError as e:
        raise HTTPException(400, str(e)) from None
    from app.skills import requests

    requests.decide(db, row, approved=True)
    db.commit()
    _spawn(service.install(sid))
    return service.to_dict(row)


@router.post("/{sid}/reject")
def reject(sid: int, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> dict:
    row = _get(db, sid, user_id)
    try:
        service.reject(db, row)
    except service.SkillError as e:
        raise HTTPException(400, str(e)) from None
    from app.skills import requests

    requests.decide(db, row, approved=False)
    db.commit()
    return service.to_dict(row)


@router.delete("/{sid}")
async def uninstall(sid: int, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> dict:
    row = _get(db, sid, user_id)
    service.uninstall(db, row)
    db.commit()
    await sandbox.stop_session(sid)
    return {"ok": True}


class RunIn(BaseModel):
    argv: list[str] = Field(min_length=1, max_length=service.ARG_LIMIT)


@router.post("/{sid}/run")
async def run(sid: int, body: RunIn, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> dict:
    """技能终端：用户本人在沙箱中执行技能命令（例如完成技能自己的鉴权）；同一终端的命令在同一个会话容器中执行。"""
    _get(db, sid, user_id)
    db.close()
    try:
        res = await service.call(sid, body.argv, "user", "终端")
    except service.SkillError as e:
        raise HTTPException(400, str(e)) from None
    return {"exit_code": res.exit_code, "output": res.output, "seconds": res.seconds, "blocked": res.blocked,
            "timed_out": res.timed_out}


@router.delete("/{sid}/session")
async def end_session(sid: int, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> dict:
    """关闭技能终端：结束该技能的终端会话容器。"""
    _get(db, sid, user_id)
    db.close()
    return {"ok": await sandbox.stop_session(sid)}


@router.get("/{sid}/calls")
def calls(sid: int, limit: int = 50, db: Session = Depends(get_db), user_id: int = Depends(current_user_id)) -> list:
    _get(db, sid, user_id)
    rows = list(db.scalars(select(SkillCallRow).where(SkillCallRow.skill_id == sid)
                           .order_by(SkillCallRow.id.desc()).limit(min(limit, 200))))
    return service.dumps_calls(rows)
