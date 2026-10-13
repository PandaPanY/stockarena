"""strategy origin (fork)

Revision ID: d4e7a1c2f9b0
Revises: b683049c6526
Create Date: 2026-10-11 10:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd4e7a1c2f9b0'
down_revision: str | Sequence[str] | None = 'b683049c6526'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('quant_strategies', schema=None) as batch_op:
        batch_op.add_column(sa.Column('origin', sa.JSON(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('quant_strategies', schema=None) as batch_op:
        batch_op.drop_column('origin')
