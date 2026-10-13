"""技能总开关：新安装默认关闭；已安装过技能的旧安装保持开启；配置写明时以配置为准。"""

from __future__ import annotations

import pytest

from app.core.config import get_settings
from app.db.session import get_sessionmaker
from app.models.skills import SkillRow
from app.skills import gate, tools


@pytest.fixture(autouse=True)
def _fresh_gate():
    gate.reset_cache()
    yield
    gate.reset_cache()


def _add_installed_skill() -> None:
    with get_sessionmaker()() as s:
        s.add(SkillRow(user_id=1, name="demo", source_type="upload", status="installed", enabled=True))
        s.commit()


def test_disabled_by_default_on_fresh_install(client):
    assert gate.skills_enabled() is False
    assert client.get("/api/auth/status").json()["features"] == {"skills": False}
    r = client.get("/api/skills")
    assert r.status_code == 409 and "skills_enabled" in r.json()["detail"]
    with get_sessionmaker()() as s:
        assert tools.catalog(s) == [] and tools.market_enabled(s) is False


def test_auto_on_when_skills_already_installed(client):
    _add_installed_skill()
    gate.reset_cache()
    assert gate.skills_enabled() is True
    with get_sessionmaker()() as s:
        assert [x["name"] for x in tools.catalog(s)] == ["demo"]


def test_explicit_setting_wins(client, monkeypatch):
    _add_installed_skill()
    monkeypatch.setenv("SA_SKILLS_ENABLED", "false")
    get_settings.cache_clear()
    assert gate.skills_enabled() is False
    with get_sessionmaker()() as s:
        assert tools.catalog(s) == []
    monkeypatch.setenv("SA_SKILLS_ENABLED", "true")
    get_settings.cache_clear()
    assert gate.skills_enabled() is True
