from __future__ import annotations

from decimal import Decimal

import pytest

from app.market.types import CorporateAction
from app.trading.engine import settle_day
from app.trading.fees import calc_fees_cn
from app.trading.portfolio import AccountState
from app.trading.types import Fill, PendingOrder, Position
from tests.trading.conftest import T1, T2, D, bar, etf, stock

S = stock()
E = etf()
INSTS = {S.symbol: S, E.symbol: E}


def _reconcile(initial: Decimal, ledger) -> Decimal:
    """K2：由流水重算现金。"""
    return initial + sum((e.amount for e in ledger), Decimal("0"))


def test_buy_then_sell_updates_cash_cost_and_realized_pnl(params):
    acc = AccountState(cash=D("100000"))
    buy = Fill(1, S.symbol, "buy", 1000, D("10"), calc_fees_cn(S, "buy", 1000, D("10"), params), T1)
    ledger = acc.apply_fill(buy)
    pos = acc.position(S.symbol)
    assert pos.qty == 1000
    assert pos.avg_cost == D("10.0051")          # (10000 + 5 + 0.10) / 1000
    assert acc.cash == D("100000") - D("10005.10")
    sell = Fill(2, S.symbol, "sell", 1000, D("11"), calc_fees_cn(S, "sell", 1000, D("11"), params), T2)
    ledger += acc.apply_fill(sell)
    assert acc.position(S.symbol) is None
    # 已实现盈亏 = (11 - 10.0051) × 1000 - 卖出费用(5 + 0.11 + 5.50)
    assert sell.realized_pnl == D("984.29")
    assert _reconcile(D("100000"), ledger) == acc.cash


def test_sell_more_than_held_raises(params):
    acc = AccountState(cash=D("0"), positions={S.symbol: Position(S.symbol, 100, D("10"))})
    with pytest.raises(ValueError):
        acc.apply_fill(Fill(1, S.symbol, "sell", 200, D("10"), {}, T1))


def test_nav_requires_all_prices():
    acc = AccountState(cash=D("1000"), positions={S.symbol: Position(S.symbol, 100, D("10"))})
    assert acc.nav({S.symbol: D("12")}) == D("2200")
    with pytest.raises(KeyError):
        acc.nav({})


def test_cash_dividend_and_bonus_shares():
    acc = AccountState(cash=D("0"), positions={S.symbol: Position(S.symbol, 1000, D("10"))})
    ledger = acc.apply_corporate_action(
        CorporateAction(S.symbol, T1, cash_per_share=D("0.5"), shares_per_share=D("0.3")), T1)
    pos = acc.position(S.symbol)
    assert acc.cash == D("500.00")
    assert pos.qty == 1300
    # 成本：(10 - 0.5) × 1000 / 1300
    assert pos.avg_cost == D("9500") / 1300
    assert [e.type for e in ledger] == ["dividend", "adjust"]


def test_dividend_ignored_without_position():
    acc = AccountState(cash=D("0"))
    assert acc.apply_corporate_action(CorporateAction(S.symbol, T1, D("1")), T1) == []


def test_settle_day_sells_before_buys_and_uses_proceeds(params):
    acc = AccountState(cash=D("1000"), positions={E.symbol: Position(E.symbol, 10000, D("4"))})
    orders = [PendingOrder(1, S.symbol, "buy", 400), PendingOrder(2, E.symbol, "sell", 10000)]
    bars = {S.symbol: bar(o="10", h="10.5", low="9.9"),
            E.symbol: bar(E.symbol, o="4.4", h="4.5", low="4.3", prev="4.4")}
    st = settle_day(acc, orders, bars, INSTS, [], params, T1)
    assert [f.side for f in st.fills] == ["sell", "buy"]
    assert all(o.status == "filled" for o in st.orders)
    assert acc.position(S.symbol).qty == 400
    assert _reconcile(D("1000"), st.ledger) == acc.cash


def test_settle_day_reduces_buy_when_cash_short(params):
    acc = AccountState(cash=D("3000"))
    st = settle_day(acc, [PendingOrder(1, S.symbol, "buy", 1000)], {S.symbol: bar(o="10", h="10.5", low="9.9")},
                    INSTS, [], params, T1)
    assert st.fills[0].qty == 200                 # 3000 元只买得起 200 股（含费用）
    assert st.orders[0].status == "partially_filled"
    assert acc.cash >= 0


def test_settle_day_expires_unfillable_orders(params):
    acc = AccountState(cash=D("100000"))
    limit_up = bar(o="11", h="11", low="11", prev="10")
    st = settle_day(acc, [PendingOrder(1, S.symbol, "buy", 100)], {S.symbol: limit_up}, INSTS, [], params, T1)
    assert st.fills == []
    assert st.orders[0].status == "expired" and "涨停" in st.orders[0].note
    assert acc.cash == D("100000")


def test_settle_day_corporate_action_before_matching(params):
    acc = AccountState(cash=D("0"), positions={S.symbol: Position(S.symbol, 1000, D("10"))})
    st = settle_day(acc, [], {}, INSTS, [CorporateAction(S.symbol, T1, D("1"))], params, T1)
    assert acc.cash == D("1000.00")
    assert st.events[0].type == "corporate_action"


def test_settle_day_sell_without_position_expires(params):
    acc = AccountState(cash=D("0"))
    st = settle_day(acc, [PendingOrder(1, S.symbol, "sell", 100)], {S.symbol: bar()}, INSTS, [], params, T1)
    assert st.orders[0].status == "expired"
