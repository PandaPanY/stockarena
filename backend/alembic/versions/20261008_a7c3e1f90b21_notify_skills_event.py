"""notify skills event

Revision ID: a7c3e1f90b21
Revises: f2ca91081982
Create Date: 2026-10-08 18:00:00

"""
import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'a7c3e1f90b21'
down_revision: str | Sequence[str] | None = 'f2ca91081982'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """已有的推送通道默认订阅新增的「技能申请」（AI 申请安装技能时提醒用户审批）。"""
    conn = op.get_bind()
    for cid, events in conn.execute(sa.text("SELECT id, events FROM notify_channels")).all():
        lst = json.loads(events) if isinstance(events, str) else list(events or [])
        if "skills" not in lst:
            conn.execute(sa.text("UPDATE notify_channels SET events = :e WHERE id = :i"),
                         {"e": json.dumps([*lst, "skills"]), "i": cid})


def downgrade() -> None:
    pass
