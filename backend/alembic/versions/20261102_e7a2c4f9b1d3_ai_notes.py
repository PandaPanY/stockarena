"""ai notes (weekly review / deviation analysis)

Revision ID: e7a2c4f9b1d3
Revises: d4f8b2c6e1a7
Create Date: 2026-11-02 23:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.types

# revision identifiers, used by Alembic.
revision: str = 'e7a2c4f9b1d3'
down_revision: str | Sequence[str] | None = 'd4f8b2c6e1a7'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'ai_notes',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('created_at', app.db.types.UTCDateTime(), nullable=False),
        sa.Column('updated_at', app.db.types.UTCDateTime(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('ref_kind', sa.String(length=16), nullable=False),
        sa.Column('ref_id', sa.Integer(), nullable=False),
        sa.Column('ref_name', sa.String(length=64), nullable=False),
        sa.Column('account_id', sa.Integer(), nullable=True),
        sa.Column('period_start', sa.Date(), nullable=True),
        sa.Column('period_end', sa.Date(), nullable=True),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('material', sa.JSON(), nullable=False),
        sa.Column('status', sa.String(length=8), nullable=False),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('model', sa.String(length=255), nullable=True),
        sa.Column('tokens_in', sa.Integer(), nullable=False),
        sa.Column('tokens_out', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('ai_notes', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_ai_notes_kind'), ['kind'], unique=False)
        batch_op.create_index(batch_op.f('ix_ai_notes_user_id'), ['user_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('ai_notes', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_ai_notes_user_id'))
        batch_op.drop_index(batch_op.f('ix_ai_notes_kind'))
    op.drop_table('ai_notes')
