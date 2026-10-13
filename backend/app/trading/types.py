"""交易引擎的数据结构（纯逻辑，不依赖数据库）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Literal

# buy 买入开多 / sell 卖出平多 / short 融券卖出开空 / cover 买券还券平空
Side = Literal["buy", "sell", "short", "cover"]
TradeSide = Literal["buy", "sell"]          # 资金方向：buy / cover 付出现金，sell / short 收入现金
OPENING_SIDES = ("buy", "short")
CLOSING_SIDES = ("sell", "cover")
SIDE_LABELS = {"buy": "买入", "sell": "卖出", "short": "卖空", "cover": "平空"}


def trade_side(side: str) -> TradeSide:
    """撮合价格、滑点、费用按资金方向计算：平空等同买入，卖空等同卖出。"""
    return "buy" if side in ("buy", "cover") else "sell"

# market 市价；limit 限价；stop 止损（触发后按市价卖出）；take_profit 止盈（触及后按不低于止盈价卖出）
OrderType = Literal["market", "limit", "stop", "take_profit"]
TimeInForce = Literal["day", "gtc"]
OrderStatus = Literal["pending", "filled", "partially_filled", "rejected", "expired", "cancelled"]
LedgerType = Literal["trade", "fee", "dividend", "deposit", "adjust", "interest", "borrow_fee"]


@dataclass
class OrderIntent:
    """AI 或基线策略提出的下单意图（尚未通过风控）。"""

    symbol: str
    side: Side
    qty: int
    order_type: OrderType = "market"
    reason: str = ""
    confidence: float | None = None
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None          # stop / take_profit 的触发价
    tif: TimeInForce = "day"
    attach_stop: Decimal | None = None         # 开仓单（买入 / 卖空）成交后自动挂出的止损价
    attach_take_profit: Decimal | None = None  # 开仓单成交后自动挂出的止盈价
    forced: bool = False                       # 系统强制平仓


@dataclass
class PendingOrder:
    """已通过风控、等待撮合的订单。"""

    id: int
    symbol: str
    side: Side
    qty: int
    filled_qty: int = 0
    status: OrderStatus = "pending"
    note: str = ""
    order_type: OrderType = "market"
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    tif: TimeInForce = "day"
    oco_group: int | None = None               # 同组条件单（止损 / 止盈）一个成交后撤销其余
    forced: bool = False

    @property
    def conditional(self) -> bool:
        return self.order_type in ("stop", "take_profit")


@dataclass
class Position:
    symbol: str
    qty: int                          # 正数为多头，负数为空头
    avg_cost: Decimal                 # 多头：含买入费用的成本价；空头：扣除费用后的平均卖空价
    last_buy_date: date | None = None
    last_buy_qty: int = 0             # last_buy_date 当天开仓的数量（A股多头 T+1 不可卖、融券当日不可还）
    opened_date: date | None = None   # 本轮持有的建仓日（从空仓开始买入的日期），A股红利税按持有期计算

    @property
    def is_short(self) -> bool:
        return self.qty < 0


@dataclass
class Fill:
    order_id: int
    symbol: str
    side: Side
    qty: int
    price: Decimal
    fees: dict[str, Decimal]
    trade_date: date
    realized_pnl: Decimal | None = None   # 平仓（卖出 / 平空）时的已实现盈亏（扣除开平仓费用）

    @property
    def amount(self) -> Decimal:
        return self.price * self.qty

    @property
    def total_fee(self) -> Decimal:
        return sum(self.fees.values(), Decimal("0"))

    @property
    def cash_delta(self) -> Decimal:
        return -(self.amount + self.total_fee) if trade_side(self.side) == "buy" else self.amount - self.total_fee


@dataclass
class LedgerEntry:
    type: LedgerType
    amount: Decimal           # 正数为现金流入
    symbol: str | None = None
    note: str = ""
    ref_order_id: int | None = None


@dataclass
class EngineEvent:
    type: str                 # corporate_action / order_rejected / order_expired / partial_fill ...
    message: str
    symbol: str | None = None
    detail: dict = field(default_factory=dict)
