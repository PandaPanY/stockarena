"""A股交易规则（附录 6.3 CN-01～CN-14）。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.core.money import round_to_tick
from app.market.types import Instrument
from app.trading.types import Position, Side

STAR_MIN_BUY = 200


def sellable_qty(inst: Instrument, pos: Position | None, on: date) -> int:
    """在 on 日可卖出的数量。T+1 品种当天买入的部分不可卖（CN-07）。"""
    if pos is None or pos.qty <= 0:
        return 0
    if inst.t0 or pos.last_buy_date != on:
        return pos.qty
    return max(pos.qty - pos.last_buy_qty, 0)


def validate_qty(inst: Instrument, side: Side, qty: int, sellable: int) -> str | None:
    """数量规则校验，返回 None 表示合规，否则返回拒绝原因。"""
    if qty <= 0:
        return "数量必须为正整数"
    if side == "buy":
        if inst.board == "star":
            # CN-02：科创板买入不少于 200 股，超出部分以 1 股递增
            return None if qty >= STAR_MIN_BUY else f"科创板买入数量不少于 {STAR_MIN_BUY} 股"
        # CN-01：买入为每手股数（100）的整数倍
        return None if qty % inst.lot_size == 0 else f"买入数量须为 {inst.lot_size} 的整数倍"
    if qty > sellable:
        return f"可卖数量不足（可卖 {sellable}）"
    if inst.board == "star":
        # 科创板卖出：不少于 200 股，余额不足 200 股时须一次性卖出
        if qty >= STAR_MIN_BUY or qty == sellable:
            return None
        return f"科创板卖出不少于 {STAR_MIN_BUY} 股，或一次性卖出余额"
    # CN-03：卖出可有零股，但零股须一次性卖出（即卖出全部可卖数量）
    if qty % inst.lot_size == 0 or qty == sellable:
        return None
    return f"卖出数量须为 {inst.lot_size} 的整数倍，或一次性卖出全部可卖数量"


def price_limits(inst: Instrument, prev_close: Decimal | None) -> tuple[Decimal | None, Decimal | None]:
    """CN-05/06：涨跌停价 = 前收盘价 × (1 ± 比例)，四舍五入到最小价位。返回 (跌停价, 涨停价)。"""
    if prev_close is None or inst.price_limit_pct is None:
        return None, None
    up = round_to_tick(prev_close * (1 + inst.price_limit_pct), inst.tick, "nearest")
    down = round_to_tick(prev_close * (1 - inst.price_limit_pct), inst.tick, "nearest")
    return down, up
