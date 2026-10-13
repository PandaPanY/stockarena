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
