"""设置（键值）：交易成本、自选股监控、首页明星股等在界面上修改的设置。

按用户区分的设置用 user_key：管理员沿用原来的键（多用户之前的数据不用迁移），其他用户的键加「@用户ID」。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.timeutils import utcnow
from app.db.types import UTCDateTime
from app.models.base import DEFAULT_USER_ID, Base


def user_key(key: str, uid: int) -> str:
    """按用户区分的设置键：管理员为 key，其他用户为 key@uid。"""
    return key if uid == DEFAULT_USER_ID else f"{key}@{uid}"


class AppSettingRow(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)
