"""做空、融资、计息、保证金与强平（PRD 4.6.4、4.6.5，v0.3）。"""

from __future__ import annotations

import pytest

from app.market.types import CorporateAction
from app.trading.engine import settle_bar
from app.trading.margin import check_margin, liquidation_orders
from app.trading.market_rules import closable_qty
from app.trading.matching import match_order
from app.trading.params import RuleParams
from app.trading.portfolio import AccountState
from app.trading.risk import precheck
from app.trading.targets import targets_to_intents
from app.trading.types import Fill, OrderIntent, PendingOrder, Position
from tests.trading.conftest import T1, T2, D, bar, etf, stock

S = stock()
S2 = stock("601318.SH")
E = etf()
INSTS = {S.symbol: S, S2.symbol: S2, E.symbol: E}
NO_FEE = dict(commission_rate=D("0"), commission_min=D("0"), stamp_duty_rate=D("0"), transfer_fee_rate=D("0"))


def lev(**kw) -> RuleParams:
    return RuleParams(allow_short=True, allow_margin=True, **kw)


def fill(side, qty, price, sym=S.symbol, d=T1, fees=None) -> Fill:
    return Fill(1, sym, side, qty, D(price), fees or {}, d)


# ---------------------------------------------------------------- 参数


def test_params_leverage_default_and_validation():
    assert RuleParams().leverage_cap == 1
    p = RuleParams(allow_short=True)
    assert p.max_leverage == 2 and p.leverage_cap == 2
    assert RuleParams(allow_margin=True, max_leverage="3").leverage_cap == 2       # 受初始保证金 50% 限制
    with pytest.raises(ValueError, match="强平线"):
        RuleParams(maintenance_margin="0.2", liquidation_margin="0.25")


# ---------------------------------------------------------------- 账户


def test_short_and_cover_accounting():
    acc = AccountState(cash=D("100000"))
    acc.apply_fill(fill("short", 1000, "10", fees={"commission": D("5")}))
    pos = acc.position(S.symbol)
    assert pos.qty == -1000 and pos.avg_cost == D("9.995")       # 扣除费用后的卖空均价
    assert acc.cash == D("109995")
    prices = {S.symbol: D("11")}
    assert acc.nav(prices) == D("98995")                         # 现金 − 空头市值
    assert acc.short_value(prices) == D("11000") and acc.gross_exposure(prices) == D("11000")
    f = fill("cover", 400, "9", d=T2, fees={"commission": D("5")})
    acc.apply_fill(f)
    assert f.realized_pnl == D("393.00")                         # (9.995 − 9) × 400 − 5
    assert acc.position(S.symbol).qty == -600
    with pytest.raises(ValueError, match="反向仓位"):
        acc.apply_fill(fill("buy", 100, "9"))
    with pytest.raises(ValueError, match="超过持仓"):
        acc.apply_fill(fill("cover", 700, "9"))


def test_short_dividend_and_split():
    acc = AccountState(cash=D("0"), positions={S.symbol: Position(S.symbol, -1000, D("10"))})
    entries = acc.apply_corporate_action(CorporateAction(S.symbol, T1, D("0.5"), D("0.2")), T1, D("0.1"))
    assert acc.cash == D("-500")                                 # 补偿税前分红
    pos = acc.position(S.symbol)
    assert pos.qty == -1200 and pos.avg_cost == D("9.5") * 1000 / 1200
    assert [e.type for e in entries] == ["dividend", "adjust"]


def test_accrue_financing():
    acc = AccountState(cash=D("-100000"), positions={S.symbol: Position(S.symbol, -1000, D("10"))})
    p = lev(margin_interest_rate="0.0365", borrow_fee_rate="0.073")
    entries = acc.accrue_financing({S.symbol: D("10")}, p, days=3)
    # 融资利息 100000 × 3.65% × 3 / 365 = 30；融券费 10000 × 7.3% × 3 / 365 = 6
    assert [(e.type, e.amount) for e in entries] == [("interest", D("-30.00")), ("borrow_fee", D("-6.00"))]
    assert acc.cash == D("-100036.00")
    assert AccountState(cash=D("100")).accrue_financing({}, p, 1) == []


def test_margin_ratio_methods():
    acc = AccountState(cash=D("-50000"), positions={S.symbol: Position(S.symbol, 10000, D("15"))})
    prices = {S.symbol: D("15")}
    assert acc.margin_ratio(prices, lev()) == D("100000") / D("150000")            # 净值 / 总敞口
    cn = lev(margin_check_method="collateral_ratio", maintenance_margin="1.3", liquidation_margin="1.1")
    assert acc.margin_ratio(prices, cn) == 3                                       # 150000 / 50000
    assert AccountState(cash=D("1"), positions={S.symbol: Position(S.symbol, 1, D("1"))}).margin_ratio(
        prices, lev()) is None                                                     # 没有负债


def test_buying_power():
    prices = {S.symbol: D("10")}
    acc = AccountState(cash=D("50000"), positions={S.symbol: Position(S.symbol, 5000, D("10"))})
    assert acc.buying_power(prices, RuleParams()) == D("50000")
    assert acc.buying_power(prices, lev()) == D("150000")              # 净值 10 万 × 2 − 敞口 5 万
    short = AccountState(cash=D("150000"), positions={S.symbol: Position(S.symbol, -5000, D("10"))})
    p = RuleParams(allow_short=True)
    assert short.buying_power(prices, p) == D("100000")                # 卖空所得不能用于买入
    assert short.short_room(prices, p) == D("150000")


# ---------------------------------------------------------------- 可平数量与撮合


def test_cn_short_t1_and_closable():
    pos = Position(S.symbol, -1000, D("10"), last_buy_date=T1, last_buy_qty=600)
    assert closable_qty(S, pos, T1, "cover", lev()) == 400
    assert closable_qty(S, pos, T2, "cover", lev()) == 1000
    assert closable_qty(S, pos, T1, "cover", lev(cn_short_t1=False)) == 1000
    assert closable_qty(S, pos, T1, "sell", lev()) == 0


def test_matching_short_side(params):
    b = bar(o="10", h="10.6", low="9.7")
    short = PendingOrder(1, S.symbol, "short", 100)
    assert match_order(short, 100, b, S, params).price == D("9.99")           # 按卖出减滑点
    stop = PendingOrder(2, S.symbol, "cover", 100, order_type="stop", stop_price=D("10.5"))
    r = match_order(stop, 100, b, S, params)
    assert r.price == D("10.51")                                               # 10.5 × (1 + 5bp) 向上取整
    assert match_order(stop, 100, bar(o="10", h="10.4", low="9.7"), S, params).waiting
    tp = PendingOrder(3, S.symbol, "cover", 100, order_type="take_profit", stop_price=D("9.8"))
    assert match_order(tp, 100, b, S, params).price == D("9.8")
    limit_down = bar(o="9", h="9", low="9", prev="10")
    assert "无法卖空" in match_order(short, 100, limit_down, S, params).reason


# ---------------------------------------------------------------- 结算


def test_settle_short_then_cover_and_margin_buy():
    p = lev(**NO_FEE)
    acc = AccountState(cash=D("100000"))
    orders = [PendingOrder(1, S.symbol, "short", 2000), PendingOrder(2, S2.symbol, "buy", 15000)]
    bars = {S.symbol: bar(o="10"), S2.symbol: bar(S2.symbol, o="10")}
    st = settle_bar(acc, orders, bars, INSTS, p, T1, True)
    assert [f.side for f in st.fills] == ["short", "buy"]
    # 融资买入：现金变为负数；总敞口 = 2 万空头 + 15 万多头 ≤ 净值 10 万 × 2
    assert acc.position(S.symbol).qty == -2000 and acc.position(S2.symbol).qty == 15000
    assert acc.cash < 0
    # 超出杠杆额度的买入只成交可承受的部分（剩余额度约 3 万）
    more = PendingOrder(3, S2.symbol, "buy", 10000)
    settle_bar(acc, [more], {S2.symbol: bar(S2.symbol, o="10", d=T2)}, INSTS, p, T2, True)
    assert more.status == "partially_filled" and more.filled_qty == 3000   # 净值 10 万 × 2 − 敞口 17 万
    cover = PendingOrder(4, S.symbol, "cover", 5000)
    settle_bar(acc, [cover], {S.symbol: bar(o="9", d=T2)}, INSTS, p, T2, True)
    assert cover.filled_qty == 2000 and acc.position(S.symbol) is None


def test_settle_blocks_direction_conflict():
    p = lev(**NO_FEE)
    acc = AccountState(cash=D("100000"), positions={S.symbol: Position(S.symbol, 1000, D("10"))})
    o = PendingOrder(1, S.symbol, "short", 100)
    settle_bar(acc, [o], {S.symbol: bar()}, INSTS, p, T1, True)
    assert o.status == "expired" and "须先卖出" in o.note
    c = PendingOrder(2, S.symbol, "cover", 100)
    settle_bar(acc, [c], {S.symbol: bar()}, INSTS, p, T1, True)
    assert c.status == "expired" and "没有可平的空头" in c.note


# ---------------------------------------------------------------- 风控预检


def _refs(**prices):
    return {sym: bar(sym, c=px, o=px) for sym, px in prices.items()}


def test_precheck_short_rules(params):
    acc = AccountState(cash=D("1000000"), positions={S.symbol: Position(S.symbol, 1000, D("10"))})
    refs = _refs(**{S.symbol: "10", S2.symbol: "10"})
    short = OrderIntent(S2.symbol, "short", 1000)
    r = precheck([short], acc, INSTS, refs, params, T2)
    assert "不允许卖空" in r.rejected[0][1]
    p = RuleParams(allow_short=True)
    r = precheck([OrderIntent(S.symbol, "short", 500)], acc, INSTS, refs, p, T2)
    assert "持有多头" in r.rejected[0][1]
    # 同一轮先卖出再卖空：多翻空
    r = precheck([OrderIntent(S.symbol, "short", 500), OrderIntent(S.symbol, "sell", 1000)], acc, INSTS, refs, p, T2)
    assert [i.side for i in r.accepted] == ["sell", "short"] and not r.rejected
    # 空头止损价必须高于现价
    held_short = AccountState(cash=D("1000000"), positions={S.symbol: Position(S.symbol, -1000, D("10"))})
    bad = OrderIntent(S.symbol, "cover", 1000, order_type="stop", stop_price=D("9"))
    assert "应高于当前价" in precheck([bad], held_short, INSTS, refs, p, T2).rejected[0][1]
    assert "请先平空" in precheck([OrderIntent(S.symbol, "buy", 100)], held_short, INSTS, refs, p, T2).rejected[0][1]


def test_precheck_leverage_limits():
    acc = AccountState(cash=D("100000"))
    refs = _refs(**{S.symbol: "10", S2.symbol: "10", E.symbol: "10"})
    p = lev(max_position_pct="1")
    r = precheck([OrderIntent(S.symbol, "buy", 9000), OrderIntent(S2.symbol, "short", 5000),
                  OrderIntent(E.symbol, "buy", 7000)], acc, INSTS, refs, p, T2)
    assert [i.symbol for i in r.accepted] == [S2.symbol, S.symbol]                    # 先卖空、后买入
    assert "资金不足" in r.rejected[0][1] and "购买力" in r.rejected[0][1]          # 总敞口将超过 2 倍
    assert "超过单票持仓上限" in precheck([OrderIntent(S.symbol, "buy", 15000)], acc, INSTS, refs, p,
                                     T2).rejected[0][1]
    cash_only = precheck([OrderIntent(S.symbol, "buy", 15000)], acc, INSTS, refs,
                         RuleParams(allow_short=True, max_position_pct="1"), T2)
    assert "资金不足" in cash_only.rejected[0][1]                                    # 只开做空不开融资


# ---------------------------------------------------------------- 目标仓位


def test_targets_short_and_flip():
    acc = AccountState(cash=D("50000"), positions={S.symbol: Position(S.symbol, 5000, D("10"))})
    refs = _refs(**{S.symbol: "10", S2.symbol: "10"})
    p = RuleParams(allow_short=True, cash_buffer_pct="0", slippage_bps="0")
    conv = targets_to_intents({S.symbol: D("-0.2"), S2.symbol: D("0.3")}, acc, INSTS, refs, p)
    assert [(i.side, i.symbol, i.qty) for i in conv.intents] == [
        ("sell", S.symbol, 5000), ("short", S.symbol, 2000), ("buy", S2.symbol, 3000)]
    long_only = targets_to_intents({S.symbol: D("-0.2")}, acc, INSTS, refs, RuleParams())
    assert [i.side for i in long_only.intents] == ["sell"] and "未开启做空" in long_only.notes[0]
    scaled = targets_to_intents({S.symbol: D("0.3"), S2.symbol: D("-0.3"), E.symbol: D("-0.3")}, acc, INSTS,
                                {**refs, **_refs(**{E.symbol: "10"})}, p.model_copy(update={"max_leverage": D("0.6")}))
    assert any("杠杆上限" in n for n in scaled.notes)


# ---------------------------------------------------------------- 保证金检查与强平


def test_margin_call_then_liquidation():
    p = lev()
    acc = AccountState(cash=D("-100000"), positions={S.symbol: Position(S.symbol, 10000, D("15")),
                                                     S2.symbol: Position(S2.symbol, 4000, D("10"))})
    prices = {S.symbol: D("12"), S2.symbol: D("10")}                # 净值 6 万 / 敞口 16 万 = 37.5%
    assert check_margin(acc, prices, p, False, INSTS).level == "ok"
    prices[S.symbol] = D("10.5")                                    # 净值 4.5 万 / 14.5 万 = 31% → 正常
    assert check_margin(acc, prices, p, False, INSTS).level == "ok"
    prices[S.symbol] = D("10")                                      # 4 万 / 14 万 = 28.6%：追保
    call = check_margin(acc, prices, p, False, INSTS)
    assert call.level == "call" and "维持线" in call.message and not call.orders
    liq = check_margin(acc, prices, p, True, INSTS)                 # 下一次检查仍不足：强平
    assert liq.level == "liquidate" and liq.orders[0].symbol == S.symbol and liq.orders[0].forced
    # 强平后比率恢复到维持线 + 5% 以上，且只平必要的整手数量
    after = AccountState(cash=acc.cash, positions={k: Position(k, v.qty, v.avg_cost) for k, v in acc.positions.items()})
    for o in liq.orders:
        after.apply_fill(Fill(0, o.symbol, o.side, o.qty, prices[o.symbol], {}, T2))
    assert after.margin_ratio(prices, p) >= D("0.35")
    assert liq.orders[0].qty < 10000


def test_liquidation_below_liquidation_line_and_shorts():
    p = lev(margin_check_method="collateral_ratio", maintenance_margin="1.3", liquidation_margin="1.1")
    acc = AccountState(cash=D("110000"), positions={S.symbol: Position(S.symbol, -10000, D("10"))})
    prices = {S.symbol: D("10.5")}                                   # 11 万 / 10.5 万 = 104.8% < 110%
    res = check_margin(acc, prices, p, False, INSTS)
    assert res.level == "liquidate" and res.orders[0].side == "cover"
    assert liquidation_orders(AccountState(cash=D("1")), {}, p, INSTS) == []


def test_collateral_liquidation_skips_longs_without_debt():
    # 没有融资负债（现金为正）时，卖出多头不能提高维持担保比例，只应平空
    p = lev(margin_check_method="collateral_ratio", maintenance_margin="2.7", liquidation_margin="2.5")
    acc = AccountState(cash=D("400000"), positions={S.symbol: Position(S.symbol, 120000, D("10")),
                                                    S2.symbol: Position(S2.symbol, -60000, D("10"))})
    prices = {S.symbol: D("10"), S2.symbol: D("10")}             # (40 万 + 120 万) / 60 万 = 266.7%
    orders = liquidation_orders(acc, prices, p, INSTS)
    assert [(o.side, o.symbol) for o in orders] == [("cover", S2.symbol)]
    after = AccountState(cash=acc.cash, positions={k: Position(k, v.qty, v.avg_cost) for k, v in acc.positions.items()})
    after.apply_fill(Fill(0, S2.symbol, "cover", orders[0].qty, D("10"), {}, T2))
    assert after.margin_ratio(prices, p) >= D("2.75")


def test_collateral_liquidation_sells_then_covers():
    # 有融资负债且没有正现金：先卖出多头还债，再卖出多头换现金平空
    p = lev(margin_check_method="collateral_ratio", maintenance_margin="1.3", liquidation_margin="1.1")
    acc = AccountState(cash=D("-10000"), positions={S.symbol: Position(S.symbol, 10000, D("10")),
                                                    S2.symbol: Position(S2.symbol, -7000, D("10"))})
    prices = {S.symbol: D("10"), S2.symbol: D("10")}             # 10 万 / (1 万 + 7 万) = 125%
    orders = liquidation_orders(acc, prices, p, INSTS)
    # 需要把负债从 8 万降到 2 万 / 0.35 ≈ 5.71 万：卖出 2.29 万（还清 1 万负债，余 1.29 万现金）并平空 1.29 万
    assert [(o.side, o.qty) for o in orders] == [("sell", 2300), ("cover", 1300)]


def test_insolvent_account_closes_everything():
    p = lev(margin_check_method="collateral_ratio", maintenance_margin="1.3", liquidation_margin="1.1")
    acc = AccountState(cash=D("-20000"), positions={S.symbol: Position(S.symbol, 10000, D("10")),
                                                    S2.symbol: Position(S2.symbol, -9000, D("10"))})
    prices = {S.symbol: D("10"), S2.symbol: D("10")}             # 净值 −1 万
    orders = liquidation_orders(acc, prices, p, INSTS)
    assert sorted((o.side, o.qty) for o in orders) == [("cover", 9000), ("sell", 10000)]
