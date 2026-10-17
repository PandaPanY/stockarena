"""AI 优化时间线：起点、每轮诊断与候选、规则差异、检验结果与是否采纳；不含留出期。"""

from __future__ import annotations

from app.db.session import get_sessionmaker
from app.models.quant import QuantStrategyRow
from app.quant import research, timeline
from tests.quant.test_lab import BAD, GOOD, _create, lab  # noqa: F401


def test_rule_diff():
    d = timeline.rule_diff(BAD, GOOD)
    assert any("越低越好" in x for x in d["removed"]) and any("越高越好" in x for x in d["added"])
    assert timeline.rule_diff(BAD, BAD) == {"removed": [], "added": []}


async def test_timeline_after_research_round(lab, client):  # noqa: F811
    _llm, pid = lab
    sid = _create(provider_id=pid)
    with get_sessionmaker()() as s:
        run = research.start_run(s, s.get(QuantStrategyRow, sid))
        s.commit()
        rid = run.id
    await research.run_research(rid)

    tl = client.get(f"/api/quant/strategies/{sid}/timeline").json()
    assert tl["start"]["number"] == 1 and tl["start"]["rules"]
    assert tl["summary"]["ai_promoted"] == 1 and tl["summary"]["champion"] == 2
    rnd = tl["events"][0]
    assert rnd["type"] == "round" and rnd["run_id"] == rid and rnd["round"] == 1 and rnd["analysis"]
    champ = next(c for c in rnd["candidates"] if c.get("number") == 2)
    assert champ["outcome"] == "升级为冠军" and champ["parent_number"] == 1
    assert champ["diff"]["added"] and champ["metrics"]["sharpe"] > 0 and champ["metrics"]["p"] is not None
    assert any(c["outcome"].startswith("与已试过") for c in rnd["candidates"])
    assert "holdout" not in str(tl)                                        # 只放开发期
