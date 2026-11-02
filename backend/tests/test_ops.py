"""运行保障：停机检测与错过的任务、数据库备份、每日完整性检查、运行状况接口。"""

from __future__ import annotations

import gzip
import sqlite3
from datetime import date, timedelta

import pytest

from app.core.config import get_settings
from app.core.timeutils import utcnow
from app.db.session import get_sessionmaker
from app.ops import backup, downtime, integrity


@pytest.fixture
def sent(monkeypatch):
    out = []
    from app.notify import service as notify

    monkeypatch.setattr(notify, "deliver", lambda msg: out.append(msg))
    return out


@pytest.fixture
def jobs():
    from app.scheduler import get_scheduler, register_jobs, shutdown_scheduler

    register_jobs()
    yield get_scheduler().get_jobs()
    shutdown_scheduler()


def test_short_restart_is_quiet_long_downtime_alerts(db, jobs, sent):
    downtime._file().write_text((utcnow() - timedelta(minutes=2)).isoformat(), encoding="utf-8")
    info = downtime.on_startup(jobs)
    assert info["minutes"] == 2 and not sent                     # 部署这类短重启：不提醒

    downtime._file().write_text((utcnow() - timedelta(days=2)).isoformat(), encoding="utf-8")
    info = downtime.on_startup(jobs)
    ids = {m["id"] for m in info["missed"]}
    assert "ops_backup" in ids and len(sent) == 1 and "服务中断" in sent[0].title
    assert "每日" in sent[0].body or "备份" in sent[0].body
    assert downtime.read_last() > utcnow() - timedelta(seconds=5)      # 启动时立即写心跳


def test_missed_jobs_lists_cron_fire_times(jobs):
    from datetime import datetime

    start = datetime(2026, 10, 26, 13, 0, tzinfo=downtime.UTC)          # 北京时间 21:00（周一）
    end = start + timedelta(hours=2)
    names = [i for i, _n, _t in downtime.missed_jobs(jobs, start, end)]
    assert "watch_daily" in names and "westock_snapshot_2200" in names and "ops_integrity" not in names


def test_backup_is_consistent_and_pruned(db, monkeypatch, tmp_path):
    extra = tmp_path / "offsite"
    monkeypatch.setenv("SA_BACKUP_KEEP", "2")
    monkeypatch.setenv("SA_BACKUP_DIR", str(extra))
    get_settings.cache_clear()
    folder = backup.local_dir()
    folder.mkdir(parents=True, exist_ok=True)
    for name in ("stockarena-20260101-0230.db.gz", "stockarena-20260102-0230.db.gz"):
        (folder / name).write_bytes(b"old")
    out = backup.run_backup()
    left = sorted(p.name for p in folder.glob("stockarena-*.db.gz"))
    assert len(left) == 2 and "stockarena-20260101-0230.db.gz" not in left and out["removed"]
    restored = tmp_path / "restored.db"
    with gzip.open(out["file"], "rb") as fi:
        restored.write_bytes(fi.read())
    con = sqlite3.connect(restored)
    try:
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert con.execute("select count(*) from alembic_version").fetchone()[0] == 1
    finally:
        con.close()
    from pathlib import Path

    assert (extra / Path(out["file"]).name).is_file() and out["extra"]


def test_integrity_check(db, sent, monkeypatch):
    from app.models.market import TradingDayRow

    monkeypatch.setattr(integrity, "DISK_MIN_FREE", 0.0)
    monkeypatch.setattr(integrity, "DISK_MIN_GB", 0)
    day = date(2026, 10, 30)
    with get_sessionmaker()() as s:
        s.add(TradingDayRow(market="CN", date=day))
        s.commit()
    r = integrity.check(day)
    assert not r["ok"] and any("备份" in p for p in r["problems"]) and len(sent) == 1
    import asyncio

    asyncio.run(backup.scheduled())
    r = integrity.check(day)
    assert r["ok"] and r["day"] == "2026-10-30"
    with get_sessionmaker()() as s:
        assert integrity.latest_trading_day(s, date(2026, 11, 2)) == day


def test_integrity_finds_missing_nav_and_failed_reviews(arena, sent, monkeypatch):  # noqa: F811
    from app.models import DecisionRow, RoundRow
    from app.quant import review
    from tests.quant.test_review import _setup

    monkeypatch.setattr(integrity, "DISK_MIN_FREE", 0.0)
    day = date(2026, 10, 30)
    with get_sessionmaker()() as s:
        pf, track, rule_acc, provider = _setup(s)
        ai = review.enable(s, pf, provider.id)
        track.mode, track.status = "live", "running"
        rnd = RoundRow(track_id=track.id, data_date=day, trigger="schedule", status="done")
        s.add(rnd)
        s.flush()
        s.add(DecisionRow(round_id=rnd.id, account_id=ai.id, status="baseline", parsed={"review": {"items": [
            {"symbol": "600000.SH", "decision": "approve", "failed": True, "reason": "未得到复核结果，按规则执行"}]}}))
        s.commit()
        probs = integrity._tracking(s, day) + integrity._reviews(s, day)
    assert any("没有 2026-10-30 的净值" in p for p in probs)
    assert any("全部复核失败" in p for p in probs)


def test_external_heartbeat(db, monkeypatch):
    import httpx

    calls = []

    class Resp:
        def raise_for_status(self):
            return None

    def fake_get(url, timeout):
        calls.append(url)
        if fail:
            raise httpx.ConnectError("连不上")
        return Resp()

    fail = False
    monkeypatch.setattr(downtime, "_last_ping", {})
    monkeypatch.setattr(downtime.httpx, "get", fake_get)
    assert downtime.ping() is None and not calls                          # 未配置：不报到
    assert downtime.ping_status()["host"] is None

    monkeypatch.setenv("SA_HEARTBEAT_URL", "https://hc-ping.com/abc-123")
    get_settings.cache_clear()
    r = downtime.ping()
    assert r["ok"] and calls == ["https://hc-ping.com/abc-123"]
    st = downtime.ping_status()
    assert st["host"] == "hc-ping.com" and "abc" not in str(st)          # 页面只显示主机名

    fail = True
    r = downtime.ping()
    assert not r["ok"] and "hc-ping.com" in r["error"] and r["ok_at"] == st["ok_at"]

    # 数据库不可用：不报到，让外部服务按超时报警
    fail = False
    import app.db.session as session

    def broken():
        raise RuntimeError("database is locked")

    monkeypatch.setattr(session, "get_sessionmaker", broken)
    r = downtime.ping()
    assert not r["ok"] and "数据库" in r["error"] and len(calls) == 2


def test_ops_api(client, db):
    r = client.get("/api/system/ops")
    assert r.status_code == 200
    body = r.json()
    assert {"downtime", "jobs", "integrity", "backup", "disk", "models", "failed_sends", "heartbeat"} <= set(body)
    assert body["disk"]["total"] > 0
    r = client.post("/api/system/ops/backup")
    assert r.status_code == 200 and r.json()["size"] > 0
    assert client.get("/api/system/ops").json()["backup"]["files"]


from tests.arena.conftest import arena  # noqa: E402, F401
