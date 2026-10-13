from __future__ import annotations

from sqlalchemy import select

from app.db.session import get_sessionmaker
from app.models import DEFAULT_USER_ID, User


def test_health_ok(client):
    resp = client.get("/api/system/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["env"] == "test"
    assert body["database"]["ok"] is True
    assert body["database"]["dialect"] == "sqlite"
    assert body["database"]["revision"] is not None
    assert body["scheduler_running"] is True
    assert {m["market"] for m in body["markets"]} == {"CN", "HK", "US"}


def test_startup_migration_creates_default_user(client):
    with get_sessionmaker()() as session:
        user = session.scalar(select(User).where(User.id == DEFAULT_USER_ID))
    assert user is not None
    assert user.name == "admin" and user.role == "admin"      # 原来的唯一用户成为管理员


def test_jobs_endpoint_empty(client):
    resp = client.get("/api/system/jobs")
    assert resp.status_code == 200
    assert resp.json() == []


def test_cors_allows_frontend_dev_origin(client):
    resp = client.get("/api/system/health", headers={"Origin": "http://localhost:15173"})
    assert resp.headers.get("access-control-allow-origin") == "http://localhost:15173"


def test_serves_frontend_dist(settings, tmp_path, monkeypatch):
    """部署模式：后端托管前端打包目录，页面路由返回 index.html，接口路径不被页面覆盖。"""
    from fastapi.testclient import TestClient

    from app.core.config import get_settings
    from app.main import create_app

    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>首页</html>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    monkeypatch.setenv("SA_FRONTEND_DIST", str(dist))
    get_settings.cache_clear()
    with TestClient(create_app()) as c:
        assert "首页" in c.get("/").text
        assert "首页" in c.get("/lab/8").text                       # 前端路由
        assert c.get("/assets/app.js").text == "console.log(1)"
        assert c.get("/api/system/health").json()["status"] in ("ok", "degraded")
        assert c.get("/api/not-exist").status_code == 404
        assert "首页" in c.get("/../secret.key").text                # 不能读出目录之外的文件
