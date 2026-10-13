from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.timeutils import ensure_utc, to_market_time


def test_ensure_utc_treats_naive_as_utc():
    assert ensure_utc(datetime(2026, 1, 1, 8, 0)) == datetime(2026, 1, 1, 8, 0, tzinfo=UTC)


def test_cn_and_hk_are_utc_plus_8():
    dt = datetime(2026, 7, 1, 1, 30, tzinfo=UTC)
    assert to_market_time(dt, "CN").hour == 9
    assert to_market_time(dt, "HK").utcoffset() == timedelta(hours=8)


def test_us_daylight_saving_time():
    # 美股 09:30 开盘：夏令时对应 13:30 UTC，冬令时对应 14:30 UTC
    summer = to_market_time(datetime(2026, 7, 1, 13, 30, tzinfo=UTC), "US")
    winter = to_market_time(datetime(2026, 12, 1, 14, 30, tzinfo=UTC), "US")
    assert (summer.hour, summer.minute) == (9, 30)
    assert (winter.hour, winter.minute) == (9, 30)


def test_unknown_market():
    with pytest.raises(ValueError):
        to_market_time(datetime.now(UTC), "XX")
