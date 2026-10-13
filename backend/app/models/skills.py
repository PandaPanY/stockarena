"""技能（Skill）：外部的「说明 + 脚本 / 程序」包，经用户审批后安装，在隔离环境中供 AI 按需使用。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.timeutils import utcnow
from app.db.types import UTCDateTime
from app.models.base import Base, TimestampMixin, UserOwnedMixin


class SkillRow(TimestampMixin, UserOwnedMixin, Base):
    """一个技能包。

    status：pending 待审批 / installing 安装中 / installed 已安装 / failed 安装失败 / rejected 已拒绝 / removed 已卸载。
    """

    __tablename__ = "skills"

    name: Mapped[str] = mapped_column(String(64), index=True)           # SKILL.md 中的 name（目录名）
    display_name: Mapped[str] = mapped_column(String(128), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    version: Mapped[str] = mapped_column(String(32), default="")
    source_type: Mapped[str] = mapped_column(String(16))                 # github / upload / market
    source_url: Mapped[str] = mapped_column(Text, default="")
    market_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)         # 已安装后可停用（AI 看不到、不能调用）
    # 审阅信息：文件清单（路径、大小、类型、sha256）、脚本 / 程序、文中出现的域名、风险提示
    manifest: Mapped[dict] = mapped_column(JSON, default=dict)
    # 运行策略（用户在审批时确认）：允许访问的域名、AI 可调用的命令入口、仅限用户的子命令、安装命令
    allow_domains: Mapped[list] = mapped_column(JSON, default=list)
    commands: Mapped[list] = mapped_column(JSON, default=list)
    user_only: Mapped[list] = mapped_column(JSON, default=list)
    setup_command: Mapped[str] = mapped_column(Text, default="")
    package_sha256: Mapped[str] = mapped_column(String(64), default="")
    install_log: Mapped[str] = mapped_column(Text, default="")
    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    request_id: Mapped[int | None] = mapped_column(Integer, nullable=True)   # 来自 AI 的安装申请


class SkillCallRow(Base):
    """技能命令的调用记录（审计）：谁调用（AI / 用户）、命令、结果、被拦截的域名。"""

    __tablename__ = "skill_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    skill_id: Mapped[int] = mapped_column(ForeignKey("skills.id"), index=True)
    caller: Mapped[str] = mapped_column(String(32))                      # user / ai:review / ai:research / setup
    context: Mapped[str] = mapped_column(String(128), default="")        # 如 account:31 / 2026-10-08
    argv: Mapped[list] = mapped_column(JSON, default=list)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    seconds: Mapped[float] = mapped_column(default=0.0)
    output: Mapped[str] = mapped_column(Text, default="")               # 截断保存
    blocked: Mapped[list] = mapped_column(JSON, default=list)            # 被代理拦截的域名
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)


class SkillMarketRow(TimestampMixin, UserOwnedMixin, Base):
    """技能市场（第 3 步）：kind=github 时为一个包含多个技能目录的 GitHub 仓库。"""

    __tablename__ = "skill_markets"

    name: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16), default="github")
    url: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    index: Mapped[list] = mapped_column(JSON, default=list)             # 缓存的技能目录：[{name, description, path}]
    indexed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")


class SkillRequestRow(TimestampMixin, UserOwnedMixin, Base):
    """AI 发起的安装申请（第 4 步）：用户批准后才会安装。status：pending / approved / rejected / failed。"""

    __tablename__ = "skill_requests"

    requester: Mapped[str] = mapped_column(String(64))                   # 如 ai:review
    reason: Mapped[str] = mapped_column(Text, default="")
    query: Mapped[str] = mapped_column(Text, default="")
    source_url: Mapped[str] = mapped_column(Text, default="")
    market_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    skill_id: Mapped[int | None] = mapped_column(Integer, nullable=True)   # 已暂存待审阅的技能
    status: Mapped[str] = mapped_column(String(16), default="pending")
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
