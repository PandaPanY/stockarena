"""自选倾斜账户：只调整策略已选中的股票、保持总仓位、单只上限、与规则账户在同一赛道并行、不影响规则选手查找。"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.arena import cycle
from app.db.session import get_sessionmaker
from app.models import AccountRow, DecisionRow, PositionRow
from app.models.quant import QuantPortfolioRow
from app.quant import review
from app.trading.portfolio import AccountState
from app.watch import service as watch
from app.watch import tilt
from tests.quant.test_review import _setup, _Snap


def test_apply_tilt_rules():
    t = {"A": Decimal("0.3"), "B": Decimal("0.3"), "C": Decimal("0.3"), "X": Decimal("0")}
    out, changes = tilt.apply_tilt(t, {"A": 0.0, "B": 2.0, "Z": 3.0})
    # A 不买，钱按比例分给其他；B 多配；总仓位保持 0.9；没有选中的 Z 不会出现；权重 0 的已选股不保留
    assert out["A"] == 0 and "Z" not in out and out["X"] == 0
    # B 想加到 0.6，但原目标 0.3 已超过单只上限 10%，不再加；C 不变；A 让出的 0.3 留作现金
    assert out["B"] == Decimal("0.3") and out["C"] == Decimal("0.3")
    small = {f"S{i}": Decimal("0.02") for i in range(45)}               # 45 只各 2%，共 90%
    out2, _ = tilt.apply_tilt(small, {"S0": 0.0, "S1": 2.0})
    assert out2["S0"] == 0 and float(out2["S1"]) == pytest.approx(0.04 * 0.9 / 0.90, abs=2e-4)
    assert float(sum(out2.values())) == pytest.approx(0.9, abs=1e-3)     # 总仓位保持
    assert {c["symbol"] for c in changes} >= {"A", "B"}
    # 没有自选权重时不变
    same, _ = tilt.apply_tilt(t, {})
    assert same == {k: Decimal(str(round(float(v), 4))) if v > 0 else v for k, v in t.items()}


def test_apply_tilt_caps():
    t = {f"S{i}": Decimal("0.03") for i in range(30)}                 # 30 只各 3%
    out, _ = tilt.apply_tilt(t, {"S0": 3.0})
    assert float(out["S0"]) <= 0.09 + 1e-9                            # 不超过原目标的 3 倍
    assert float(sum(out.values())) <= 0.9 + 1e-6
    big = {"A": Decimal("0.2"), "B": Decimal("0.2")}
    out, _ = tilt.apply_tilt(big, {"A": 3.0})
    assert float(out["A"]) <= 0.2 + 1e-9                              # 原目标已超过单只上限时不再加
    allzero, _ = tilt.apply_tilt(big, {"A": 0.0, "B": 0.0})
    assert all(v == 0 for v in allzero.values())


async def test_tilt_first_decision_and_skip(monkeypatch):
    calls = {"first": True, "targets": None}

    def fake_ctx(pid, aid):
        return 1, AccountState(cash=Decimal("0")), {"A.SH": "0.45", "B.SH": "0.45"}, calls["first"]

    from app.baselines import BaselineDecision

    def fake_decide(pid, aid, state, snap):
        return BaselineDecision(calls["targets"], "规则：本轮", extra={"members": {}})

    monkeypatch.setattr(review, "_rule_context", fake_ctx)
    monkeypatch.setattr("app.quant.live.decide_portfolio", fake_decide)
    monkeypatch.setattr(watch, "weights", lambda s, uid=1: {"A.SH": 2.0})
    out = await tilt.decide_tilted(1, 2, AccountState(cash=Decimal("1000000")), _Snap())
    assert out.extra["tilt"]["sync"] and float(out.targets["A.SH"]) > float(out.targets["B.SH"])
    assert "调整了" in out.analysis
    calls["first"] = False
    out = await tilt.decide_tilted(1, 2, AccountState(cash=Decimal("1000000")), _Snap())
    assert out.targets is None and out.extra["tilt"]["skipped"]


async def test_tilt_account_runs_alongside_rule(arena, monkeypatch):
    with get_sessionmaker()() as s:
        pf, track, rule_acc, _provider = _setup(s)
        acc = tilt.enable(s, pf)
        assert review.rule_agent(s, pf.id).id == rule_acc.agent_id          # 规则选手查找不受倾斜选手影响
        with pytest.raises(Exception, match="已开启"):
            tilt.enable(s, pf)
        s.commit()
        track_id, pf_id, rule_id, tilt_id = track.id, pf.id, rule_acc.id, acc.id
    # 第一只被规则选中的股票：自选权重 0（不买）
    picked: list[str] = []
    real_apply = tilt.apply_tilt

    def spy(targets, weights):
        if not picked:
            picked.extend(sorted(k for k, v in targets.items() if v > 0)[:1])
        return real_apply(targets, {picked[0]: 0.0} if picked else {})

    monkeypatch.setattr(tilt, "apply_tilt", spy)
    await cycle.run_replay(track_id, lambda m: None)
    assert picked
    with get_sessionmaker()() as s:
        rule_pos = {p.symbol for p in s.scalars(select(PositionRow).where(PositionRow.account_id == rule_id))
                    if p.qty > 0}
        tilt_pos = {p.symbol for p in s.scalars(select(PositionRow).where(PositionRow.account_id == tilt_id))
                    if p.qty > 0}
        assert picked[0] not in tilt_pos
        assert tilt_pos <= rule_pos                                       # 不会买规则没有选的股票
        decs = list(s.scalars(select(DecisionRow).where(DecisionRow.account_id == tilt_id)))
        assert any(((d.parsed or {}).get("tilt") or {}).get("changes") for d in decs)
        rep = tilt.report(s, s.get(QuantPortfolioRow, pf_id))
        assert rep["enabled"] and rep["curve"] and rep["last_changes"]
        tilt.disable(s, s.get(QuantPortfolioRow, pf_id))
        s.commit()
        assert s.get(AccountRow, tilt_id).status == "stopped"
