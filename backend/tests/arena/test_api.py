from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.arena import deps, lifecycle
from tests.arena.conftest import FakeProvider


@pytest.fixture
def api(client, monkeypatch):
    monkeypatch.setattr(deps, "cn_provider", lambda: FakeProvider())
    deps.secret_box.cache_clear()
    # 固定「现在」为 2026-09-30 16:00（北京时间），保证首轮决策日期可预期
    monkeypatch.setattr("app.api.tracks.utcnow", lambda: datetime(2026, 9, 30, 8, 0, tzinfo=UTC))
    monkeypatch.setattr("app.api.tracks.tasks.spawn_cycle", lambda *a, **k: True)
    return client


def test_provider_agent_track_flow(api):
    presets = api.get("/api/providers/presets").json()
    assert any(p["protocol"] == "anthropic" for p in presets)

    r = api.post("/api/providers", json={"name": "本地Qwen", "protocol": "openai_compatible",
                                         "base_url": "http://10.0.0.1:9100/v1", "api_key": "sk-secret-123456",
                                         "default_model": "qwen"})
    assert r.status_code == 200, r.text
    prov = r.json()
    assert prov["api_key_masked"] == "****3456" and "sk-secret" not in r.text

    # 更新时 api_key 为 null 表示保持不变
    r = api.put(f"/api/providers/{prov['id']}", json={**prov, "api_key": None, "name": "本地Qwen2"})
    assert r.json()["has_api_key"] and r.json()["name"] == "本地Qwen2"
    assert api.post("/api/providers", json={"name": "x", "protocol": "bad"}).status_code == 400

    templates = api.get("/api/meta/prompt-templates").json()
    assert templates[0]["name"] == "default"
    r = api.post("/api/agents", json={"name": "Qwen27B", "provider_id": prov["id"], "params": {"temperature": 0.3},
                                      "persona": "价值投资"})
    agent = r.json()
    assert agent["kind"] == "llm" and agent["model"] == "qwen"
    agents = api.get("/api/agents").json()
    baselines = [a for a in agents if a["kind"] == "baseline"]
    assert len(baselines) == 3
    assert api.delete(f"/api/providers/{prov['id']}").status_code == 400   # 被选手使用中

    rulesets = api.get("/api/meta/rulesets").json()
    universes = {u["name"]: u for u in api.get("/api/meta/universes").json()}
    assert universes["cn_sse50_etf"]["benchmark"] == "000016.SH"
    r = api.post("/api/tracks", json={"name": "A股首赛", "universe_name": "cn_sse50_etf",
                                      "rule_set_id": rulesets[0]["id"], "initial_cash": "100000",
                                      "agent_ids": [agent["id"], baselines[0]["id"]]})
    assert r.status_code == 200, r.text
    track = r.json()
    assert track["status"] == "draft" and len(track["accounts"]) == 2

    r = api.post(f"/api/tracks/{track['id']}/start")
    assert r.status_code == 200, r.text
    started = r.json()
    assert started["status"] == "running" and started["start_date"] == "2026-09-30"
    assert started["symbol_count"] == 13

    # 启动后不能再报名
    r = api.post(f"/api/tracks/{track['id']}/accounts", json={"agent_id": baselines[1]["id"]})
    assert r.status_code == 400 and "启动后" in r.json()["detail"]

    board = api.get(f"/api/tracks/{track['id']}/leaderboard").json()
    assert len(board) == 2 and {b["rank"] for b in board} == {1, 2}
    assert api.get(f"/api/tracks/{track['id']}/nav").json()["benchmark"]["name"] == "上证50"
    assert api.get("/api/decisions", params={"track_id": track["id"]}).json() == {"items": [], "more": False}
    assert api.post(f"/api/tracks/{track['id']}/pause").json()["status"] == "paused"
    assert api.post(f"/api/tracks/{track['id']}/pause").status_code == 400
    assert api.delete(f"/api/tracks/{track['id']}").status_code == 400


def test_lifecycle_status_errors():
    from app.models import TrackRow

    t = TrackRow(status="draft")
    with pytest.raises(lifecycle.ArenaError):
        lifecycle.set_status(t, "pause")


def test_personas(api):
    data = api.get("/api/meta/personas").json()
    names = {p["name"] for p in data}
    assert {"value", "momentum", "long_short"} <= names
    assert all(p["persona"] and p["title"] for p in data)
