"""westock 每日快照：解析一致预期与评分、只存成功的、同一天不重复查询、失败太多时停止当天的查询。"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.db.session import get_sessionmaker
from app.models.research import WestockSnapshotRow
from app.models.skills import SkillRow
from app.quant import snapshots
from app.skills import gate, sandbox, service
from app.skills.sandbox import RunResult

CONSENSUS = """
#### sh600519 贵州茅台

目标价: 1661.4929

| year | eps | revenue | netProfit | pe | pb | ps | revenueYoy | netProfitYoy | institutionCnt |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2028 | 75.44 | 19693260.20 | 9430784.07 | 17.62 | 3.51 | 8.44 | 5.53 | 6.16 | 0 |
| 2027 | 71.06 | 18660658.07 | 8883401.88 | 18.70 | 4.38 | 8.90 | 5.22 | 6.13 | 0 |
"""

LIMITED = "请求过于频繁，请稍后再试\n"

_CELLS = ["81.33 (周↑+7.19 月↑+25.10 季↑+81.33)", "81.86 (周↑+2.60 月↑+4.98 季↑+81.86)",
          "85.63 (周↓-1.22 月↓-0.70 季↑+85.63)", "82.37 (周↑+0.19 月↓-0.29 季↑+82.37)",
          "85.44 (周↑+22.02 月↑+25.46 季↑+85.44)"]
SCORE = ("| code | name | capScore | compScore | funmScore | riskScore | tecScore |\n"
         "| --- | --- | --- | --- | --- | --- | --- |\n"
         "| sh600519 | 贵州茅台 | " + " | ".join(_CELLS) + " |\n")


def test_parse_consensus_and_score():
    c = snapshots.parse_consensus(CONSENSUS)
    assert c["target_price"] == pytest.approx(1661.4929)
    assert c["years"][0]["year"] == 2028 and c["years"][0]["eps"] == pytest.approx(75.44)
    assert c["years"][1]["netProfitYoy"] == pytest.approx(6.13)
    sc = snapshots.parse_score(SCORE)
    assert sc["capScore"] == {"value": 81.33, "week": 7.19, "month": 25.10, "quarter": 81.33}
    assert sc["funmScore"]["week"] == pytest.approx(-1.22)
    assert set(sc) == {"capScore", "compScore", "funmScore", "riskScore", "tecScore"}


def test_classify():
    assert snapshots.classify("consensus", CONSENSUS, 0)[0] == "ok"
    assert snapshots.classify("consensus", "查询一致预期失败：TLS handshake timeout", 0)[0] == "error"
    assert snapshots.classify("score", "", 0)[0] == "error"
    assert snapshots.classify("score", SCORE, 1)[0] == "error"
    assert snapshots.classify("consensus", "暂无数据", 0)[0] == "empty"
    assert snapshots.classify("score", "请求过于频繁，请稍后再试", 0)[0] == "limited"


@pytest.fixture
def westock(db, monkeypatch):
    monkeypatch.setenv("SA_SKILLS_ENABLED", "true")
    get_settings.cache_clear()
    gate.reset_cache()
    with get_sessionmaker()() as s:
        s.add(SkillRow(user_id=1, name="westockdata", source_type="upload", status="installed", enabled=True))
        s.commit()
    monkeypatch.setattr(service, "_token", lambda row: "tok")
    monkeypatch.setattr(snapshots, "MIN_INTERVAL_S", 0)
    monkeypatch.setattr(snapshots, "LIMIT_BACKOFF_S", (0, 0))
    calls: list[list[str]] = []
    replies: dict[str, str] = {"consensus": CONSENSUS, "score": SCORE}

    async def fake_run(skill_id, token, argv, timeout=60):
        calls.append(argv)
        out = replies[argv[1]]
        if isinstance(out, list):                  # 依次返回（模拟先被限流、后成功）
            out = out.pop(0) if len(out) > 1 else out[0]
        return RunResult(0, out, 0.1)

    async def fake_stop(skill_id):
        calls.append(["stop"])

    monkeypatch.setattr(sandbox, "run_batch", fake_run)
    monkeypatch.setattr(sandbox, "stop_batch", fake_stop)
    yield calls, replies
    gate.reset_cache()


async def test_run_saves_and_is_idempotent(westock):
    calls, _ = westock
    d = date(2026, 10, 19)
    stats = await snapshots.run(d, symbols=["600519.SH", "000651.SZ"])
    assert stats.saved == 4 and stats.failed == 0 and not stats.stopped
    assert ["westock", "consensus", "sh600519"] in calls and ["westock", "score", "sz000651"] in calls
    assert calls[-1] == ["stop"]                                   # 结束后销毁批量容器
    with get_sessionmaker()() as s:
        rows = s.scalars(select(WestockSnapshotRow).where(WestockSnapshotRow.as_of == d)).all()
        assert len(rows) == 4 and all(r.status == "ok" and r.raw for r in rows)
    calls.clear()
    again = await snapshots.run(d, symbols=["600519.SH", "000651.SZ"])
    assert again.saved == 0 and again.skipped == 4 and calls == [["stop"]]


async def test_run_stops_after_consecutive_failures(westock):
    calls, replies = westock
    replies["consensus"] = replies["score"] = "查询一致预期失败：TLS handshake timeout"
    stats = await snapshots.run(date(2026, 10, 19), symbols=[f"6000{i:02d}.SH" for i in range(20)])
    assert stats.failed == snapshots.MAX_CONSECUTIVE_FAILS and stats.saved == 0 and "停止" in stats.stopped
    with get_sessionmaker()() as s:
        assert s.scalars(select(WestockSnapshotRow)).first() is None          # 失败的不保存，下次补


async def test_run_skips_without_skill(db):
    stats = await snapshots.run(date(2026, 10, 19), symbols=["600519.SH"])
    assert stats.saved == 0 and "跳过" in stats.stopped


async def test_rate_limited_retries_then_saves(westock):
    calls, replies = westock
    replies["score"] = [LIMITED, SCORE]
    stats = await snapshots.run(date(2026, 10, 19), symbols=["600519.SH"], kinds=("score",))
    assert stats.saved == 1 and stats.limited == 1 and stats.failed == 0
    assert calls.count(["westock", "score", "sh600519"]) == 2


async def test_persistent_rate_limit_not_saved(westock):
    _, replies = westock
    replies["score"] = LIMITED
    stats = await snapshots.run(date(2026, 10, 19), symbols=["600519.SH"], kinds=("score",))
    assert stats.saved == 0 and stats.failed == 1 and stats.limited == 2
    with get_sessionmaker()() as s:
        assert s.scalars(select(WestockSnapshotRow)).first() is None
