"""行情领域的数据结构（与数据库、数据源无关）。所有价格使用 Decimal。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Literal

Market = Literal["CN", "HK", "US"]
InstrumentType = Literal["stock", "etf", "lof", "index"]
# main=主板 star=科创板 gem=创业板 bse=北交所 fund=场内基金
Board = Literal["main", "star", "gem", "bse", "fund", "index"]


@dataclass(frozen=True)
class Instrument:
    symbol: str                 # 统一代码，如 600519.SH
    market: Market
    name: str
    type: InstrumentType
    board: Board
    lot_size: int = 100
    tick: Decimal = Decimal("0.01")
    is_st: bool = False
    t0: bool = False            # 是否 T+0（A股跨境/债券/黄金/货币 ETF）
    price_limit_pct: Decimal | None = None   # 涨跌幅限制，如 0.10；None 表示无限制
    list_date: date | None = None
    stamp_duty: bool = True     # 港股是否征收印花税（ETF 等豁免）
    delisted: date | None = None    # 退市日期（全时点股票池里已退市的股票；之后没有行情）

    @property
    def code(self) -> str:
        return self.symbol.split(".")[0]

    @property
    def exchange(self) -> str:
        return self.symbol.split(".")[1]


@dataclass(frozen=True)
class DailyBar:
    symbol: str
    date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int                  # 成交量（股 / 份）
    amount: Decimal              # 成交额
    prev_close: Decimal | None   # 交易所前收盘价（除权除息日为除权参考价），用于计算涨跌停
    adj_factor: Decimal = Decimal("1")   # 后复权因子：后复权价 = 不复权价 × adj_factor

    @property
    def suspended(self) -> bool:
        return self.volume <= 0


@dataclass(frozen=True)
class CorporateAction:
    """除权除息事件。每股口径。"""

    symbol: str
    ex_date: date
    cash_per_share: Decimal = Decimal("0")     # 每股派现（税前）
    shares_per_share: Decimal = Decimal("0")   # 每股送转股数（送股 + 转增）
    source: str = ""


@dataclass
class UniverseSpec:
    """股票池定义（来自 config/universes/*.yaml）。"""

    name: str
    title: str
    market: Market
    members: list[dict] = field(default_factory=list)   # 显式成员：{symbol, name?, type?, t0?, price_limit_pct?}
    index: str | None = None                             # 指数代码：成分股动态获取
    include: list[str] = field(default_factory=list)     # 合并其他股票池
    benchmark: str | None = None                         # 默认基准指数代码
    description: str = ""
    # 时点股票池：按该指数的历史成分（纳入 / 剔除日期）限定每天的可选股票，减少幸存者偏差（策略工坊回测用）
    point_in_time: str | None = None
    live_universe: str | None = None                     # 实时跟踪使用的股票池（时点股票池含历史成分时）
    # 全时点股票池：成员包含该指数历史上的全部成分（含已剔除、已退市的股票），成分记录来自 Baostock
    history: bool = False
