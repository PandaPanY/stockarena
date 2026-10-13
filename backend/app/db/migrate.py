"""以代码方式执行 Alembic 迁移（启动时自动升级、测试建库共用）。"""

from __future__ import annotations

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, inspect, text

from app.core.config import BACKEND_ROOT


def alembic_config(database_url: str) -> Config:
    cfg = Config(str(BACKEND_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", database_url)
    cfg.attributes["skip_logging_config"] = True
    return cfg


def upgrade_to_head(database_url: str) -> None:
    command.upgrade(alembic_config(database_url), "head")


def current_revision(engine: Engine) -> str | None:
    """读取当前迁移版本。直接查表而不是用 MigrationContext，避免健康检查每次都打印 Alembic 日志。"""
    with engine.connect() as conn:
        if not inspect(conn).has_table("alembic_version"):
            return None
        return conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
