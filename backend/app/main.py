"""FastAPI 应用入口。

开发运行：在 backend/ 目录下执行 `uv run uvicorn app.main:app --reload`
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from app.api import (
    accounts,
    agents,
    auth,
    experiments,
    home,
    meta,
    notify,
    providers,
    quant,
    skills,
    stocks,
    system,
    tracks,
    watch,
)
from app.arena.deps import secret_box
from app.arena.intraday import get_engine as get_intraday_engine
from app.arena.lifecycle import ArenaError
from app.arena.seed import seed_defaults
from app.auth import core as auth_core
from app.auth.middleware import AuthMiddleware
from app.core.config import get_settings
from app.core.crypto import SecretError
from app.core.keycheck import check_encrypted_secrets
from app.core.logsetup import setup_logging
from app.core.network import apply_direct_hosts
from app.core.slowlog import SlowRequestLog, watch_loop_lag
from app.db.migrate import upgrade_to_head
from app.db.session import get_engine, get_sessionmaker
from app.market.retry import DataSourceError
from app.mcp import server as mcp_server
from app.quant import tasks as quant_tasks
from app.scheduler import register_jobs, shutdown_scheduler, start_scheduler
from app.skills import sandbox as skill_sandbox
from app.skills.gate import skills_enabled

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_dir)
    apply_direct_hosts(settings.direct_hosts)
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    if settings.auto_migrate:
        upgrade_to_head(settings.resolved_database_url)
    get_engine()
    with get_sessionmaker()() as session:
        seed_defaults(session, settings.config_dir)
    if settings.auth_enabled and not auth_core.admin_configured():
        auth_core.ensure_setup_code(announce=True)     # 首次设置码写进日志
    secret_box()                                       # 主密钥格式不对时在启动时就报错
    check_encrypted_secrets()
    quant_tasks.recover()
    quant_tasks.check_costs_on_startup()
    intraday_task = None
    if settings.auto_run:
        register_jobs()
        asyncio.get_running_loop().run_in_executor(None, quant_tasks.warm_caches)   # 后台，不阻塞启动
        intraday_task = asyncio.get_running_loop().create_task(get_intraday_engine().run())
    start_scheduler()
    if skills_enabled():
        await skill_sandbox.cleanup_sessions()
    lag_stop = asyncio.Event()
    lag_task = asyncio.get_running_loop().create_task(watch_loop_lag(lag_stop))     # 事件循环卡顿诊断
    logger.info("%s %s 已启动（env=%s）", settings.app_name, settings.version, settings.env)
    try:
        yield
    finally:
        lag_stop.set()
        await lag_task
        if intraday_task is not None:
            get_intraday_engine().stop()
            intraday_task.cancel()
        shutdown_scheduler()
        if skills_enabled():
            await skill_sandbox.cleanup_sessions()
        logger.info("%s 已停止", settings.app_name)


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, version=settings.version, lifespan=lifespan)
    # 先加的在内层：跨域预检由外层 CORS 处理，登录校验在其内
    app.add_middleware(AuthMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(SlowRequestLog)      # 最外层：记录包括登录校验在内的完整耗时

    @app.exception_handler(ArenaError)
    async def _arena_error(_req: Request, exc: ArenaError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(SecretError)
    async def _secret_error(_req: Request, exc: SecretError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(DataSourceError)
    async def _data_error(_req: Request, exc: DataSourceError) -> JSONResponse:
        return JSONResponse(status_code=502, content={"detail": f"行情数据源暂时不可用：{exc}"})

    for module in (auth, system, providers, agents, meta, tracks, accounts, experiments, quant, notify, skills, stocks,
                   home, watch, mcp_server):
        app.include_router(module.router)

    dist = settings.frontend_dist
    if dist is not None and (dist / "index.html").is_file():
        _serve_frontend(app, dist)
    else:
        @app.get("/", include_in_schema=False)
        def root() -> dict[str, str]:
            return {"app": settings.app_name, "docs": "/docs", "health": "/api/system/health"}

    return app


def _serve_frontend(app: FastAPI, dist: Path) -> None:
    """托管前端打包结果：静态文件按路径返回，其余页面路由（Vue Router 的 history 模式）返回 index.html。"""
    root = dist.resolve()
    index = root / "index.html"

    @app.get("/{path:path}", include_in_schema=False)
    def frontend(path: str) -> FileResponse:
        if path.startswith(("api/", "ws/")) or path in ("api", "ws"):
            raise HTTPException(404, "接口不存在")
        f = (root / path).resolve()
        if path and f.is_file() and root in f.parents:
            return FileResponse(f)
        return FileResponse(index)


app = create_app()
