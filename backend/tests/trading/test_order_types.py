"""限价单、止损单、止盈单、有效期与 OCO（PRD US-4.3）。"""

from __future__ import annotations

from app.trading.engine import settle_bar
from app.trading.matching import match_order
from app.trading.portfolio import AccountState
from app.trading.types import PendingOrder, Position
from tests.trading.conftest import T1, T2, D, bar, stock

S = stock()
INSTS = {S.symbol: S}


def _order(oid=1, side="buy", qty=100, **kw) -> PendingOrder:
    return PendingOrder(oid, S.symbol, side, qty, **kw)


def _held(qty=1000, cost="10") -> AccountState:
    return AccountState(cash=D("0"), positions={S.symbol: Position(S.symbol, qty, D(cost))})


# ---------------- 撮合价格 ----------------
def test_limit_buy_triggered_and_price(params):
    b = bar(o="10.2", h="10.5", low="9.8")
    r = match_order(_order(order_type="limit", limit_price=D("10")), 100, b, S, params)
    assert (r.qty, r.price) == (100, D("10"))                    # 开盘 10.2 高于限价，以限价成交
    gap = bar(o="9.9", h="10.1", low="9.8")
    assert match_order(_order(order_type="limit", limit_price=D("10")), 100, gap, S, params).price == D("9.9")


def test_limit_not_reached_waits(params):
    r = match_order(_order(order_type="limit", limit_price=D("9.5")), 100, bar(o="10", h="10.5", low="9.8"), S, params)
    assert r.qty == 0 and r.waiting


def test_limit_sell(params):
    r = match_order(_order(side="sell", order_type="limit", limit_price=D("10.3")), 100,
                    bar(o="10", h="10.5", low="9.8"), S, params)
    assert r.price == D("10.3")


def test_stop_loss_triggered_with_slippage_and_gap(params):
    stop = _order(side="sell", order_type="stop", stop_price=D("9.5"))
    r = match_order(stop, 100, bar(o="10", h="10", low="9.4"), S, params)
    assert r.price == D("9.49")                                  # 9.5 × (1 - 5bp) = 9.49525 → 9.49
    gap = match_order(stop, 100, bar(o="9.2", h="9.3", low="9.1", prev="10"), S, params)
    assert gap.price == D("9.19")                                # 跳空低开：按开盘价减滑点
    assert match_order(stop, 100, bar(o="10", h="10.2", low="9.6"), S, params).waiting


def test_take_profit(params):
    tp = _order(side="sell", order_type="take_profit", stop_price=D("10.8"))
    assert match_order(tp, 100, bar(o="10.5", h="10.9", low="10.4"), S, params).price == D("10.8")
    assert match_order(tp, 100, bar(o="11", h="11", low="10.9"), S, params).price == D("11")   # 高开按开盘价
    assert match_order(tp, 100, bar(o="10.5", h="10.7", low="10.4"), S, params).waiting


# ---------------- 结算：有效期与 OCO ----------------
def test_day_limit_expires_at_close_gtc_keeps_waiting(params):
    acc = AccountState(cash=D("100000"))
    day = _order(1, order_type="limit", limit_price=D("9"))
    gtc = _order(2, order_type="limit", limit_price=D("9"), tif="gtc")
    st = settle_bar(acc, [day, gtc], {S.symbol: bar(o="10", h="10.5", low="9.8")}, INSTS, params, T1, True)
    assert day.status == "expired" and gtc.status == "pending"
    assert [o.id for o in st.orders] == [1]


def test_intraday_bar_keeps_day_orders_until_close(params):
    acc = AccountState(cash=D("100000"))
    day = _order(1, order_type="limit", limit_price=D("9"))
    settle_bar(acc, [day], {S.symbol: bar(o="10")}, INSTS, params, T1, end_of_session=False)
    assert day.status == "pending"
    mkt = _order(2)
    settle_bar(acc, [mkt], {}, INSTS, params, T1, end_of_session=False)   # 本分钟没有行情：市价单继续等待
    assert mkt.status == "pending"


def test_oco_stop_fills_cancels_take_profit(params):
    acc = _held()
    tp = _order(1, side="sell", qty=1000, order_type="take_profit", stop_price=D("11"), tif="gtc", oco_group=7)
    sl = _order(2, side="sell", qty=1000, order_type="stop", stop_price=D("9.5"), tif="gtc", oco_group=7)
    st = settle_bar(acc, [tp, sl], {S.symbol: bar(o="9.6", h="9.7", low="9.3")}, INSTS, params, T2, True)
    assert sl.status == "filled" and tp.status == "cancelled"
    assert acc.position(S.symbol) is None
    assert len(st.fills) == 1


def test_conditional_without_position_cancelled(params):
    sl = _order(1, side="sell", order_type="stop", stop_price=D("9"), tif="gtc", oco_group=3)
    settle_bar(AccountState(cash=D("0")), [sl], {S.symbol: bar()}, INSTS, params, T1, True)
    assert sl.status == "cancelled" and "没有可卖持仓" in sl.note


def test_stop_capped_by_reduced_position(params):
    acc = _held(qty=300)
    sl = _order(1, side="sell", qty=1000, order_type="stop", stop_price=D("9.5"), tif="gtc")
    st = settle_bar(acc, [sl], {S.symbol: bar(o="9.6", h="9.7", low="9.3")}, INSTS, params, T2, True)
    assert st.fills[0].qty == 300


def test_intraday_volume_capped_order_keeps_filling(params):
    acc = AccountState(cash=D("1000000"))
    o = _order(1, qty=3000)
    thin = bar(o="10", h="10.1", low="9.9", volume=10_000)          # 上限 1000 股
    settle_bar(acc, [o], {S.symbol: thin}, INSTS, params, T1, end_of_session=False)
    assert (o.status, o.filled_qty) == ("pending", 1000) and "继续等待" in o.note
    settle_bar(acc, [o], {S.symbol: thin}, INSTS, params, T1, end_of_session=False)
    settle_bar(acc, [o], {S.symbol: thin}, INSTS, params, T1, end_of_session=False)
    assert (o.status, o.filled_qty) == ("filled", 3000)
    assert acc.position(S.symbol).qty == 3000


def test_daily_volume_capped_order_remainder_expires(params):
    acc = AccountState(cash=D("1000000"))
    o = _order(1, qty=3000)
    settle_bar(acc, [o], {S.symbol: bar(volume=10_000)}, INSTS, params, T1, end_of_session=True)
    assert (o.status, o.filled_qty) == ("partially_filled", 1000)
