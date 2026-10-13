"""风控预检（PRD 4.6.3）：决策阶段对下单意图逐笔检查，拒绝的订单附带原因反馈给 AI。

决策在 T 日收盘后进行，订单在下一交易日开盘撮合，因此用 T 日收盘价作为参考价估算金额。
同一轮的订单按「卖出 → 平空 → 卖空 → 买入」的顺序在账户副本上模拟成交，后面的订单基于模拟后的资金、
额度和持仓方向检查（例如先卖出再卖空，即可把多头翻成空头）。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from app.market.types import DailyBar, Instrument
from app.trading.market_rules import closable_qty, get_rules, is_leveraged_etf, validate_side_qty
from app.trading.matching import BPS
from app.trading.params import RuleParams
from app.trading.portfolio import AccountState
from app.trading.types import CLOSING_SIDES, Fill, OrderIntent, trade_side

_SIDE_ORDER = {"sell": 0, "cover": 1, "short": 2, "buy": 3}
# 同一标的同一轮只允许一笔买卖订单；例外是「先平仓再反向开仓」
_FLIPS = {("sell", "short"), ("cover", "buy")}


@dataclass
class PrecheckResult:
    accepted: list[OrderIntent] = field(default_factory=list)
    rejected: list[tuple[OrderIntent, str]] = field(default_factory=list)


def precheck(
    intents: list[OrderIntent],
    account: AccountState,
    instruments: dict[str, Instrument],
    ref_bars: dict[str, DailyBar],
    params: RuleParams,
    fill_date: date,
) -> PrecheckResult:
    """instruments 为赛道股票池；ref_bars 为决策时点的最新日线（参考价 = 收盘价）。"""
    result = PrecheckResult()
    prices = {s: b.close for s, b in ref_bars.items()}
    for sym, pos in account.positions.items():
        prices.setdefault(sym, pos.avg_cost)
    nav = account.nav(prices)
    slip = params.slippage_bps / BPS
    est = copy.deepcopy(account)
    seen: dict[str, str] = {}

    for intent in sorted(intents, key=lambda i: _SIDE_ORDER.get(i.side, 9)):
        reason = _check_one(intent, est, instruments, ref_bars, params, fill_date, seen, len(result.accepted))
        if reason is None:
            reason = _check_prices(intent, prices[intent.symbol])
        if reason is None and not _is_conditional(intent):
            reason = _simulate(intent, est, instruments[intent.symbol], prices, nav, params, slip, fill_date)
        if reason is None:
            if not _is_conditional(intent):
                seen[intent.symbol] = intent.side
            result.accepted.append(intent)
        else:
            result.rejected.append((intent, reason))
    return result


def _simulate(intent: OrderIntent, est: AccountState, inst: Instrument, prices: dict[str, Decimal], nav: Decimal,
              p: RuleParams, slip: Decimal, fill_date: date) -> str | None:
    """资金、额度、集中度检查，通过后在账户副本上按参考价模拟成交。"""
    ref = prices[intent.symbol]
    pays = trade_side(intent.side) == "buy"
    price = ref * (1 + slip) if pays else ref * (1 - slip)
    if intent.order_type == "limit":
        if not pays and intent.side == "sell":
            return None            # 限价卖出不一定成交，不计入可用资金
        price = min(price, intent.limit_price) if pays else max(price, intent.limit_price)
    fees = get_rules(inst.market).calc_fees(inst, trade_side(intent.side), intent.qty, price, p)
    fee = sum(fees.values(), Decimal("0"))
    if intent.side == "buy":
        cost = price * intent.qty + fee
        power = est.buying_power(prices, p)
        if cost > power:
            what = "购买力" if p.allow_margin else "可用"
            return f"资金不足（预估需要 {cost:.2f}，{what}约 {power:.2f}）"
    elif intent.side == "short":
        room = est.short_room(prices, p)
        if price * intent.qty > room:
            return f"超过杠杆上限（净值的 {p.leverage_cap:g} 倍），还可卖空约 {room:.2f}"
    if intent.side in ("buy", "short"):
        pos = est.position(intent.symbol)
        held = abs(pos.qty) if pos else 0
        if (held + intent.qty) * ref > nav * p.max_position_pct:
            return f"超过单票持仓上限（净值的 {p.max_position_pct:.0%}）"
    est.apply_fill(Fill(0, intent.symbol, intent.side, intent.qty, price, fees, fill_date))
    return None


def _is_conditional(intent: OrderIntent) -> bool:
    return intent.order_type in ("stop", "take_profit")


def _check_prices(intent: OrderIntent, ref: Decimal) -> str | None:
    """限价、止损、止盈价格相对参考价（最新收盘价）的合理性。空头方向相反：止损在上方，止盈在下方。"""
    short_side = intent.side in ("short", "cover")
    if intent.order_type == "stop":
        if not short_side and intent.stop_price >= ref:
            return f"止损价 {intent.stop_price} 应低于当前价 {ref}"
        if short_side and intent.stop_price <= ref:
            return f"空头止损价 {intent.stop_price} 应高于当前价 {ref}"
    if intent.order_type == "take_profit":
        if not short_side and intent.stop_price <= ref:
            return f"止盈价 {intent.stop_price} 应高于当前价 {ref}"
        if short_side and intent.stop_price >= ref:
            return f"空头止盈价 {intent.stop_price} 应低于当前价 {ref}"
    if intent.attach_stop is not None and (intent.attach_stop <= ref if short_side else intent.attach_stop >= ref):
        return f"附带止损价 {intent.attach_stop} 应{'高' if short_side else '低'}于当前价 {ref}"
    if intent.attach_take_profit is not None and (intent.attach_take_profit >= ref if short_side
                                                  else intent.attach_take_profit <= ref):
        return f"附带止盈价 {intent.attach_take_profit} 应{'低' if short_side else '高'}于当前价 {ref}"
    return None


def _check_one(intent: OrderIntent, est: AccountState, instruments: dict[str, Instrument],
               ref_bars: dict[str, DailyBar], params: RuleParams, fill_date: date,
               seen: dict[str, str], accepted_count: int) -> str | None:
    if accepted_count >= params.max_orders_per_round:
        return f"超过单轮订单数上限（{params.max_orders_per_round}）"
    inst = instruments.get(intent.symbol)
    if inst is None:
        return "标的不在赛道股票池内"
    if inst.type == "index":
        return "指数不可交易"
    if intent.side not in ("buy", "sell", "short", "cover"):
        return f"无效的方向：{intent.side}"
    if intent.side in ("short", "cover") and not params.allow_short:
        return "当前赛道不允许卖空（只能 buy / sell）"
    if is_leveraged_etf(inst) and not params.allow_leveraged_etf:
        return "当前赛道不允许交易杠杆 / 反向 ETF"
    if intent.side == "short" and inst.type not in ("stock", "etf"):
        return "该品种不可卖空"
    if not _is_conditional(intent) and intent.symbol in seen and (seen[intent.symbol], intent.side) not in _FLIPS:
        return "同一标的本轮只能下一笔买卖订单（止损 / 止盈单、先平仓再反向开仓除外）"
    if ref_bars.get(intent.symbol) is None:
        return "缺少该标的行情"
    pos = est.position(intent.symbol)
    if intent.side == "short" and pos is not None and pos.qty > 0:
        return "持有多头时不能直接卖空，请先卖出（同一轮先 sell 再 short 即可）"
    if intent.side == "buy" and pos is not None and pos.qty < 0:
        return "持有空头时不能直接买入，请先平空（cover）"
    if intent.side in CLOSING_SIDES:
        if _is_conditional(intent):
            # 条件单可以等到持仓可平时再触发，因此按同方向持仓总量检查
            long_side = intent.side == "sell"
            closable = (max(pos.qty, 0) if long_side else max(-pos.qty, 0)) if pos else 0
        else:
            closable = closable_qty(inst, pos, fill_date, intent.side, params)
        return validate_side_qty(inst, intent.side, intent.qty, closable)
    return validate_side_qty(inst, intent.side, intent.qty, 0)
