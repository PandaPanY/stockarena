"""研究模型对比：建对比策略（归档、不带笔记）、依次优化且中断后只补剩余轮数、按实验设计汇总。"""

from __future__ import annotations

import asyncio

from app.db.session import get_sessionmaker
from app.models.quant import QuantStrategyRow, ResearchRunRow
from app.quant import model_compare, service
from tests.quant.test_lab import BAD, GOOD, SETTINGS, lab  # noqa: F401


def test_setup_run_and_summarize(lab, monkeypatch):  # noqa: F811
    _, pid = lab
    monkeypatch.setattr(service, "load_seeds", lambda: [
        {"style": "a", "title": "甲", "description": "甲风格", "spec": BAD},
        {"style": "b", "title": "乙", "description": "乙风格", "spec": GOOD}])
    with get_sessionmaker()() as s:
        data = model_compare.setup(s, [pid], 1, rounds=2, settings=SETTINGS)
        assert [i["style"] for i in data["items"]] == ["a", "b"]
        sts = [s.get(QuantStrategyRow, i["strategy_id"]) for i in data["items"]]
        assert all(st.status == "archived" and st.provider_id == pid and not st.research_notes for st in sts)
        assert sts[0].name.startswith("模型对比·")

    saved = []
    asyncio.run(model_compare.run_all(data, save=lambda d: saved.append(len(d["items"][0]["runs"]))))
    with get_sessionmaker()() as s:
        first = s.get(ResearchRunRow, data["items"][0]["runs"][0])
        assert first.status == "done" and first.rounds_done == 2 and first.trigger == "compare"
        first.status, first.rounds_done = "running", 1          # 模拟脚本在第 2 轮中途被中断
        s.commit()
    asyncio.run(model_compare.run_all(data, save=lambda d: None))
    with get_sessionmaker()() as s:
        runs = [s.get(ResearchRunRow, r) for r in data["items"][0]["runs"]]
        assert [r.status for r in runs] == ["failed", "done"] and runs[1].rounds == 1    # 只补剩余 1 轮
        rep = model_compare.summarize(s, data)
    row = rep["rows"][0]
    assert row["rounds"] == 2 and row["interrupted"] == 1 and row["candidates"] > 0
    assert row["dev_delta"] is not None and row["holdout_delta"] is not None
    assert rep["models"]["本地"]["promoted"] >= 1 and rep["verdict"] == "看不出差别"
