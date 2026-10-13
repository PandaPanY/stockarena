"""撮合：基于 K 线（日线或分钟线）的订单成交（PRD 4.6.2）。

- 市价单：以 K 线开盘价 ± 滑点成交
- 限价单：买单最低价触及限价时以 min(开盘价, 限价) 成交；卖单对称
- 止损单（卖出）：最低价触及止损价时触发，以 min(开盘价, 止损价) 减滑点成交（跳空低开按开盘价）
- 止盈单（卖出）：最高价触及止盈价时以 max(开盘价, 止盈价) 成交
- 空头的止损 / 止盈（平空）方向相反：止损在最高价触及时以 max(开盘价, 止损价) 加滑点成交，止盈在最低价触及时成交
- 价格、滑点、涨跌停按资金方向处理：平空等同买入，卖空等同卖出
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.market.types import DailyBar, Instrument
from app.trading.market_rules import get_rules, round_price
from app.trading.params import RuleParams
from app.trading.types import OPENING_SIDES, PendingOrder, Side, trade_side

BPS = Decimal("10000")


@dataclass(frozen=True)
class MatchResult:
    qty: int                     # 可成交数量（0 表示无法成交）
    price: Decimal | None
    reason: str = ""             # 无法成交或部分成交的原因
    waiting: bool = False        # 价格尚未触及或暂时无法成交（限价 / 止损 / 止盈），订单继续等待


def fill_price(side: Side, bar: DailyBar, inst: Instrument, p: RuleParams) -> Decimal:
    """以开盘价加滑点成交（买入加、卖出减），按最小价位向不利方向取整（G-02），并限制在当日最高最低价与涨跌停价之内。"""
    slip = p.slippage_bps / BPS
    down, up = get_rules(inst.market).price_limits(inst, bar.prev_close)
    if trade_side(side) == "buy":
        price = round_price(inst, bar.open * (1 + slip), "up")
        price = min(price, bar.high)
        if up is not None:
            price = min(price, up)
    else:
        price = round_price(inst, bar.open * (1 - slip), "down")
        price = max(price, bar.low)
        if down is not None:
            price = max(price, down)
    return price


def match_market_order(
    side: Side, qty: int, bar: DailyBar | None, inst: Instrument, p: RuleParams, sell_all: bool = False
) -> MatchResult:
    """撮合一笔市价单。sell_all 表示卖出全部可卖数量（允许零股）。"""
    if bar is None or bar.suspended:
        return MatchResult(0, None, "停牌或无行情", waiting=True)                 # CN-12

    rules = get_rules(inst.market)
    blocked = _one_price_block(side, bar, inst)
    if blocked:
        return MatchResult(0, None, blocked, waiting=True)                           # CN-11

    # G-04：单笔成交量不超过 K 线成交量 × 参与率，按交易单位向下取整
    cap = int(Decimal(bar.volume) * p.participation_rate)
    cap -= cap % max(inst.lot_size, 1)
    filled = min(qty, cap)
    reason = ""
    if filled < qty:
        if side not in OPENING_SIDES and sell_all and cap >= qty:
            filled = qty
        elif side in OPENING_SIDES and filled < rules.min_buy(inst):
            filled = 0
        reason = f"成交量受限（上限 {cap}），部分成交" if filled else "成交量不足，无法成交"
    if filled <= 0:
        return MatchResult(0, None, reason or "成交量不足，无法成交", waiting=True)
    return MatchResult(filled, fill_price(side, bar, inst, p), reason)


def _one_price_block(side: Side, bar: DailyBar, inst: Instrument) -> str:
    """一字涨停无法买入 / 平空，一字跌停无法卖出 / 卖空。"""
    if bar.high != bar.low:
        return ""
    down, up = get_rules(inst.market).price_limits(inst, bar.prev_close)
    if trade_side(side) == "buy" and up is not None and bar.high >= up:
        return "一字涨停，无法买入" if side == "buy" else "一字涨停，无法平空"
    if trade_side(side) == "sell" and down is not None and bar.low <= down:
        return "一字跌停，无法卖出" if side == "sell" else "一字跌停，无法卖空"
    return ""


def _clamp(price: Decimal, bar: DailyBar, down: Decimal | None, up: Decimal | None) -> Decimal:
    price = min(max(price, bar.low), bar.high)
    if up is not None:
        price = min(price, up)
    if down is not None:
        price = max(price, down)
    return price


def match_order(order: PendingOrder, qty: int, bar: DailyBar | None, inst: Instrument, p: RuleParams,
                sell_all: bool = False) -> MatchResult:
    """按订单类型撮合。市价单沿用 match_market_order；限价单与条件单先判断是否触及价格。"""
    if order.order_type == "market":
        return match_market_order(order.side, qty, bar, inst, p, sell_all)
    if bar is None or bar.suspended:
        return MatchResult(0, None, "停牌或无行情", waiting=True)
    rules = get_rules(inst.market)
    down, up = rules.price_limits(inst, bar.prev_close)
    slip = p.slippage_bps / BPS
    pays = trade_side(order.side) == "buy"          # 买入 / 平空
    trigger = order.stop_price
    if order.order_type == "limit":
        limit = order.limit_price
        if pays:
            if bar.low > limit:
                return MatchResult(0, None, "未触及限价", waiting=True)
            price = round_price(inst, min(bar.open, limit), "down")
        else:
            if bar.high < limit:
                return MatchResult(0, None, "未触及限价", waiting=True)
            price = round_price(inst, max(bar.open, limit), "up")
    elif order.order_type == "stop":
        if pays:       # 空头止损：价格涨到止损价
            if bar.high < trigger:
                return MatchResult(0, None, "未触发止损", waiting=True)
            price = round_price(inst, max(bar.open, trigger) * (1 + slip), "up")
        else:
            if bar.low > trigger:
                return MatchResult(0, None, "未触发止损", waiting=True)
            price = round_price(inst, min(bar.open, trigger) * (1 - slip), "down")
    else:  # take_profit
        if pays:       # 空头止盈：价格跌到止盈价
            if bar.low > trigger:
                return MatchResult(0, None, "未触及止盈", waiting=True)
            price = round_price(inst, min(bar.open, trigger), "down")
        else:
            if bar.high < trigger:
                return MatchResult(0, None, "未触及止盈", waiting=True)
            price = round_price(inst, max(bar.open, trigger), "up")
    price = _clamp(price, bar, down, up)

    blocked = _one_price_block(order.side, bar, inst)
    if blocked:
        return MatchResult(0, None, blocked, waiting=True)
    sized = match_market_order(order.side, qty, bar, inst, p, sell_all)
    if sized.qty == 0:
        return MatchResult(0, None, sized.reason, waiting=True)
    return MatchResult(sized.qty, price, sized.reason)
