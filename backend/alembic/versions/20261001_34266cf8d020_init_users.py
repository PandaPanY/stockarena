"""init users

Revision ID: 34266cf8d020
Revises:
Create Date: 2026-10-01 23:13:57.812114

"""
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

import app.db.types

# revision identifiers, used by Alembic.
revision: str = '34266cf8d020'
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    users = op.create_table('users',
    sa.Column('name', sa.String(length=64), nullable=False),
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('created_at', app.db.types.UTCDateTime(), nullable=False),
    sa.Column('updated_at', app.db.types.UTCDateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_users')),
    sa.UniqueConstraint('name', name=op.f('uq_users_name'))
    )
    # 单用户模式的默认用户（DEFAULT_USER_ID = 1）
    now = datetime.now(UTC)
    op.bulk_insert(users, [{"id": 1, "name": "default", "created_at": now, "updated_at": now}])


def downgrade() -> None:
    op.drop_table('users')
