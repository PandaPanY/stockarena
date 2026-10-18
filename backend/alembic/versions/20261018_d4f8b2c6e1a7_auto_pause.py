"""quant strategies: auto_pause (nightly optimization paused when upgrade gate is too strict)

Revision ID: d4f8b2c6e1a7
Revises: c3e7a9b1d5f2
Create Date: 2026-10-18 10:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd4f8b2c6e1a7'
down_revision: str | Sequence[str] | None = 'c3e7a9b1d5f2'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('quant_strategies', schema=None) as batch_op:
        batch_op.add_column(sa.Column('auto_pause', sa.JSON(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('quant_strategies', schema=None) as batch_op:
        batch_op.drop_column('auto_pause')
