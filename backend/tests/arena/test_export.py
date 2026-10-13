"""导出与 HTML 报告（v1.0 子阶段 F）。"""

from __future__ import annotations

import csv
import io
from datetime import date

from app.arena import cycle
from tests.arena.conftest import cst
from tests.arena.test_replay import _replay_track


async def test_export_and_report(arena, client):
    track_id = _replay_track(date(2026, 9, 1), cst(2026, 10, 2, 10), end=date(2026, 9, 10))
    await cycle.run_replay(track_id)

    r = client.get(f"/api/tracks/{track_id}/export", params={"kind": "fills", "fmt": "csv"})
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    text = r.content.decode("utf-8")
    assert text.startswith("\ufeff")
    rows = list(csv.DictReader(io.StringIO(text.lstrip("\ufeff"))))
    assert rows and {"agent", "trade_date", "symbol", "side_label", "price"} <= set(rows[0])

    nav = client.get(f"/api/tracks/{track_id}/export", params={"kind": "nav", "fmt": "json"}).json()
    assert nav and nav[0]["date"] == "2026-09-01"
    decs = client.get(f"/api/tracks/{track_id}/export", params={"kind": "decisions", "fmt": "csv"}).text
    assert "analysis" in decs and "看好茅台" in decs
    assert client.get(f"/api/tracks/{track_id}/export", params={"kind": "secret"}).status_code == 400

    html = client.get(f"/api/tracks/{track_id}/report").text
    assert html.startswith("<!doctype html>") and "回放" in html
    assert html.count("<svg") == 2 and "<polyline" in html and "排行榜" in html
    assert "前视偏差" in html or "历史回放" in html
    assert "<script" not in html                                     # 纯静态，离线可打开
    dl = client.get(f"/api/tracks/{track_id}/report", params={"download": True})
    assert "report.html" in dl.headers["content-disposition"]
