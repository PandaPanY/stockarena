"""自定义列类型。

- DecimalString：以字符串存储 Decimal，SQLite 下无精度损失，切换 PostgreSQL 时同样可用。
- UTCDateTime：入库前转为 UTC，读出时恢复为 UTC aware datetime。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import DateTime, String
from sqlalchemy.types import TypeDecorator

from app.core.money import to_decimal
from app.core.timeutils import ensure_utc


class DecimalString(TypeDecorator[Decimal]):
    impl = String(40)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return format(to_decimal(value), "f")

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return Decimal(value)


class UTCDateTime(TypeDecorator[datetime]):
    impl = DateTime(timezone=False)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return ensure_utc(value).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=UTC)
