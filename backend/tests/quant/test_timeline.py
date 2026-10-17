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


async def test_strategy_report_html(lab, client):  # noqa: F811
    _llm, pid = lab
    sid = _create(provider_id=pid)
    with get_sessionmaker()() as s:
        run = research.start_run(s, s.get(QuantStrategyRow, sid))
        s.commit()
        rid = run.id
    await research.run_research(rid)
    r = client.get(f"/api/quant/strategies/{sid}/report")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    page = r.text
    for text in ("策略体检报告", "不构成任何投资建议", "折算可信度", "累计尝试次数", "当前规则（v2）", "被采纳的改动",
                 "<svg", "可实盘检查", "AGPL-3.0"):
        assert text in page, text
    assert "<script" not in page                                          # 纯静态，离线可开
    d = client.get(f"/api/quant/strategies/{sid}/report", params={"download": True})
    assert "attachment" in d.headers["content-disposition"] and "UTF-8''" in d.headers["content-disposition"]
