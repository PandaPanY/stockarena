"""日终结算：除权除息 → 撮合待成交订单 → 订单状态更新。纯函数式，结果由调用方持久化。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from app.market.types import CorporateAction, DailyBar, Instrument
from app.trading.fees import dividend_tax_rate
from app.trading.market_rules import closable_qty, get_rules
from app.trading.matching import match_order
from app.trading.params import RuleParams
from app.trading.portfolio import AccountState
from app.trading.types import EngineEvent, Fill, LedgerEntry, PendingOrder, trade_side

# 先平仓后开仓：卖出、平空释放资金和额度，再卖空、买入
_SIDE_ORDER = {"sell": 0, "cover": 1, "short": 2, "buy": 3}


@dataclass
class Settlement:
    fills: list[Fill] = field(default_factory=list)
    ledger: list[LedgerEntry] = field(default_factory=list)
    events: list[EngineEvent] = field(default_factory=list)
    orders: list[PendingOrder] = field(default_factory=list)   # 状态已更新的订单


def _affordable_qty(inst: Instrument, qty: int, price: Decimal, budget: Decimal, p: RuleParams,
                    side: str = "buy") -> int:
    """资金（买入：含费用的金额）或额度（卖空：市值）不足时按整手递减，找出可成交的最大数量。"""
    rules = get_rules(inst.market)
    step, minimum = rules.buy_step(inst), rules.min_buy(inst)
    # 先按不含费用的金额估算上限，再逐步扣减直到含费用也买得起
    q = min(qty, int(budget / price) if price > 0 else 0)
    q -= q % step
    while q >= minimum:
        cost = price * q
        if side == "buy":
            cost += sum(rules.calc_fees(inst, "buy", q, price, p).values())
        if cost <= budget:
            return q
        q -= step
    return 0


def _valuation(account: AccountState, bars: dict[str, DailyBar], marks: dict[str, Decimal] | None) -> dict:
    """估算净值用的价格：本根 K 线开盘价 > 调用方提供的参考价 > 持仓成本。"""
    prices = dict(marks or {})
    for sym, b in bars.items():
        prices[sym] = b.open
    for sym, pos in account.positions.items():
        prices.setdefault(sym, pos.avg_cost)
    return prices


def settle_day(
    account: AccountState,
    orders: list[PendingOrder],
    bars: dict[str, DailyBar],
    instruments: dict[str, Instrument],
    actions: list[CorporateAction],
    params: RuleParams,
    today: date,
    marks: dict[str, Decimal] | None = None,
) -> Settlement:
    """日频：处理 today 这个交易日（除权除息 → 以日线撮合所有生效订单，当日有效的订单收盘后失效）。"""
    st = apply_actions(account, actions, params, today, instruments)
    bar_st = settle_bar(account, orders, bars, instruments, params, today, end_of_session=True, marks=marks)
    st.fills += bar_st.fills
    st.ledger += bar_st.ledger
    st.events += bar_st.events
    st.orders += bar_st.orders
    return st


def apply_actions(account: AccountState, actions: list[CorporateAction], params: RuleParams,
                  today: date, instruments: dict[str, Instrument] | None = None) -> Settlement:
    """开盘前处理除权除息。红利税率按规则参数（A股可按持有期差别化计算，需要 instruments 判断股票 / 基金）。"""
    st = Settlement()
    for act in actions:
        pos = account.positions.get(act.symbol)
        rate = dividend_tax_rate((instruments or {}).get(act.symbol), pos.opened_date if pos else None, today, params)
        entries = account.apply_corporate_action(act, today, rate)
        if entries:
            st.ledger.extend(entries)
            st.events.append(EngineEvent("corporate_action", "；".join(e.note for e in entries), act.symbol))
    return st


def settle_bar(
    account: AccountState,
    orders: list[PendingOrder],
    bars: dict[str, DailyBar],
    instruments: dict[str, Instrument],
    params: RuleParams,
    trade_date: date,
    end_of_session: bool,
    marks: dict[str, Decimal] | None = None,
) -> Settlement:
    """用一根 K 线（日线或分钟线）撮合生效中的订单。account 原地修改；返回状态有变化的订单。

    - 先平仓后开仓（卖出 → 平空 → 卖空 → 买入），平仓所得可用于开仓
    - 买入受购买力约束，卖空受杠杆额度约束（marks 为估值参考价，用于融资 / 卖空账户计算净值）
    - 未触及价格的限价 / 条件单继续等待；end_of_session 时当日有效（day）的订单失效
    - 同一 OCO 组（止损 + 止盈）任一成交后，其余撤销
    """
    st = Settlement()
    filled_groups: set[int] = set()
    for order in sorted(orders, key=lambda o: _SIDE_ORDER.get(o.side, 9)):
        if order.status != "pending":
            continue
        if order.oco_group is not None and order.oco_group in filled_groups:
            _finish(order, st, "cancelled", "同组条件单已成交，自动撤销")
            continue
        inst = instruments[order.symbol]
        rules = get_rules(inst.market)
        bar = bars.get(order.symbol)
        qty = order.qty - order.filled_qty
        sell_all = False
        held = account.position(order.symbol)
        if order.side in ("sell", "cover"):
            available = closable_qty(inst, held, trade_date, order.side, params)
            if available <= 0:
                long_side = order.side == "sell"
                if held is None or (held.qty <= 0 if long_side else held.qty >= 0):
                    _finish(order, st, "cancelled" if order.conditional else "expired",
                            "没有可卖持仓" if long_side else "没有可平的空头")
                elif end_of_session and order.tif == "day":
                    _finish(order, st, "expired",
                            "持仓当日不可卖（T+1）" if long_side else "当日融券卖出的证券当日不能平空")
                continue
            qty = min(qty, available)
            sell_all = qty == available
        elif held is not None and (held.qty < 0 if order.side == "buy" else held.qty > 0):
            _finish(order, st, "expired", "持有空头，不能直接买入（须先平空）" if order.side == "buy"
                    else "持有多头，不能直接卖空（须先卖出）")
            continue
        result = match_order(order, qty, bar, inst, params, sell_all)
        if result.qty == 0 or result.price is None:
            # 暂时无法成交（未触及价格、停牌、一字板、量不足）：收盘时市价单与当日有效订单失效，GTC 继续等待
            if end_of_session and (order.order_type == "market" or order.tif == "day"):
                _finish(order, st, "expired", result.reason or "当日未成交")
            continue

        fill_qty = result.qty
        note = result.reason
        if order.side in ("buy", "short"):
            prices = _valuation(account, bars, marks)
            budget = (account.buying_power(prices, params) if order.side == "buy"
                      else account.short_room(prices, params))
            fill_qty = _affordable_qty(inst, fill_qty, result.price, budget, params, order.side)
            short_of = "资金不足" if order.side == "buy" else "可卖空额度不足"
            if fill_qty == 0:
                _finish(order, st, "expired", f"{short_of}，无法成交")
                continue
            if fill_qty < result.qty:
                note = f"{short_of}，部分成交"

        fill = Fill(order.id, order.symbol, order.side, fill_qty, result.price,
                    rules.calc_fees(inst, trade_side(order.side), fill_qty, result.price, params), trade_date)
        st.ledger.extend(account.apply_fill(fill))
        st.fills.append(fill)
        order.filled_qty += fill_qty
        if order.oco_group is not None:
            filled_groups.add(order.oco_group)
        if order.filled_qty >= order.qty:
            order.status = "filled"
        elif not end_of_session and order.side in ("buy", "short") and note.endswith("不足，部分成交"):
            order.status = "partially_filled"
            order.note = note
        elif not end_of_session:
            # 分钟级：本分钟成交量不够，剩余部分在后续分钟继续成交，收盘仍未成交的按有效期处理
            order.note = f"已成交 {order.filled_qty}/{order.qty}，剩余部分继续等待"
        else:
            # 简化处理：部分成交后剩余部分失效（G-05）
            order.status = "partially_filled"
            order.note = note or "部分成交，剩余部分已失效"
            st.events.append(EngineEvent("partial_fill", order.note, order.symbol,
                                         {"order_id": order.id, "filled": order.filled_qty, "qty": order.qty}))
        st.orders.append(order)
    # 同组中排在前面、本根 K 线未触发的条件单也要撤销；持仓已经平掉的止损止盈单同样撤销
    for order in orders:
        if order.status != "pending":
            continue
        if order.oco_group is not None and order.oco_group in filled_groups:
            _finish(order, st, "cancelled", "同组条件单已成交，自动撤销")
        elif order.conditional:
            held = account.position(order.symbol)
            if held is None or (held.qty <= 0 if order.side == "sell" else held.qty >= 0):
                _finish(order, st, "cancelled", "持仓已平，条件单自动撤销")
    return st


def _finish(order: PendingOrder, st: Settlement, status: str, reason: str) -> None:
    order.status = status  # type: ignore[assignment]
    order.note = reason
    st.orders.append(order)
    st.events.append(EngineEvent(f"order_{status}", reason, order.symbol, {"order_id": order.id}))
