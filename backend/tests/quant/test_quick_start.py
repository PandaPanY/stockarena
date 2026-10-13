"""快速开始：先准备快速体验股票池，完成后自动在后台准备完整数据；完整数据没好之前，数据状态里带上快速数据的情况。"""

from __future__ import annotations

import time

from app.quant import data, service, tasks


def _wait(name: str, timeout: float = 10.0) -> dict:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        st = tasks.status.get(name) or {}
        if st and not st.get("running"):
            return st
        time.sleep(0.05)
    raise AssertionError(f"任务 {name} 没有结束")


def test_quick_start_then_full(client, monkeypatch):
    calls = []

    def fake_prepare(universe, start, progress=None):
        calls.append(universe)
        return {"message": f"数据准备完成：{universe}"}

    monkeypatch.setattr(data, "prepare", fake_prepare)
    monkeypatch.setattr(data, "coverage", lambda s, u: {"ready": u == service.QUICK_UNIVERSE, "with_data": 60})
    tasks.status.clear()

    st = client.get("/api/quant/data").json()
    assert st["coverage"]["ready"] is False and st["quick"]["universe"] == service.QUICK_UNIVERSE
    assert st["quick"]["coverage"]["ready"] is True

    assert client.post("/api/quant/data/quick-start").status_code == 200
    q = _wait(f"prepare:{service.QUICK_UNIVERSE}")
    assert "完整数据已在后台开始准备" in q["message"]
    _wait(f"prepare:{service.DEFAULT_UNIVERSE}")
    assert calls == [service.QUICK_UNIVERSE, service.DEFAULT_UNIVERSE]
    tasks.status.clear()


def test_quick_universe_flag(client):
    us = {u["name"]: u for u in client.get("/api/meta/universes").json()}
    assert us[service.QUICK_UNIVERSE]["quick"] is True and us[service.QUICK_UNIVERSE]["history"] is False
    assert us[service.DEFAULT_UNIVERSE]["quick"] is False
