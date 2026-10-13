from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.db.session import reset_engine


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    """每个测试使用独立的数据目录与数据库，不读取本机 config.yaml / .env。"""
    monkeypatch.setenv("SA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SA_CONFIG_FILE", str(tmp_path / "missing.yaml"))
    monkeypatch.setenv("SA_ENV", "test")
    monkeypatch.setenv("SA_AUTO_RUN", "false")
    monkeypatch.setenv("SA_AUTH_ENABLED", "false")
    monkeypatch.delenv("SA_DATABASE_URL", raising=False)
    get_settings.cache_clear()
    reset_engine()
    yield get_settings()
    reset_engine()
    get_settings.cache_clear()
    # 关闭文件日志句柄，否则 Windows 上临时目录无法清理
    root = logging.getLogger()
    for handler in list(root.handlers):
        handler.close()
        root.removeHandler(handler)


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c
