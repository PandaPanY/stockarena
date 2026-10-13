"""user roles (multi-user)

Revision ID: e5f8b2d3a1c4
Revises: d4e7a1c2f9b0
Create Date: 2026-10-11 16:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e5f8b2d3a1c4'
down_revision: str | Sequence[str] | None = 'd4e7a1c2f9b0'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('role', sa.String(length=16), server_default='user', nullable=False))
        batch_op.add_column(sa.Column('disabled', sa.Boolean(), server_default='0', nullable=False))
    # 原来的唯一用户（id 1）成为管理员，用户名改为 admin
    op.execute("UPDATE users SET role = 'admin' WHERE id = 1")
    op.execute("UPDATE users SET name = 'admin' WHERE id = 1 AND name = 'default'")


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('disabled')
        batch_op.drop_column('role')
