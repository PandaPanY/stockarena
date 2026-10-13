from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

from alembic import command
from sqlalchemy import Column, Integer, MetaData, Table, create_engine, inspect, select

from app.db.migrate import alembic_config, current_revision, upgrade_to_head
from app.db.session import build_engine
from app.db.types import DecimalString, UTCDateTime


def test_custom_types_roundtrip():
    engine = create_engine("sqlite://")
    meta = MetaData()
    t = Table(
        "t", meta,
        Column("id", Integer, primary_key=True),
        Column("amount", DecimalString()),
        Column("ts", UTCDateTime()),
    )
    meta.create_all(engine)
    beijing = timezone(timedelta(hours=8))
    with engine.begin() as conn:
        conn.execute(t.insert().values(
            id=1,
            amount=Decimal("12345678901234.123456789"),
            ts=datetime(2026, 10, 1, 9, 30, tzinfo=beijing),
        ))
        row = conn.execute(select(t)).one()
    assert row.amount == Decimal("12345678901234.123456789")
    assert isinstance(row.amount, Decimal)
    assert row.ts == datetime(2026, 10, 1, 1, 30, tzinfo=UTC)
    assert row.ts.tzinfo is UTC


def test_migrations_upgrade_and_downgrade(tmp_path):
    url = f"sqlite:///{(tmp_path / 'm.db').as_posix()}"
    upgrade_to_head(url)
    engine = build_engine(url)
    try:
        assert "users" in inspect(engine).get_table_names()
        assert current_revision(engine) is not None
        command.downgrade(alembic_config(url), "base")
        assert "users" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_current_revision_none_before_migration(tmp_path):
    engine = build_engine(f"sqlite:///{(tmp_path / 'empty.db').as_posix()}")
    try:
        assert current_revision(engine) is None
    finally:
        engine.dispose()


def test_sqlite_pragmas(tmp_path):
    engine = build_engine(f"sqlite:///{(tmp_path / 'p.db').as_posix()}")
    try:
        with engine.connect() as conn:
            assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
            assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
    finally:
        engine.dispose()


def test_registration_migration_keeps_existing_installs_open(tmp_path):
    """已设置管理员密码的现有安装升级后保持开放注册；新安装默认关闭。"""
    from alembic import command
    from sqlalchemy import create_engine, text

    for configured in (True, False):
        url = f"sqlite:///{(tmp_path / f'r{configured}.db').as_posix()}"
        command.upgrade(alembic_config(url), "f6a9c3e4b2d5")
        eng = create_engine(url)
        with eng.begin() as conn:
            # 初始迁移已建好 id 1 的默认用户
            conn.execute(text("UPDATE users SET password_hash = :h WHERE id = 1"),
                         {"h": "scrypt$x" if configured else None})
        command.upgrade(alembic_config(url), "head")
        with eng.connect() as conn:
            row = conn.execute(text("SELECT value FROM app_settings WHERE key = 'auth.registration'")).first()
        eng.dispose()
        assert (row is not None) is configured
        if configured:
            assert '"open": true' in row[0]
