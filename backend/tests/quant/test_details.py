"""策略交易明细（PRD 3.8）：每笔持有的收益、交易原因与选股理由。"""

from __future__ import annotations

import pytest

from app.quant import details
from app.quant.backtest import run_backtest
from app.quant.service import LabSettings
from app.quant.spec import parse_spec
from tests.quant.test_lab import GOOD, PANEL, SETTINGS

SPEC = {**GOOD, "filters": [{"name": "momentum", "params": {"window": 20}, "op": ">", "value": 0}],
        "risk": {"stop_loss": 0.08}}


def test_episode_pnl_adds_up_to_nav_change():
    cfg = LabSettings.from_dict(SETTINGS)
    res = run_backtest(parse_spec(SPEC), PANEL, cfg.train_start, PANEL.dates[-1], explain=True)
    assert sum(e.pnl for e in res.episodes) == pytest.approx(res.nav[-1] - res.initial_cash, rel=1e-9)
    assert all(t.note for t in res.trades)
    # 不带 explain 的回测结果完全相同（解释只用于展示）
    plain = run_backtest(parse_spec(SPEC), PANEL, cfg.train_start, PANEL.dates[-1])
    assert plain.nav[-1] == pytest.approx(res.nav[-1]) and not plain.rebalances


def test_details_windows_and_reasons():
    cfg = LabSettings.from_dict(SETTINGS)
    d = details.details(1, SPEC, PANEL, cfg, "all")
    s = d["summary"]
    assert s["trades"] == d["trades_total"] and s["closed"] + s["open"] == len(d["episodes"])
    assert s["pnl"] == pytest.approx(sum(x["pnl"] for x in d["symbols"]), abs=1)
    reb = next(r for r in d["rebalances"] if r["kind"] == "rebalance" and r["picks"])
    p = reb["picks"][0]
    assert p["rank"] >= 1 and p["of"] >= p["rank"] and p["factors"][0]["name"] == "动量(60,0)"
    assert 0 < p["factors"][0]["pct"] <= 1
    drops = [x for r in d["rebalances"] for x in r.get("drops", [])]
    assert drops and all(x["reason"] for x in drops)
    assert any("排名" in x["reason"] or "过滤" in x["reason"] or "止损" in x["reason"] for x in drops)
    assert {g["reason"] for g in s["exit_reasons"]} <= {"移动止损", "止损", "掉出排名", "不满足过滤条件", "大盘择时",
                                                       "不在选股范围", "停牌", "其他"}
    train = details.details(1, SPEC, PANEL, cfg, "train")
    assert train["summary"]["end"] == "2020-06-30" and all(t["date"] <= "2020-06-30" for t in train["trades"])
    assert not any(e["open"] for e in train["episodes"])          # 仍持有的只出现在最后的区间
