"""研究报告数据：同一截止日重新回测全部版本，统计漏斗、秩相关、相对父版本的提升、升级后的留出期表现。"""

from __future__ import annotations

import asyncio

from app.db.session import get_sessionmaker
from app.models.quant import QuantStrategyRow
from app.quant import ai_report, research
from tests.quant.test_lab import BAD, PANEL, _create, lab  # noqa: F401


def test_report_over_research_history(lab):  # noqa: F811
    _, pid = lab
    sid = _create(BAD, pid)
    with get_sessionmaker()() as s:
        run = research.start_run(s, s.get(QuantStrategyRow, sid), 2)
        s.commit()
    asyncio.run(research.run_research(run.id))
    other = _create(BAD, pid)
    with get_sessionmaker()() as s:
        s.get(QuantStrategyRow, other).name = "模型对比·x"           # 对比实验的策略不计入
        s.commit()
    out = ai_report.run(PANEL.dates[-1])
    assert [r["id"] for r in out["strategies"]] == [sid]
    r = out["strategies"][0]
    assert r["versions"] == r["evaluated"] >= 2 and r["ai_versions"] >= 1 and r["promotions"] >= 1
    assert r["rounds"] == 2 and r["cost"] >= 0 and r["tokens_in"] == 10000
    assert r["initial"]["dev"]["sharpe"] is not None and r["champion_number"] > 1
    assert r["delta_n"] >= 1 and len(r["upgrades"]) == r["promotions"]
    p = out["pooled"]
    assert p["strategies"] == 1 and p["upgrades"] == r["promotions"] and p["versions"] == r["versions"]
