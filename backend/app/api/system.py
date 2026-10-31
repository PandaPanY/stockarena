"""系统状态接口。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.api.deps import require_admin
from app.core.config import get_settings
from app.core.timeutils import MARKET_TIMEZONES, to_market_time, utcnow
from app.db.migrate import current_revision
from app.db.session import get_db, get_engine
from app.models import DataSourceStatusRow, EventRow
from app.scheduler import get_scheduler
from app.schemas.system import DatabaseStatus, HealthResponse, JobInfo, MarketClock

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/system", tags=["system"])


def _database_status() -> DatabaseStatus:
    engine = get_engine()
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return DatabaseStatus(ok=True, dialect=engine.dialect.name, revision=current_revision(engine))
    except Exception as e:  # 健康检查需要返回错误信息而不是抛出
        logger.exception("数据库健康检查失败")
        return DatabaseStatus(ok=False, dialect=engine.dialect.name, revision=None, error=str(e))


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    settings = get_settings()
    now = utcnow()
    db = _database_status()
    scheduler_running = get_scheduler().running
    return HealthResponse(
        status="ok" if db.ok and scheduler_running else "degraded",
        app=settings.app_name,
        version=settings.version,
        env=settings.env,
        time_utc=now,
        database=db,
        scheduler_running=scheduler_running,
        markets=[
            MarketClock(market=m, timezone=str(tz), local_time=to_market_time(now, m))
            for m, tz in MARKET_TIMEZONES.items()
        ],
    )


@router.get("/datasources")
def datasources(db: Session = Depends(get_db)) -> list[dict]:
    return [{"name": r.name, "last_success_at": r.last_success_at.isoformat() if r.last_success_at else None,
             "last_failure_at": r.last_failure_at.isoformat() if r.last_failure_at else None,
             "last_error": r.last_error, "consecutive_failures": r.consecutive_failures}
            for r in db.scalars(select(DataSourceStatusRow).order_by(DataSourceStatusRow.name))]


@router.get("/events")
def events(limit: int = 50, level: str | None = None, db: Session = Depends(get_db),
           _: int = Depends(require_admin)) -> list[dict]:
    """系统事件日志（管理员）。"""
    q = select(EventRow).order_by(EventRow.id.desc()).limit(min(limit, 500))
    if level:
        q = q.where(EventRow.level == level)
    return [{"id": e.id, "track_id": e.track_id, "trade_date": str(e.trade_date) if e.trade_date else None,
             "type": e.type, "level": e.level, "message": e.message, "created_at": e.created_at.isoformat()}
            for e in db.scalars(q)]


@router.get("/jobs", response_model=list[JobInfo])
def jobs(_: int = Depends(require_admin)) -> list[JobInfo]:
    return [
        JobInfo(id=job.id, name=job.name, next_run_time=job.next_run_time, trigger=str(job.trigger))
        for job in get_scheduler().get_jobs()
    ]


# ---------------------------------------------------------------- 运行保障（app/ops）


@router.get("/ops")
def ops_status(db: Session = Depends(get_db), _: int = Depends(require_admin)) -> dict:
    """运行状况：最近一次停机、定时任务最近结果、每日完整性检查、备份与磁盘、模型与推送通道。"""
    from datetime import timedelta

    from app.llm import health
    from app.models import LLMProviderRow, NotificationRow
    from app.ops import backup, get_state, integrity

    recent = get_state(db, "jobs")
    jobs_out = []
    for job in get_scheduler().get_jobs():
        r = recent.get(job.id) or {}
        jobs_out.append({"id": job.id, "name": job.name, "next_run_time": job.next_run_time.isoformat()
                         if job.next_run_time else None, **{k: r.get(k) for k in
                                                              ("status", "started_at", "finished_at", "seconds",
                                                               "error")}})
    folder = backup.local_dir()
    files = sorted(folder.glob(f"{backup.PREFIX}*{backup.SUFFIX}"), reverse=True) if folder.is_dir() else []
    since = utcnow() - timedelta(days=7)
    failed_sends = [{"title": n.title, "error": n.error, "at": n.created_at.isoformat()}
                    for n in db.scalars(select(NotificationRow).where(NotificationRow.status == "failed",
                                                                      NotificationRow.created_at >= since)
                                        .order_by(NotificationRow.id.desc()).limit(10))]
    return {
        "downtime": get_state(db, "downtime").get("last"),
        "jobs": jobs_out,
        "integrity": get_state(db, "integrity").get("last"),
        "backup": get_state(db, "backup") | {
            "files": [{"name": f.name, "size": f.stat().st_size} for f in files[:10]],
            "keep": get_settings().backup_keep, "extra_dir": str(get_settings().backup_dir or "") or None},
        "disk": integrity.disk_status(),
        "models": [{"id": p.id, "name": p.name, **{k: v for k, v in health.get(db, p.id).items()
                                                    if k in ("fails", "last_error", "last_ok_at", "down_since")}}
                   for p in db.scalars(select(LLMProviderRow).order_by(LLMProviderRow.id))],
        "failed_sends": failed_sends,
    }


@router.post("/ops/backup")
async def ops_backup_now(_: int = Depends(require_admin)) -> dict:
    """立即备份一次数据库。"""
    import asyncio

    from fastapi import HTTPException

    from app.ops import backup, now_iso, update_state

    try:
        out = await asyncio.to_thread(backup.run_backup)
    except backup.BackupError as e:
        raise HTTPException(400, str(e)) from None
    update_state("backup", last=out | {"at": now_iso()}, last_error=None)
    return out


@router.post("/ops/check")
async def ops_check_now(_: int = Depends(require_admin)) -> dict:
    """立即做一次完整性检查（最近一个 A 股交易日；有问题也推送）。"""
    import asyncio

    from app.ops import integrity

    return await asyncio.to_thread(integrity.check)
