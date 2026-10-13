"""交易日历。"""

from __future__ import annotations

import bisect
from datetime import date


class TradingCalendar:
    def __init__(self, market: str, days: list[date]):
        if not days:
            raise ValueError(f"{market} 交易日历为空")
        self.market = market
        self._days = sorted(set(days))
        self._set = set(self._days)

    @property
    def first(self) -> date:
        return self._days[0]

    @property
    def last(self) -> date:
        return self._days[-1]

    def covers(self, d: date) -> bool:
        return self.first <= d <= self.last

    def is_trading_day(self, d: date) -> bool:
        if not self.covers(d):
            raise ValueError(f"{self.market} 交易日历未覆盖 {d}（范围 {self.first}～{self.last}）")
        return d in self._set

    def previous(self, d: date) -> date:
        """严格早于 d 的最近一个交易日。"""
        i = bisect.bisect_left(self._days, d)
        if i == 0:
            raise ValueError(f"{d} 之前没有交易日数据")
        return self._days[i - 1]

    def next(self, d: date) -> date:
        """严格晚于 d 的最近一个交易日。"""
        i = bisect.bisect_right(self._days, d)
        if i >= len(self._days):
            raise ValueError(f"{d} 之后没有交易日数据")
        return self._days[i]

    def latest_on_or_before(self, d: date) -> date:
        return d if d in self._set else self.previous(d)

    def between(self, start: date, end: date) -> list[date]:
        """闭区间 [start, end] 内的交易日。"""
        lo = bisect.bisect_left(self._days, start)
        hi = bisect.bisect_right(self._days, end)
        return self._days[lo:hi]
