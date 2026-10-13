from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.market.calendar import TradingCalendar
from app.market.symbols import (
    cn_board,
    cn_exchange,
    cn_fund_type,
    cn_stock_price_limit,
    is_st_name,
    normalize_cn,
    to_sina,
)

# 2026 国庆：9/30 之后到 10/8 才开市
DAYS = [date(2026, 9, 28), date(2026, 9, 29), date(2026, 9, 30), date(2026, 10, 8), date(2026, 10, 9)]


def test_calendar_navigation_over_holiday():
    cal = TradingCalendar("CN", DAYS)
    assert cal.is_trading_day(date(2026, 9, 30))
    assert not cal.is_trading_day(date(2026, 10, 1))
    assert cal.next(date(2026, 9, 30)) == date(2026, 10, 8)
    assert cal.previous(date(2026, 10, 8)) == date(2026, 9, 30)
    assert cal.latest_on_or_before(date(2026, 10, 5)) == date(2026, 9, 30)
    assert cal.between(date(2026, 9, 29), date(2026, 10, 8)) == DAYS[1:4]


def test_calendar_rejects_uncovered_dates():
    cal = TradingCalendar("CN", DAYS)
    with pytest.raises(ValueError):
        cal.is_trading_day(date(2027, 1, 4))
    with pytest.raises(ValueError):
        cal.next(date(2026, 10, 9))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("600519", "600519.SH"), ("sz000001", "000001.SZ"), ("510300.sh", "510300.SH"),
     ("159915", "159915.SZ"), ("688981", "688981.SH")],
)
def test_normalize_cn(raw, expected):
    assert normalize_cn(raw) == expected


def test_to_sina():
    assert to_sina("600519.SH") == "sh600519"
    assert to_sina("000001") == "sz000001"


def test_exchange_unknown():
    with pytest.raises(ValueError):
        cn_exchange("999999")


@pytest.mark.parametrize(
    ("code", "board"),
    [("600519", "main"), ("000001", "main"), ("688981", "star"), ("300750", "gem"), ("302132", "gem"),
     ("510300", "fund"), ("159915", "fund"), ("920001", "bse")],
)
def test_cn_board(code, board):
    assert cn_board(code) == board


def test_fund_type():
    assert cn_fund_type("510300") == "etf"
    assert cn_fund_type("159915") == "etf"
    assert cn_fund_type("600519") is None


def test_price_limits():
    assert cn_stock_price_limit("main", False) == Decimal("0.10")
    assert cn_stock_price_limit("main", True) == Decimal("0.05")
    assert cn_stock_price_limit("star", True) == Decimal("0.20")
    assert cn_stock_price_limit("gem", False) == Decimal("0.20")


def test_is_st_name():
    assert is_st_name("*ST 某某")
    assert is_st_name("ST某某")
    assert not is_st_name("贵州茅台")
