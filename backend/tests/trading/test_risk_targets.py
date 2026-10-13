from __future__ import annotations

from app.trading.portfolio import AccountState
from app.trading.risk import precheck
from app.trading.targets import targets_to_intents
from app.trading.types import OrderIntent, Position
from tests.trading.conftest import T1, T2, D, bar, etf, stock

S = stock()
STAR = stock("688981.SH", board="star")
E = etf()
INSTS = {i.symbol: i for i in (S, STAR, E)}
BARS = {S.symbol: bar(S.symbol, o="10"), STAR.symbol: bar(STAR.symbol, o="50", prev="50"),
        E.symbol: bar(E.symbol, o="4", prev="4")}


def _reasons(result):
    return [r for _, r in result.rejected]


def test_precheck_accepts_valid_orders(params):
    acc = AccountState(cash=D("100000"))
    r = precheck([OrderIntent(S.symbol, "buy", 1000)], acc, INSTS, BARS, params, T2)
    assert len(r.accepted) == 1 and not r.rejected


def test_precheck_rejections(params):
    acc = AccountState(cash=D("100000"), positions={S.symbol: Position(S.symbol, 100, D("10"))})
    intents = [
        OrderIntent("000001.SZ", "buy", 100),          # 不在股票池
        OrderIntent(S.symbol, "sell", 500),            # 超过持仓
        OrderIntent(STAR.symbol, "buy", 100),          # 科创板不足 200 股
        OrderIntent(E.symbol, "buy", 150),             # 非整手
    ]
    reasons = _reasons(precheck(intents, acc, INSTS, BARS, params, T2))
    assert "标的不在赛道股票池内" in reasons
    assert any("可卖数量不足" in r for r in reasons)
    assert any("科创板" in r for r in reasons)
    assert any("整数倍" in r for r in reasons)


def test_precheck_cash_includes_same_round_sell_proceeds(params):
    acc = AccountState(cash=D("1000"), positions={E.symbol: Position(E.symbol, 10000, D("4"))})
    # 净值 41,000；买入 1.2 万元（< 30% 上限），现金只有 1,000，需要用同轮卖出 ETF 的约 4 万元
    intents = [OrderIntent(S.symbol, "buy", 1200), OrderIntent(E.symbol, "sell", 10000)]
    r = precheck(intents, acc, INSTS, BARS, params, T2)
    assert not r.rejected


def test_precheck_insufficient_cash(params):
    r = precheck([OrderIntent(S.symbol, "buy", 2000)], AccountState(cash=D("10000")), INSTS, BARS, params, T2)
    assert "资金不足" in _reasons(r)[0]


def test_precheck_max_position_pct(params):
    acc = AccountState(cash=D("100000"))
    r = precheck([OrderIntent(S.symbol, "buy", 4000)], acc, INSTS, BARS, params, T2)   # 4 万 > 净值 30%
    assert "单票持仓上限" in _reasons(r)[0]


def test_precheck_duplicate_symbol_and_max_orders(params):
    acc = AccountState(cash=D("1000000"))
    r = precheck([OrderIntent(S.symbol, "buy", 100), OrderIntent(S.symbol, "buy", 100)],
                 acc, INSTS, BARS, params, T2)
    assert any("同一标的本轮只能下一笔" in x for x in _reasons(r))
    small = params.model_copy(update={"max_orders_per_round": 1})
    r = precheck([OrderIntent(S.symbol, "buy", 100), OrderIntent(E.symbol, "buy", 100)], acc, INSTS, BARS, small, T2)
    assert any("订单数上限" in x for x in _reasons(r))


def test_precheck_t1_same_day_sell_blocked(params):
    # 分钟级场景：当天买入的 T+1 股票当天不可卖
    acc = AccountState(cash=D("0"), positions={S.symbol: Position(S.symbol, 100, D("10"), T1, 100)})
    assert _reasons(precheck([OrderIntent(S.symbol, "sell", 100)], acc, INSTS, BARS, params, T1))
    assert not precheck([OrderIntent(S.symbol, "sell", 100)], acc, INSTS, BARS, params, T2).rejected


def test_targets_build_from_cash(params):
    acc = AccountState(cash=D("100000"))
    conv = targets_to_intents({S.symbol: D("0.2"), E.symbol: D("0.3")}, acc, INSTS, BARS, params)
    by = {i.symbol: i for i in conv.intents}
    # 可投资 = 100000 × 99.5%；股票 19900 / (10 × 1.0005) → 1900 股（整手）
    assert by[S.symbol].qty == 1900 and by[S.symbol].side == "buy"
    assert by[E.symbol].qty == 7400   # 29850 / (4 × 1.0005) = 7458 → 7400
    assert not precheck(conv.intents, acc, INSTS, BARS, params, T2).rejected


def test_targets_unlisted_positions_are_sold_and_sells_first(params):
    acc = AccountState(cash=D("0"), positions={S.symbol: Position(S.symbol, 1000, D("10"))})
    conv = targets_to_intents({E.symbol: D("0.5")}, acc, INSTS, BARS, params)
    assert [(i.symbol, i.side, i.qty) for i in conv.intents][0] == (S.symbol, "sell", 1000)
    assert conv.intents[1].side == "buy"


def test_targets_clip_and_scale(params):
    acc = AccountState(cash=D("100000"))
    conv = targets_to_intents({S.symbol: D("0.9"), E.symbol: D("0.9"), "000001.SZ": D("0.1")},
                              acc, INSTS, BARS, params)
    assert any("截断" in n for n in conv.notes)
    assert any("不在股票池" in n for n in conv.notes)


def test_targets_small_rebalance_ignored(params):
    acc = AccountState(cash=D("80000"), positions={S.symbol: Position(S.symbol, 2000, D("10"))})
    # 当前权重 20%，目标 20.5%，差额小于净值 1%，不调仓
    conv = targets_to_intents({S.symbol: D("0.205")}, acc, INSTS, BARS, params)
    assert conv.intents == []



def test_targets_new_position_free_of_threshold(params):
    # 新建仓 0.6%（约 600 元）低于 1% 门槛：默认忽略；开启 rebalance_new_free 后照常买入；已有持仓的微调仍受门槛限制
    acc = AccountState(cash=D("80000"), positions={S.symbol: Position(S.symbol, 2000, D("10"))})
    targets = {S.symbol: D("0.205"), E.symbol: D("0.006")}
    assert targets_to_intents(targets, acc, INSTS, BARS, params).intents == []
    free = params.model_copy(update={"rebalance_new_free": True})
    conv = targets_to_intents(targets, acc, INSTS, BARS, free)
    assert [(i.symbol, i.side) for i in conv.intents] == [(E.symbol, "buy")]

def test_targets_star_board_minimum(params):
    acc = AccountState(cash=D("100000"))
    conv = targets_to_intents({STAR.symbol: D("0.05")}, acc, INSTS, BARS, params)  # 约 99 股 < 200
    assert conv.intents == []


def test_targets_sub_lot_weight_is_adjustment_not_exit(params):
    # 目标权重 > 0 但不足一手：不算清仓，按减仓处理并受门槛（净值 1%）限制
    acc = AccountState(cash=D("99000"), positions={S.symbol: Position(S.symbol, 100, D("10"))})
    assert targets_to_intents({S.symbol: D("0.009")}, acc, INSTS, BARS, params).intents == []   # 差 105 元 < 1000
    acc = AccountState(cash=D("98000"), positions={S.symbol: Position(S.symbol, 200, D("10"))})
    conv = targets_to_intents({S.symbol: D("0.0009")}, acc, INSTS, BARS, params)               # 差约 1910 元
    assert [(i.symbol, i.side, i.qty) for i in conv.intents] == [(S.symbol, "sell", 200)]
