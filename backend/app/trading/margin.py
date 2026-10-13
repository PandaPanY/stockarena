"""保证金检查与强制平仓计划（PRD 4.6.5）。纯逻辑，由日终流程调用并持久化结果。

每日收盘：
  比率 ≥ 维持线：正常（清除追保状态）
  强平线 ≤ 比率 < 维持线：首次 → 追保通知（下一轮 prompt 告知 AI）；上一次检查已在追保中 → 强制平仓
  比率 < 强平线：立即强制平仓
强平：按持仓市值从大到小依次平仓，直到比率恢复到维持线 + 缓冲；以下一根 K 线开盘价成交，订单标记 forced。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_CEILING, Decimal
from typing import Literal

from app.core.money import ZERO
from app.market.types import Instrument
from app.trading.market_rules import get_rules
from app.trading.params import RuleParams
from app.trading.portfolio import AccountState
from app.trading.types import OrderIntent

Level = Literal["ok", "call", "liquidate"]


@dataclass
class MarginCheck:
    ratio: Decimal | None
    level: Level
    message: str = ""
    orders: list[OrderIntent] = field(default_factory=list)     # 强平订单


def _fmt(x: Decimal | None) -> str:
    return "-" if x is None else f"{x * 100:.1f}%"


def check_margin(state: AccountState, prices: dict[str, Decimal], p: RuleParams, in_call: bool,
                 instruments: dict[str, Instrument]) -> MarginCheck:
    """in_call：上一次检查时是否已处于追保状态。"""
    ratio = state.margin_ratio(prices, p)
    if not p.leveraged or ratio is None or ratio >= p.maintenance_margin:
        return MarginCheck(ratio, "ok")
    name = "维持担保比例" if p.margin_check_method == "collateral_ratio" else "保证金比率"
    if ratio >= p.liquidation_margin and not in_call:
        return MarginCheck(ratio, "call",
                           f"{name} {_fmt(ratio)} 低于维持线 {_fmt(p.maintenance_margin)}，"
                           "请在下一次检查前降低仓位或杠杆，"
                           "否则将被强制平仓")
    why = (f"低于强平线 {_fmt(p.liquidation_margin)}" if ratio < p.liquidation_margin
           else "追保后仍低于维持线")
    orders = liquidation_orders(state, prices, p, instruments)
    return MarginCheck(ratio, "liquidate", f"{name} {_fmt(ratio)}，{why}，强制平仓 {len(orders)} 笔", orders)


def _to_qty(amount: Decimal, price: Decimal, held: int, inst: Instrument | None) -> int:
    """把要平仓的金额换算为股数：按交易单位向上取整，不超过持仓。"""
    if amount <= 0 or price <= 0:
        return 0
    step = max(get_rules(inst.market).buy_step(inst), 1) if inst else 1
    lots = (amount / price / step).to_integral_value(rounding=ROUND_CEILING)
    return min(int(lots) * step, held)


def _allocate(total: Decimal, legs: list[tuple[str, int, Decimal]]) -> dict[str, Decimal]:
    """把要平仓的金额按市值从大到小分配到各持仓：legs 为 (代码, 持仓股数, 价格)。"""
    out: dict[str, Decimal] = {}
    for sym, held, price in sorted(legs, key=lambda x: -x[1] * x[2]):
        if total <= 0:
            break
        take = min(total, held * price)
        out[sym] = take
        total -= take
    return out


def liquidation_orders(state: AccountState, prices: dict[str, Decimal], p: RuleParams,
                       instruments: dict[str, Instrument]) -> list[OrderIntent]:
    """计算恢复到维持线 + 缓冲所需的最小平仓（按市值从大到小、整手向上取整，不计费用）。

    - equity_ratio（净值 / 总敞口）：任何平仓都降低总敞口、净值不变，需要把总敞口降到 净值 / 目标
    - collateral_ratio（总资产 / 总负债）：总资产 − 总负债 = 净值，比率 = 1 + 净值 / 总负债，需要把总负债降到
      净值 / (目标 − 1)。负债只能这样减少：卖出多头偿还融资负债 → 用正现金平空 → 仍不够则卖出多头换现金再平空。
      没有融资负债时单独卖出多头只是把股票换成现金，比率不变，因此不会被选中
    """
    target = p.maintenance_margin + p.liquidation_buffer
    nav = state.nav(prices)
    longs = [(sym, pos.qty, prices[sym]) for sym, pos in state.positions.items() if pos.qty > 0]
    shorts = [(sym, -pos.qty, prices[sym]) for sym, pos in state.positions.items() if pos.qty < 0]
    if nav <= 0 or (p.margin_check_method == "collateral_ratio" and target <= 1):
        # 穿仓：部分平仓无法恢复比率，全部平仓
        sell = {sym: q * px for sym, q, px in longs}
        cover = {sym: q * px for sym, q, px in shorts}
        reason = "强制平仓：账户净值已为负，全部平仓"
    elif p.margin_check_method == "collateral_ratio":
        debt = -state.cash if state.cash < 0 else ZERO
        liabilities = debt + state.short_value(prices)
        need = liabilities - nav / (target - 1)
        repay = min(debt, need)                                      # 1. 卖出多头偿还融资负债
        with_cash = min(max(state.cash, ZERO), need - repay)         # 2. 用正现金平空
        rest = need - repay - with_cash                              # 3. 卖出多头换现金再平空
        long_value = state.long_value(prices)
        rest = min(rest, max(long_value - repay, ZERO))
        sell = _allocate(repay + rest, longs)
        cover = _allocate(with_cash + rest, shorts)
        reason = f"强制平仓：维持担保比例低于要求（目标恢复到 {target * 100:.0f}%）"
    else:
        need = state.gross_exposure(prices) - nav / target
        plan = _allocate(need, longs + shorts)
        long_syms = {sym for sym, _, _ in longs}
        sell = {k: v for k, v in plan.items() if k in long_syms}
        cover = {k: v for k, v in plan.items() if k not in long_syms}
        reason = f"强制平仓：保证金比率低于要求（目标恢复到 {target * 100:.0f}%）"
    out: list[OrderIntent] = []
    for side, plan in (("sell", sell), ("cover", cover)):
        for sym, amount in plan.items():
            qty = _to_qty(amount, prices[sym], abs(state.positions[sym].qty), instruments.get(sym))
            if qty > 0:
                out.append(OrderIntent(sym, side, qty, reason=reason, forced=True))
    return out
