"""数据库引擎与会话。"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings


def _sqlite_pragmas(dbapi_conn, _record) -> None:
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")   # 读写并发：调度器写入时前端仍可读取
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=30000")   # 日终流程可能较长时间持有写锁
    cursor.close()


def build_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        engine = create_engine(url, connect_args={"check_same_thread": False})
        event.listen(engine, "connect", _sqlite_pragmas)
        return engine
    return create_engine(url, pool_pre_ping=True)


_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine, _SessionLocal
    if _engine is None:
        settings = get_settings()
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        _engine = build_engine(settings.resolved_database_url)
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def get_sessionmaker() -> sessionmaker[Session]:
    get_engine()
    assert _SessionLocal is not None
    return _SessionLocal


def get_db() -> Iterator[Session]:
    """FastAPI 依赖：每个请求一个会话。"""
    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.close()


def reset_engine() -> None:
    """测试用：切换配置后丢弃缓存的引擎。"""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None
