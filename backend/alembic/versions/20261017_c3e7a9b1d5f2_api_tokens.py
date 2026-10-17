"""api tokens (MCP)

Revision ID: c3e7a9b1d5f2
Revises: b9d2e6f8a4c1
Create Date: 2026-10-17 20:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.types

# revision identifiers, used by Alembic.
revision: str = 'c3e7a9b1d5f2'
down_revision: str | Sequence[str] | None = 'b9d2e6f8a4c1'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'api_tokens',
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=64), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('prefix', sa.String(length=16), nullable=False),
        sa.Column('can_write', sa.Boolean(), server_default='0', nullable=False),
        sa.Column('last_used_at', app.db.types.UTCDateTime(), nullable=True),
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('created_at', app.db.types.UTCDateTime(), nullable=False),
        sa.Column('updated_at', app.db.types.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_api_tokens_user_id_users')),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_api_tokens')),
        sa.UniqueConstraint('token_hash', name=op.f('uq_api_tokens_token_hash')),
    )
    with op.batch_alter_table('api_tokens', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_api_tokens_user_id'), ['user_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('api_tokens', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_api_tokens_user_id'))
    op.drop_table('api_tokens')
