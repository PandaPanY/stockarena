"""AI 洞察：AI 为实时跟踪的策略 / 组合写的每周点评与偏差分析（app/insight）。"""

from __future__ import annotations

from datetime import date

from sqlalchemy import JSON, Date, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UserOwnedMixin


class AiNoteRow(TimestampMixin, UserOwnedMixin, Base):
    """一篇 AI 写的点评或分析。

    kind：weekly 每周点评 / deviation 偏差分析；ref_kind + ref_id 指向策略或组合（ref_name 保存当时的名称）。
    material 是交给模型的数据（收益、持仓贡献、交易、健康度等），便于核对模型说的是否有依据。
    status：ok / error（模型调用失败时 content 为空、error 说明原因）。
    """

    __tablename__ = "ai_notes"

    kind: Mapped[str] = mapped_column(String(16), index=True)
    ref_kind: Mapped[str] = mapped_column(String(16))
    ref_id: Mapped[int] = mapped_column(Integer)
    ref_name: Mapped[str] = mapped_column(String(64), default="")
    account_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    period_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    period_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    content: Mapped[str] = mapped_column(Text, default="")
    material: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(8), default="ok")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
