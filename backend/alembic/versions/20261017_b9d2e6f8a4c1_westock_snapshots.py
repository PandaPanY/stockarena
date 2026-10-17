"""westock snapshots (daily consensus / score)

Revision ID: b9d2e6f8a4c1
Revises: a8c1d5f7e3b9
Create Date: 2026-10-17 16:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.types

# revision identifiers, used by Alembic.
revision: str = 'b9d2e6f8a4c1'
down_revision: str | Sequence[str] | None = 'a8c1d5f7e3b9'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'westock_snapshots',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('as_of', sa.Date(), nullable=False),
        sa.Column('symbol', sa.String(length=20), nullable=False),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('status', sa.String(length=8), nullable=False),
        sa.Column('data', sa.JSON(), nullable=False),
        sa.Column('raw', sa.Text(), nullable=False),
        sa.Column('fetched_at', app.db.types.UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('as_of', 'symbol', 'kind'),
    )
    with op.batch_alter_table('westock_snapshots', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_westock_snapshots_as_of'), ['as_of'], unique=False)
        batch_op.create_index(batch_op.f('ix_westock_snapshots_symbol'), ['symbol'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('westock_snapshots', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_westock_snapshots_symbol'))
        batch_op.drop_index(batch_op.f('ix_westock_snapshots_as_of'))
    op.drop_table('westock_snapshots')
