"""registration switch: keep registration open on existing installs

Revision ID: a8c1d5f7e3b9
Revises: f6a9c3e4b2d5
Create Date: 2026-10-13 10:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.core.timeutils import utcnow
from app.db.types import UTCDateTime

# revision identifiers, used by Alembic.
revision: str = 'a8c1d5f7e3b9'
down_revision: str | Sequence[str] | None = 'f6a9c3e4b2d5'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KEY = 'auth.registration'


def upgrade() -> None:
    """新安装默认不开放注册；已经设置过管理员密码的现有安装保持原来的开放注册。"""
    conn = op.get_bind()
    configured = conn.execute(sa.text("SELECT 1 FROM users WHERE id = 1 AND password_hash IS NOT NULL")).first()
    exists = conn.execute(sa.text("SELECT 1 FROM app_settings WHERE key = :k"), {"k": KEY}).first()
    if configured and not exists:
        settings = sa.table('app_settings', sa.column('key', sa.String), sa.column('value', sa.JSON),
                            sa.column('updated_at', UTCDateTime()))
        op.bulk_insert(settings, [{'key': KEY, 'value': {'open': True}, 'updated_at': utcnow()}])


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(sa.text("DELETE FROM app_settings WHERE key = :k").bindparams(k=KEY))
