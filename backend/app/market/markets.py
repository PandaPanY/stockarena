"""市场定义：时区、交易日历、收盘后数据就绪时间、自动执行时间、货币、默认本金。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class MarketSpec:
    code: str
    name: str
    currency: str
    tz: str
    calendar: str                       # exchange_calendars 代码
    data_ready: time                    # 收盘后日线数据稳定可取的时间（本地时间）
    pre_open: time                      # 开盘前截止：此前启动的赛道用上一交易日数据决策、当天开盘成交
    run_times: tuple[tuple[int, int], ...]   # 自动执行日终流程的时间（本地时间，流程幂等，多次尝试应对数据延迟）
    default_capital: int

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)


MARKETS: dict[str, MarketSpec] = {
    "CN": MarketSpec("CN", "A股", "CNY", "Asia/Shanghai", "XSHG", time(15, 40), time(9, 15),
                     ((15, 45), (16, 30), (18, 0), (20, 30)), 1_000_000),
    "HK": MarketSpec("HK", "港股", "HKD", "Asia/Hong_Kong", "XHKG", time(16, 30), time(9, 0),
                     ((16, 40), (17, 30), (19, 0), (21, 0)), 1_000_000),
    "US": MarketSpec("US", "美股", "USD", "America/New_York", "XNYS", time(16, 40), time(9, 0),
                     ((16, 50), (18, 0), (20, 0), (22, 0)), 100_000),
}


def get_market(code: str) -> MarketSpec:
    try:
        return MARKETS[code]
    except KeyError as e:
        raise ValueError(f"未知市场：{code}") from e
