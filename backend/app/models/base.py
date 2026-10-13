"""ORM 基类与通用字段。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Integer, MetaData
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column

from app.core.timeutils import utcnow
from app.db.types import UTCDateTime

# 统一约束命名，保证 SQLite 批量迁移和以后切换 PostgreSQL 时约束名稳定
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# 单用户模式下所有数据归属的用户 ID；多用户时由登录态决定
DEFAULT_USER_ID = 1


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow, onupdate=utcnow, nullable=False
    )


class UserOwnedMixin:
    """归属于某个用户的业务数据。MVP 固定为 DEFAULT_USER_ID，为多用户预留。"""

    @declared_attr
    def user_id(cls) -> Mapped[int]:
        return mapped_column(ForeignKey("users.id"), index=True, nullable=False, default=DEFAULT_USER_ID)
