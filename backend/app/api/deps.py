"""API 公共依赖：当前用户与管理员权限。"""

from __future__ import annotations

from fastapi import Depends, HTTPException
from starlette.requests import HTTPConnection

from app.models import DEFAULT_USER_ID


def current_user_id(conn: HTTPConnection) -> int:
    """当前登录用户（访问保护中间件校验凭证后写入）。未启用登录（测试、本机调试）时为管理员。"""
    uid = getattr(conn.state, "user_id", None)
    return uid if uid is not None else DEFAULT_USER_ID


def require_admin(user_id: int = Depends(current_user_id)) -> int:
    """只有管理员可用的接口：公共策略的修改、数据维护、技能、用户管理等。"""
    from app.auth import core

    if not core.is_admin(user_id):
        raise HTTPException(403, "需要管理员权限")
    return user_id
