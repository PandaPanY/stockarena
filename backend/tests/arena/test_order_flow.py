"""限价 / 止损 / 止盈 / 撤单在日终流程中的完整链路（伪造行情与 AI）。"""

from __future__ import annotations

import json
from decimal import Decimal

from sqlalchemy import select

from app.arena import cycle
from app.arena.lifecycle import create_track, enroll, start_track
from app.db.session import get_sessionmaker
from app.llm import ChatResponse
from app.models import AgentRow, DecisionRow, OrderRow, RuleSetRow
from tests.arena.conftest import cst


class ScriptedLLM:
    """按顺序返回预设的回复，记录收到的 prompt。"""

    def __init__(self, replies: list[dict]):
        self.replies = [json.dumps(r, ensure_ascii=False) for r in replies]
        self.prompts: list[str] = []

    async def chat(self, req):
        self.prompts.append(req.messages[0]["content"])
        reply = self.replies[min(len(self.prompts) - 1, len(self.replies) - 1)]
        return ChatResponse(content=reply, tokens_in=10, tokens_out=10, model="scripted")


def _track(output_mode: str) -> int:
    with get_sessionmaker()() as s:
        rs = s.scalars(select(RuleSetRow).where(RuleSetRow.name == "cn_long_only")).one()
        t = create_track(s, 1, name="订单", universe_name="cn_sse50_etf", rule_set_id=rs.id,
                         initial_cash=Decimal("1000000"), output_mode=output_mode)
        enroll(s, t, s.scalars(select(AgentRow).where(AgentRow.kind == "llm")).one().id)
        start_track(s, t, cst(2026, 9, 30, 16))
        s.commit()
        return t.id


def _orders(symbol: str | None = None) -> list[OrderRow]:
    with get_sessionmaker()() as s:
        q = select(OrderRow).order_by(OrderRow.id)
        if symbol:
            q = q.where(OrderRow.symbol == symbol)
        return list(s.scalars(q))


async def test_limit_buy_with_attached_stop_and_take_profit_then_cancel(arena, monkeypatch):
    llm = ScriptedLLM([
        {"analysis": "限价买入并设置止损止盈",
         "orders": [{"symbol": "600519", "side": "buy", "qty": 1000, "order_type": "limit", "limit_price": 101,
                     "stop_loss": 90, "take_profit": 130}]},
        {"analysis": "撤销止盈，保留止损", "orders": [], "cancel_order_ids": []},   # 第二轮占位，下面替换
    ])
    monkeypatch.setattr(cycle, "create_client", lambda cfg: llm)
    track_id = _track("orders")
    await cycle.run_track_cycle(track_id, now=cst(2026, 9, 30, 16))
    [buy] = _orders("600519.SH")
    assert (buy.order_type, buy.limit_price, buy.attach_stop, buy.attach_take_profit) == (
        "limit", Decimal("101"), Decimal("90"), Decimal("130"))

    # 10/8 结算：限价买入成交，随后自动挂出同组的止损与止盈
    tp_holder: dict = {}

    async def second_round_reply(req):
        llm.prompts.append(req.messages[0]["content"])
        return ChatResponse(content=json.dumps({"analysis": "撤销止盈", "orders": [],
                                                "cancel_order_ids": [tp_holder["id"]]}), model="scripted")

    original_prepare = cycle._prepare_round

    def capture(*args, **kwargs):
        rows = _orders("600519.SH")
        tp = [o for o in rows if o.order_type == "take_profit"]
        if tp:
            tp_holder["id"] = tp[0].id
        return original_prepare(*args, **kwargs)

    monkeypatch.setattr(cycle, "_prepare_round", capture)
    llm.chat = second_round_reply
    await cycle.run_track_cycle(track_id, now=cst(2026, 10, 8, 16))
    rows = {o.order_type: o for o in _orders("600519.SH")}
    assert rows["limit"].status == "filled"
    stop, tp = rows["stop"], rows["take_profit"]
    assert stop.qty == tp.qty == 1000 and stop.oco_group == tp.oco_group == stop.id
    assert stop.tif == "gtc" and stop.status == "pending"
    assert tp.status == "cancelled" and tp.note == "AI 撤单"
    # 第二轮 prompt 中能看到未成交的条件单
    assert f"#{stop.id} 卖出 600519.SH 1000 股 止损 触发价 90" in llm.prompts[-1]


async def test_invalid_stop_price_rejected(arena, monkeypatch):
    llm = ScriptedLLM([{"orders": [{"symbol": "600519", "side": "buy", "qty": 100, "stop_loss": 200}]}])
    monkeypatch.setattr(cycle, "create_client", lambda cfg: llm)
    track_id = _track("orders")
    await cycle.run_track_cycle(track_id, now=cst(2026, 9, 30, 16))
    [o] = _orders("600519.SH")
    assert o.status == "rejected" and "附带止损价" in o.note


async def test_targets_mode_risk_levels_replace_existing(arena, monkeypatch):
    llm = ScriptedLLM([
        {"targets": [{"symbol": "600519", "weight": 0.2, "stop_loss": 95, "take_profit": 120}]},
        {"targets": [{"symbol": "600519", "weight": 0.2, "stop_loss": 97, "take_profit": None}]},
    ])
    monkeypatch.setattr(cycle, "create_client", lambda cfg: llm)
    track_id = _track("targets")
    await cycle.run_track_cycle(track_id, now=cst(2026, 9, 30, 16))
    [buy] = _orders("600519.SH")
    assert buy.attach_stop == Decimal("95") and buy.attach_take_profit == Decimal("120")

    await cycle.run_track_cycle(track_id, now=cst(2026, 10, 8, 16))
    rows = _orders("600519.SH")
    first = [o for o in rows if o.order_type in ("stop", "take_profit") and o.reason.startswith("自动挂出")]
    assert {o.status for o in first} == {"cancelled"}              # 被第二轮的新设置替换
    new = [o for o in rows if o.reason == "AI 设置的止损止盈"]
    assert len(new) == 1 and new[0].order_type == "stop" and new[0].stop_price == Decimal("97")
    assert new[0].qty == buy.qty and new[0].status == "pending"
    with get_sessionmaker()() as s:
        assert len(s.scalars(select(DecisionRow).where(DecisionRow.status == "ok")).all()) == 2
