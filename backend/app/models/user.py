from __future__ import annotations

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

ADMIN, USER = "admin", "user"


class User(TimestampMixin, Base):
    __tablename__ = "users"

    # 登录用户名（不区分大小写唯一，见 app/auth/core.py）；管理员默认为 admin
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    # 登录密码的 scrypt 哈希；为空表示还没设置密码（管理员首次打开页面时设置）
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 登录凭证的代数：修改密码或「退出所有设备」时加 1，之前签发的凭证全部失效
    session_gen: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # admin：管理员（公共策略、数据维护、技能、用户管理）；user：普通用户（只能看到自己的数据与公共策略）
    role: Mapped[str] = mapped_column(String(16), nullable=False, default=USER, server_default=USER)
    # 管理员停用的账户不能登录，已签发的凭证立即失效
    disabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
