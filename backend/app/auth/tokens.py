"""个人 API 令牌：给 MCP 客户端（Claude Code、Cursor 等）用，只在 /api/mcp 有效，不能访问其他接口。

- 令牌原文形如 sa_xxxxxxxx…，只在创建时显示一次；数据库只存 sha256 哈希和开头几位（用于辨认）。
- 默认只读（查询、试算）；创建时勾选「允许修改」才能新建策略、提交新版本、启动 AI 优化。
- 用户被停用或删除后令牌立即失效；撤销即删除。
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import core
from app.core.timeutils import utcnow
from app.models.user import ApiToken

PREFIX = "sa_"
MAX_PER_USER = 10
CACHE_TTL = 30.0              # 校验结果缓存秒数（撤销时立即清除）
TOUCH_EVERY = 600.0           # 最近使用时间最多每 10 分钟写一次库


class TokenError(ValueError):
    pass


@dataclass(frozen=True)
class TokenInfo:
    token_id: int
    user_id: int
    can_write: bool


_cache: dict[str, tuple[float, TokenInfo | None]] = {}
_touched: dict[int, float] = {}
_lock = threading.Lock()


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_dict(t: ApiToken) -> dict:
    return {"id": t.id, "name": t.name, "prefix": t.prefix, "can_write": t.can_write,
            "created_at": t.created_at.isoformat(),
            "last_used_at": t.last_used_at.isoformat() if t.last_used_at else None}


def list_for(s: Session, user_id: int) -> list[dict]:
    rows = s.scalars(select(ApiToken).where(ApiToken.user_id == user_id).order_by(ApiToken.id.desc()))
    return [token_dict(t) for t in rows]


def create(s: Session, user_id: int, name: str, can_write: bool = False) -> tuple[ApiToken, str]:
    """新建令牌，返回（记录, 令牌原文）。原文只在这里出现一次。"""
    name = name.strip()
    if not name:
        raise TokenError("请填写令牌名称（例如用在哪个客户端）")
    n = s.scalar(select(func.count()).select_from(ApiToken).where(ApiToken.user_id == user_id)) or 0
    if n >= MAX_PER_USER:
        raise TokenError(f"每个用户最多 {MAX_PER_USER} 个令牌，请先撤销不用的")
    raw = PREFIX + secrets.token_urlsafe(32)
    row = ApiToken(user_id=user_id, name=name[:64], token_hash=_hash(raw), prefix=raw[:10], can_write=can_write)
    s.add(row)
    s.flush()
    return row, raw


def revoke(s: Session, user_id: int, token_id: int) -> bool:
    row = s.get(ApiToken, token_id)
    if row is None or row.user_id != user_id:
        return False
    s.delete(row)
    s.flush()
    with _lock:
        for k in [k for k, (_, v) in _cache.items() if v and v.token_id == token_id]:
            _cache.pop(k, None)
    return True


def _lookup(h: str) -> TokenInfo | None:
    from app.db.session import get_sessionmaker

    with get_sessionmaker()() as s:
        row = s.scalar(select(ApiToken).where(ApiToken.token_hash == h))
        return TokenInfo(row.id, row.user_id, row.can_write) if row else None


def _touch(token_id: int) -> None:
    now = time.monotonic()
    with _lock:
        if now - _touched.get(token_id, -TOUCH_EVERY) < TOUCH_EVERY:
            return
        _touched[token_id] = now
    from app.db.session import get_sessionmaker

    with get_sessionmaker()() as s:
        row = s.get(ApiToken, token_id)
        if row is not None:
            row.last_used_at = utcnow()
            s.commit()


def verify(token: str | None) -> TokenInfo | None:
    """令牌有效时返回令牌信息：存在、所属用户存在且未停用。"""
    if not token or not token.startswith(PREFIX) or len(token) > 100:
        return None
    h, now = _hash(token), time.monotonic()
    with _lock:
        hit = _cache.get(h)
    if hit and now - hit[0] < CACHE_TTL:
        info = hit[1]
    else:
        info = _lookup(h)
        with _lock:
            _cache[h] = (now, info)
    if info is None:
        return None
    st = core.user_state(info.user_id)
    if not st.exists or st.disabled:
        return None
    _touch(info.token_id)
    return info


def clear_cache() -> None:
    with _lock:
        _cache.clear()
        _touched.clear()
