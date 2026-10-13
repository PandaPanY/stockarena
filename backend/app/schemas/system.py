from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class DatabaseStatus(BaseModel):
    ok: bool
    dialect: str
    revision: str | None
    error: str | None = None


class MarketClock(BaseModel):
    market: str
    timezone: str
    local_time: datetime


class HealthResponse(BaseModel):
    status: str  # "ok" / "degraded"
    app: str
    version: str
    env: str
    time_utc: datetime
    database: DatabaseStatus
    scheduler_running: bool
    markets: list[MarketClock]


class JobInfo(BaseModel):
    id: str
    name: str
    next_run_time: datetime | None
    trigger: str
