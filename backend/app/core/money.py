"""金额与价格的 Decimal 工具。

约定：系统内所有金额、价格、费率一律使用 Decimal，禁止使用 float 参与账务计算。
"""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Literal

Number = Decimal | int | str | float
RoundDirection = Literal["up", "down", "nearest"]

ZERO = Decimal("0")
CENT = Decimal("0.01")


def to_decimal(value: Number) -> Decimal:
    """安全地转换为 Decimal。float 先转为字符串，避免二进制精度误差（0.1 → 0.1000000000000000055…）。"""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise TypeError("bool 不能转换为金额")
    if isinstance(value, float):
        return Decimal(repr(value))
    return Decimal(value)


def round_money(amount: Number, unit: Decimal = CENT) -> Decimal:
    """金额按最小货币单位四舍五入（默认到分）。"""
    return to_decimal(amount).quantize(unit, rounding=ROUND_HALF_UP)


def ceil_money(amount: Number, unit: Decimal = CENT) -> Decimal:
    """金额向上取整（如港股印花税向上取整到整数港元）。"""
    return to_decimal(amount).quantize(unit, rounding=ROUND_CEILING)


def round_to_tick(price: Number, tick: Number, direction: RoundDirection = "nearest") -> Decimal:
    """价格按最小价位取整。

    direction:
      - "up": 向上取整（如模拟买入成交价，取对模拟者不利方向）
      - "down": 向下取整（如模拟卖出成交价）
      - "nearest": 四舍五入（如涨跌停价计算）
    """
    p = to_decimal(price)
    t = to_decimal(tick)
    if t <= 0:
        raise ValueError("tick 必须为正数")
    rounding = {"up": ROUND_CEILING, "down": ROUND_FLOOR, "nearest": ROUND_HALF_UP}[direction]
    steps = (p / t).quantize(Decimal("1"), rounding=rounding)
    return (steps * t).quantize(t)
