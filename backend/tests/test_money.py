from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.money import ceil_money, round_money, round_to_tick, to_decimal


def test_to_decimal_float_has_no_binary_noise():
    assert to_decimal(0.1) == Decimal("0.1")
    assert to_decimal(0.1) + to_decimal(0.2) == Decimal("0.3")


def test_to_decimal_rejects_bool():
    with pytest.raises(TypeError):
        to_decimal(True)


@pytest.mark.parametrize(
    ("amount", "expected"),
    [("1.005", "1.01"), ("1.004", "1.00"), ("-1.005", "-1.01"), (5, "5.00")],
)
def test_round_money_half_up(amount, expected):
    assert round_money(amount) == Decimal(expected)


def test_ceil_money_to_whole_unit():
    # 港股印花税向上取整到整数港元
    assert ceil_money("12.01", Decimal("1")) == Decimal("13")
    assert ceil_money("12.00", Decimal("1")) == Decimal("12")


@pytest.mark.parametrize(
    ("price", "tick", "direction", "expected"),
    [
        ("10.123", "0.01", "up", "10.13"),
        ("10.123", "0.01", "down", "10.12"),
        ("10.125", "0.01", "nearest", "10.13"),
        ("1.2345", "0.001", "up", "1.235"),       # A股 ETF 最小价位 0.001
        ("3.07", "0.05", "down", "3.05"),          # 非 10 的幂次价位
        ("3.07", "0.05", "up", "3.10"),
        ("10.00", "0.01", "up", "10.00"),          # 已在价位上不变
    ],
)
def test_round_to_tick(price, tick, direction, expected):
    assert round_to_tick(price, tick, direction) == Decimal(expected)


def test_round_to_tick_rejects_non_positive_tick():
    with pytest.raises(ValueError):
        round_to_tick("1", "0")
