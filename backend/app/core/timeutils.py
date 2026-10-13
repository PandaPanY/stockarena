"""时间与时区工具。

约定：数据库存储与内部计算一律使用 UTC aware datetime，只在展示和判断交易时段时转换为市场本地时间。
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

# 与 app/market/markets.py 中的市场定义保持一致
MARKET_TIMEZONES: dict[str, ZoneInfo] = {
    "CN": ZoneInfo("Asia/Shanghai"),
    "HK": ZoneInfo("Asia/Hong_Kong"),
    "US": ZoneInfo("America/New_York"),
}


def utcnow() -> datetime:
    return datetime.now(UTC)


def ensure_utc(dt: datetime) -> datetime:
    """naive datetime 视为 UTC；aware datetime 转换为 UTC。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def market_tz(market: str) -> ZoneInfo:
    try:
        return MARKET_TIMEZONES[market]
    except KeyError as e:
        raise ValueError(f"未知市场：{market}") from e


def to_market_time(dt: datetime, market: str) -> datetime:
    return ensure_utc(dt).astimezone(market_tz(market))


def market_now(market: str) -> datetime:
    return to_market_time(utcnow(), market)
