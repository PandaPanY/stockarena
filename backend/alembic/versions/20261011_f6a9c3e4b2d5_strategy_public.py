"""public strategies (multi-user)

Revision ID: f6a9c3e4b2d5
Revises: e5f8b2d3a1c4
Create Date: 2026-10-11 17:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f6a9c3e4b2d5'
down_revision: str | Sequence[str] | None = 'e5f8b2d3a1c4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('quant_strategies', schema=None) as batch_op:
        batch_op.add_column(sa.Column('is_public', sa.Boolean(), server_default='0', nullable=False))
    # 管理员在用的风格策略库（由 AI 持续优化的那几条）设为公共；自定义、另存的和已归档的不公开
    op.execute("UPDATE quant_strategies SET is_public = 1 WHERE user_id = 1 AND status = 'active' "
               "AND style != 'custom' AND origin IS NULL")


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('quant_strategies', schema=None) as batch_op:
        batch_op.drop_column('is_public')
