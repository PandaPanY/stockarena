"""做空、融资、计息、追保与强平在日终流程中的完整链路（伪造行情与 AI，PRD US-4.4）。"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from app.arena import cycle
from app.arena.accounts import load_state, reconcile, save_state
from app.arena.lifecycle import create_track, enroll, start_track
from app.db.session import get_sessionmaker
from app.models import (
    AccountRow,
    AgentRow,
    CashLedgerRow,
    DecisionRow,
    EventRow,
    FillRow,
    NavSnapshotRow,
    OrderRow,
    RuleSetRow,
)
from app.trading.portfolio import AccountState
from app.trading.types import Position
from tests.arena.conftest import cst
from tests.arena.test_order_flow import ScriptedLLM

MAOTAI = "600519.SH"


def _track(output_mode: str = "orders", ruleset: str = "cn_margin") -> tuple[int, int]:
    with get_sessionmaker()() as s:
        rs = s.scalars(select(RuleSetRow).where(RuleSetRow.name == ruleset)).one()
        t = create_track(s, 1, name="两融", universe_name="cn_sse50_etf", rule_set_id=rs.id,
                         initial_cash=Decimal("1000000"), output_mode=output_mode)
        acc = enroll(s, t, s.scalars(select(AgentRow).where(AgentRow.kind == "llm")).one().id)
        start_track(s, t, cst(2026, 9, 30, 16))
        s.commit()
        return t.id, acc.id


def _set_state(account_id: int, cash: str, qty: int, cost: str = "100") -> None:
    with get_sessionmaker()() as s:
        acc = s.get(AccountRow, account_id)
        save_state(s, acc, AccountState(cash=Decimal(cash), positions={MAOTAI: Position(MAOTAI, qty, Decimal(cost))}))
        s.commit()


def _first_system_prompt(account_id: int) -> str:
    with get_sessionmaker()() as s:
        return s.scalars(select(DecisionRow.prompt_system).where(DecisionRow.account_id == account_id)
                         .order_by(DecisionRow.id)).first()


def _events(account_id: int) -> list[str]:
    with get_sessionmaker()() as s:
        return [e.type for e in s.scalars(select(EventRow).where(EventRow.account_id == account_id)
                                          .order_by(EventRow.id)) if e.type.startswith(("margin", "forced"))]


async def test_short_sell_with_protection_borrow_fee_and_cover(arena, monkeypatch):
    llm = ScriptedLLM([
        {"analysis": "卖空茅台", "orders": [{"symbol": "600519", "side": "short", "qty": 1000,
                                         "stop_loss": 130, "take_profit": 80}]},
        {"analysis": "平空", "orders": [{"symbol": "600519", "side": "cover", "qty": 1000}]},
        {"analysis": "观望", "orders": []},
    ])
    monkeypatch.setattr(cycle, "create_client", lambda cfg: llm)
    track_id, acc_id = _track()
    await cycle.run_track_cycle(track_id, now=cst(2026, 9, 30, 16))
    system = _first_system_prompt(acc_id)
    assert '"short" 卖空' in system and "维持担保比例" in system and "融资年息" not in system

    await cycle.run_track_cycle(track_id, now=cst(2026, 10, 8, 16))
    with get_sessionmaker()() as s:
        state = load_state(s, s.get(AccountRow, acc_id))
        assert state.position(MAOTAI).qty == -1000
        protect = list(s.scalars(select(OrderRow).where(OrderRow.account_id == acc_id, OrderRow.side == "cover",
                                                        OrderRow.status == "pending",
                                                        OrderRow.order_type.in_(["stop", "take_profit"]))))
        assert sorted((o.order_type, o.stop_price) for o in protect) == [("stop", Decimal("130")),
                                                                         ("take_profit", Decimal("80"))]
        fees = list(s.scalars(select(CashLedgerRow).where(CashLedgerRow.account_id == acc_id,
                                                          CashLedgerRow.type == "borrow_fee")))
        assert len(fees) == 1 and "× 1/365" in fees[0].note             # 10/8 收盘计到下一交易日 10/9，共 1 天
        nav = s.scalars(select(NavSnapshotRow).where(NavSnapshotRow.account_id == acc_id)
                        .order_by(NavSnapshotRow.date.desc())).first()
        assert nav.short_value > 0 and nav.long_value == 0
        assert reconcile(s, s.get(AccountRow, acc_id)) == 0
    # 第二轮 prompt 显示空头持仓与杠杆信息
    assert "空头市值" in llm.prompts[1] and "-1000" in llm.prompts[1] and "维持担保比例" in llm.prompts[1]

    await cycle.run_track_cycle(track_id, now=cst(2026, 10, 9, 16))
    with get_sessionmaker()() as s:
        acc = s.get(AccountRow, acc_id)
        assert load_state(s, acc).position(MAOTAI) is None
        cover = s.scalars(select(FillRow).where(FillRow.account_id == acc_id, FillRow.side == "cover")).one()
        assert cover.realized_pnl is not None
        # 空头平掉后，保护性条件单失去标的，自动撤销
        left = s.scalars(select(OrderRow).where(OrderRow.account_id == acc_id, OrderRow.status == "pending")).all()
        assert left == []
        assert reconcile(s, acc) == 0


async def test_immediate_forced_liquidation_below_liquidation_line(arena, monkeypatch):
    llm = ScriptedLLM([{"analysis": "观望", "orders": [], "cancel_order_ids": []}])
    monkeypatch.setattr(cycle, "create_client", lambda cfg: llm)
    track_id, acc_id = _track()
    await cycle.run_track_cycle(track_id, now=cst(2026, 9, 30, 16))
    _set_state(acc_id, "-95000", 1000)                 # 维持担保比例约 100,300 / 95,000 = 105.6% < 110%

    await cycle.run_track_cycle(track_id, now=cst(2026, 10, 8, 16))
    with get_sessionmaker()() as s:
        forced = s.scalars(select(OrderRow).where(OrderRow.account_id == acc_id, OrderRow.forced.is_(True))).one()
        assert (forced.side, forced.status, forced.target_date) == ("sell", "pending", date(2026, 10, 9))
        assert 0 < forced.qty < 1000                    # 只平到维持线 + 5%
        assert s.get(AccountRow, acc_id).margin_call_since == date(2026, 10, 8)
        assert any(e.type == "interest" for e in s.scalars(select(CashLedgerRow).where(
            CashLedgerRow.account_id == acc_id)))
    assert "强制平仓" in llm.prompts[-1]

    # AI 不能撤销强平单
    llm.replies = [json.dumps({"analysis": "撤销", "orders": [], "cancel_order_ids": [forced.id]})]
    await cycle.run_track_cycle(track_id, now=cst(2026, 10, 9, 16))
    with get_sessionmaker()() as s:
        row = s.get(OrderRow, forced.id)
        assert row.status == "filled"
        acc = s.get(AccountRow, acc_id)
        assert acc.margin_call_since is None            # 强平后恢复
    assert _events(acc_id) == ["forced_liquidation", "margin_restored"]


async def test_margin_call_then_liquidation_next_check(arena, monkeypatch):
    llm = ScriptedLLM([{"analysis": "观望", "orders": []}])
    monkeypatch.setattr(cycle, "create_client", lambda cfg: llm)
    track_id, acc_id = _track()
    await cycle.run_track_cycle(track_id, now=cst(2026, 9, 30, 16))
    _set_state(acc_id, "-83000", 1000)                 # 约 121%：介于强平线 110% 与维持线 130% 之间

    await cycle.run_track_cycle(track_id, now=cst(2026, 10, 8, 16))
    assert _events(acc_id) == ["margin_call"]
    assert "追保" in llm.prompts[-1]
    with get_sessionmaker()() as s:
        assert not s.scalars(select(OrderRow).where(OrderRow.forced.is_(True))).all()

    await cycle.run_track_cycle(track_id, now=cst(2026, 10, 9, 16))     # AI 没有处理，仍低于维持线
    assert _events(acc_id) == ["margin_call", "forced_liquidation"]
    with get_sessionmaker()() as s:
        forced = s.scalars(select(OrderRow).where(OrderRow.forced.is_(True))).one()
        assert forced.target_date == date(2026, 10, 12)


async def test_long_only_track_unaffected(arena, monkeypatch):
    llm = ScriptedLLM([{"analysis": "卖空", "orders": [{"symbol": "600519", "side": "short", "qty": 100}]}])
    monkeypatch.setattr(cycle, "create_client", lambda cfg: llm)
    track_id, acc_id = _track(ruleset="cn_long_only")
    await cycle.run_track_cycle(track_id, now=cst(2026, 9, 30, 16))
    system = _first_system_prompt(acc_id)
    assert '"short"' not in system and "只能做多" in system
    with get_sessionmaker()() as s:
        o = s.scalars(select(OrderRow).where(OrderRow.account_id == acc_id)).one()
        assert o.status == "rejected" and "不允许卖空" in o.note


async def test_account_margin_info_and_compare_api(arena, client, monkeypatch):
    llm = ScriptedLLM([{"analysis": "卖空", "orders": [{"symbol": "600519", "side": "short", "qty": 1000}]},
                       {"analysis": "观望", "orders": []}])
    monkeypatch.setattr(cycle, "create_client", lambda cfg: llm)
    track_id, acc_id = _track()
    await cycle.run_track_cycle(track_id, now=cst(2026, 9, 30, 16))
    await cycle.run_track_cycle(track_id, now=cst(2026, 10, 8, 16))
    await cycle.run_track_cycle(track_id, now=cst(2026, 10, 9, 16))

    acc = client.get(f"/api/accounts/{acc_id}").json()
    m = acc["margin"]
    assert m["allow_short"] and m["short_value"] > 0 and m["ratio"] > 1.3 and m["method"] == "collateral_ratio"
    assert acc["metrics"]["max_leverage"] > 0 and acc["metrics"]["radar"]["risk"] is not None
    pos = client.get(f"/api/accounts/{acc_id}/positions").json()
    assert pos[0]["short"] and pos[0]["qty"] == -1000 and pos[0]["weight"] < 0

    # 再建一个只做多赛道，跨赛道对比
    llm.replies = [json.dumps({"analysis": "买入", "orders": [{"symbol": "600519", "side": "buy", "qty": 1000}]})]
    track2, acc2 = _track(ruleset="cn_long_only")
    await cycle.run_track_cycle(track2, now=cst(2026, 9, 30, 16))
    await cycle.run_track_cycle(track2, now=cst(2026, 10, 8, 16))
    assert client.get(f"/api/accounts/{acc2}").json()["margin"] is None
    data = client.get("/api/compare", params={"account_ids": f"{acc_id},{acc2}"}).json()
    assert [a["account_id"] for a in data["accounts"]] == [acc_id, acc2]
    assert data["accounts"][0]["composition"]["short"] > 0 and data["accounts"][1]["composition"]["stock_long"] > 0
    assert data["holding_overlap"][0][1] == 0                    # 一个做空、一个做多，方向相反不算重合
    assert data["traded_overlap"][0][1] == 1.0
    assert client.get("/api/compare", params={"account_ids": str(acc_id)}).status_code == 400
