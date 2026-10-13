"""登录与账户接口：状态、管理员首次设置密码（需首次设置码）、开放注册（管理员开关）、登录、退出、修改密码、退出所有设备、用户管理（管理员）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_user_id, require_admin
from app.auth import core
from app.auth.middleware import cookie_from_scope
from app.core.config import get_settings
from app.db.session import get_db
from app.models import DEFAULT_USER_ID, User
from app.models.user import ADMIN, USER
from app.skills.gate import skills_enabled

router = APIRouter(prefix="/api/auth", tags=["auth"])


class PasswordIn(BaseModel):
    password: str = Field(max_length=core.PASSWORD_MAX)


class SetupIn(PasswordIn):
    # 首次设置码：在服务启动日志或 data/setup-code.txt 中
    setup_code: str = Field("", max_length=64)


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(max_length=core.PASSWORD_MAX)


class ChangePasswordIn(BaseModel):
    old_password: str = Field(max_length=core.PASSWORD_MAX)
    new_password: str = Field(max_length=core.PASSWORD_MAX)


def _set_cookie(resp: Response, user_id: int, gen: int) -> None:
    resp.set_cookie(core.COOKIE_NAME, core.issue_token(user_id, gen), max_age=core.SESSION_DAYS * 86400,
                    path="/", httponly=True, samesite="lax")


def _clear_cookie(resp: Response) -> None:
    resp.delete_cookie(core.COOKIE_NAME, path="/", httponly=True, samesite="lax")


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def user_dict(u: User) -> dict:
    return {"id": u.id, "name": u.name, "role": u.role, "disabled": bool(u.disabled),
            "created_at": u.created_at.isoformat() if u.created_at else None}


@router.get("/status")
def status(request: Request, db: Session = Depends(get_db)) -> dict:
    enabled = get_settings().auth_enabled
    uid = core.token_valid(cookie_from_scope(request.scope)) if enabled else DEFAULT_USER_ID
    user = db.get(User, uid) if uid is not None else None
    configured = core.admin_configured()
    if enabled and not configured:
        core.ensure_setup_code()            # 没有设置码时生成（例如命令行重置密码之后），并写进日志
    return {"enabled": enabled, "configured": configured, "authenticated": uid is not None,
            "registration_open": core.registration_open(db), "features": {"skills": skills_enabled()},
            "source_url": get_settings().source_url,
            "user": user_dict(user) if user else None}


@router.post("/setup")
def setup(body: SetupIn, request: Request, response: Response, db: Session = Depends(get_db)) -> dict:
    """首次设置管理员密码（只在管理员还没有密码时可用），需要填写首次设置码，设置后直接登录。用户名为 admin。"""
    user = core.get_admin(db)
    if user.password_hash:
        raise HTTPException(409, "已经设置过密码，请直接登录")
    ip = _client_ip(request)
    wait = core.limiter.wait_seconds(ip)
    if wait:
        raise HTTPException(429, f"错误次数过多，请 {max(1, round(wait / 60))} 分钟后再试")
    if not core.check_setup_code(body.setup_code):
        core.limiter.fail(ip)
        raise HTTPException(403, "首次设置码不正确：请在服务启动日志或数据目录下的 setup-code.txt 中查看")
    try:
        core.check_password_rule(body.password)
    except core.AuthError as e:
        raise HTTPException(400, str(e)) from None
    user.password_hash = core.hash_password(body.password)
    user.session_gen += 1
    db.commit()
    core.invalidate(user.id)
    core.clear_setup_code()
    core.limiter.reset(ip)
    _set_cookie(response, user.id, user.session_gen)
    return {"ok": True, "user": user_dict(user)}


@router.post("/register")
def register(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)) -> dict:
    """开放注册（管理员打开后可用）：创建普通用户并直接登录。同一来源 IP 每小时最多注册 5 个账户。"""
    if not core.admin_configured():
        raise HTTPException(409, "管理员还没有完成首次设置")
    if not core.registration_open(db):
        raise HTTPException(403, "管理员没有开放注册，请联系管理员")
    ip = _client_ip(request)
    if core.register_limiter.wait_seconds(ip):
        raise HTTPException(429, "注册过于频繁，请稍后再试")
    name = body.username.strip()
    try:
        core.check_username_rule(name)
        core.check_password_rule(body.password)
    except core.AuthError as e:
        raise HTTPException(400, str(e)) from None
    if core.find_user(db, name) is not None:
        raise HTTPException(409, "用户名已被使用")
    user = User(name=name, password_hash=core.hash_password(body.password), session_gen=1, role=USER)
    db.add(user)
    db.commit()
    core.register_limiter.fail(ip)          # 成功的注册也计数
    _set_cookie(response, user.id, user.session_gen)
    return {"ok": True, "user": user_dict(user)}


@router.post("/login")
def login(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)) -> dict:
    ip = _client_ip(request)
    wait = core.limiter.wait_seconds(ip)
    if wait:
        raise HTTPException(429, f"密码错误次数过多，请 {max(1, round(wait / 60))} 分钟后再试")
    user = core.find_user(db, body.username)
    if user is None or not core.verify_password(body.password, user.password_hash):
        core.limiter.fail(ip)
        raise HTTPException(401, "用户名或密码错误")
    if user.disabled:
        raise HTTPException(403, "账户已停用，请联系管理员")
    core.limiter.reset(ip)
    _set_cookie(response, user.id, user.session_gen)
    return {"ok": True, "user": user_dict(user)}


@router.post("/logout")
def logout(response: Response) -> dict:
    _clear_cookie(response)
    return {"ok": True}


@router.get("/me")
def me(user_id: int = Depends(current_user_id), db: Session = Depends(get_db)) -> dict:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(404, "用户不存在")
    return user_dict(user)


@router.post("/password")
def change_password(body: ChangePasswordIn, request: Request, response: Response,
                    user_id: int = Depends(current_user_id), db: Session = Depends(get_db)) -> dict:
    """修改密码：其他设备上的登录随之失效，当前设备保持登录。"""
    ip = _client_ip(request)
    if core.limiter.wait_seconds(ip):
        raise HTTPException(429, "密码错误次数过多，请稍后再试")
    user = db.get(User, user_id)
    if user is None or not core.verify_password(body.old_password, user.password_hash):
        core.limiter.fail(ip)
        raise HTTPException(400, "原密码不正确")
    try:
        core.check_password_rule(body.new_password)
    except core.AuthError as e:
        raise HTTPException(400, str(e)) from None
    user.password_hash = core.hash_password(body.new_password)
    user.session_gen += 1
    db.commit()
    core.invalidate(user.id)
    _set_cookie(response, user.id, user.session_gen)
    return {"ok": True}


@router.post("/logout-all")
def logout_all(response: Response, user_id: int = Depends(current_user_id), db: Session = Depends(get_db)) -> dict:
    """退出所有设备（包括当前设备）。"""
    user = db.get(User, user_id)
    if user is not None:
        user.session_gen += 1
        db.commit()
        core.invalidate(user.id)
    _clear_cookie(response)
    return {"ok": True}


# ---------------------------------------------------------------- 用户管理（管理员）


class RegistrationIn(BaseModel):
    open: bool


@router.get("/registration")
def get_registration(_: int = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    return {"open": core.registration_open(db)}


@router.put("/registration")
def put_registration(body: RegistrationIn, _: int = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    """开放 / 关闭注册（关闭后已有用户不受影响）。"""
    core.set_registration_open(db, body.open)
    db.commit()
    return {"open": body.open}


@router.get("/users")
def list_users(_: int = Depends(require_admin), db: Session = Depends(get_db)) -> list[dict]:
    return [user_dict(u) for u in db.scalars(select(User).order_by(User.id))]


class UserPatch(BaseModel):
    disabled: bool | None = None


@router.patch("/users/{uid}")
def patch_user(uid: int, body: UserPatch, admin_id: int = Depends(require_admin),
               db: Session = Depends(get_db)) -> dict:
    """停用 / 启用账户。停用后已登录的设备立即失效；不能停用管理员自己。"""
    user = db.get(User, uid)
    if user is None:
        raise HTTPException(404, "用户不存在")
    if body.disabled is not None:
        if user.role == ADMIN and body.disabled:
            raise HTTPException(400, "不能停用管理员")
        user.disabled = body.disabled
        if body.disabled:
            user.session_gen += 1
    db.commit()
    core.invalidate(user.id)
    return user_dict(user)


class ResetPasswordIn(BaseModel):
    new_password: str = Field(max_length=core.PASSWORD_MAX)


@router.post("/users/{uid}/password")
def reset_user_password(uid: int, body: ResetPasswordIn, _: int = Depends(require_admin),
                        db: Session = Depends(get_db)) -> dict:
    """管理员为用户设置新密码（用户忘记密码时）；该用户所有设备退出登录。"""
    user = db.get(User, uid)
    if user is None:
        raise HTTPException(404, "用户不存在")
    try:
        core.check_password_rule(body.new_password)
    except core.AuthError as e:
        raise HTTPException(400, str(e)) from None
    user.password_hash = core.hash_password(body.new_password)
    user.session_gen += 1
    db.commit()
    core.invalidate(user.id)
    return {"ok": True}
