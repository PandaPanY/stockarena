"""订单在数据库与交易引擎之间的转换，以及条件单（止损 / 止盈）的维护。日频与分钟级共用。"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OrderRow
from app.trading.engine import Settlement
from app.trading.types import SIDE_LABELS, OrderIntent, PendingOrder

GTC_DAYS = 45          # GTC 订单最长有效期（自然日，约 30 个交易日）


def to_pending(row: OrderRow) -> PendingOrder:
    return PendingOrder(
        id=row.id, symbol=row.symbol, side=row.side, qty=row.qty, filled_qty=row.filled_qty,
        order_type=row.order_type, limit_price=row.limit_price, stop_price=row.stop_price, tif=row.tif,
        oco_group=row.oco_group, forced=row.forced,
    )


def due_orders(s: Session, account_id: int, d: date, now: datetime | None = None) -> dict[int, OrderRow]:
    """在交易日 d（分钟级再加上时刻 now）生效的待成交订单；过期订单就地标记失效。"""
    due: dict[int, OrderRow] = {}
    for o in s.scalars(select(OrderRow).where(OrderRow.account_id == account_id, OrderRow.status == "pending")):
        if o.tif == "gtc" and o.expires_on is not None and o.expires_on < d:
            o.status, o.note = "expired", "超过有效期"
        elif o.tif == "day" and o.target_date is not None and o.target_date < d:
            o.status, o.note = "expired", "计划撮合日已过（系统停机或数据缺失），订单失效"
        elif _not_yet(o, d, now):
            continue
        else:
            due[o.id] = o
    return due


def _not_yet(o: OrderRow, d: date, now: datetime | None) -> bool:
    """尚未到达最早撮合日期 / 时刻。"""
    if o.target_date is not None and o.target_date > d:
        return True
    return now is not None and o.active_from is not None and o.active_from > now


def apply_settlement(s: Session, due: dict[int, OrderRow], st: Settlement, d: date) -> list[OrderRow]:
    """把引擎结果写回订单；开仓单（买入 / 卖空）成交后自动挂出止损 / 止盈子订单。返回新建的子订单。"""
    for o in st.orders:
        row = due[o.id]
        row.status, row.filled_qty, row.note = o.status, o.filled_qty, o.note
    children: list[OrderRow] = []
    for f in st.fills:
        parent = due[f.order_id]
        if parent.side in ("buy", "short") and (parent.attach_stop or parent.attach_take_profit):
            children += protect(s, parent.account_id, parent.symbol, f.qty, parent.attach_stop,
                                parent.attach_take_profit, d, parent.decision_id,
                                f"自动挂出（来自{SIDE_LABELS[parent.side]}单 #{parent.id}）", active_from=None,
                                side="sell" if parent.side == "buy" else "cover")
    return children


def protect(s: Session, account_id: int, symbol: str, qty: int, stop, take_profit, d: date,
            decision_id: int | None, reason: str, active_from: datetime | None, side: str = "sell") -> list[OrderRow]:
    """为持仓挂出止损 / 止盈（同一 OCO 组，任一成交撤销另一个）。side：sell 保护多头，cover 保护空头。"""
    rows: list[OrderRow] = []
    for kind, price in (("stop", stop), ("take_profit", take_profit)):
        if price is None:
            continue
        rows.append(OrderRow(
            account_id=account_id, decision_id=decision_id, symbol=symbol, side=side, order_type=kind, qty=qty,
            status="pending", reason=reason, placed_date=d, target_date=d, stop_price=price, tif="gtc",
            expires_on=d + timedelta(days=GTC_DAYS), active_from=active_from,
        ))
    if not rows:
        return rows
    s.add_all(rows)
    s.flush()
    group = rows[0].id
    for r in rows:
        r.oco_group = group
    return rows


def cancel(s: Session, account_id: int, ids: list[int], note: str) -> int:
    n = 0
    for oid in ids:
        row = s.get(OrderRow, oid)
        if row is not None and row.account_id == account_id and row.status == "pending" and not row.forced:
            row.status, row.note = "cancelled", note
            n += 1
    return n


def cancel_unfilled(s: Session, account_id: int, note: str) -> int:
    """撤销未成交的市价单和限价单（不含止损止盈与强制平仓单）。部分成交的保留已成交部分。"""
    n = 0
    for row in s.scalars(select(OrderRow).where(OrderRow.account_id == account_id, OrderRow.status == "pending",
                                                OrderRow.order_type.in_(["market", "limit"]),
                                                OrderRow.forced.is_(False))):
        row.status = "partially_filled" if row.filled_qty else "cancelled"
        row.note = note
        n += 1
    return n


def cancel_conditionals(s: Session, account_id: int, symbol: str, note: str) -> None:
    for row in s.scalars(select(OrderRow).where(OrderRow.account_id == account_id, OrderRow.symbol == symbol,
                                                OrderRow.status == "pending",
                                                OrderRow.order_type.in_(["stop", "take_profit"]))):
        row.status, row.note = "cancelled", note


def new_order(account_id: int, decision_id: int | None, it: OrderIntent, placed: date, target: date | None,
              status: str = "pending", note: str = "", active_from: datetime | None = None) -> OrderRow:
    return OrderRow(
        account_id=account_id, decision_id=decision_id, symbol=it.symbol, side=it.side, order_type=it.order_type,
        qty=it.qty, status=status, reason=it.reason, note=note, placed_date=placed, target_date=target,
        limit_price=it.limit_price, stop_price=it.stop_price, tif=it.tif,
        expires_on=placed + timedelta(days=GTC_DAYS) if it.tif == "gtc" else None,
        attach_stop=it.attach_stop, attach_take_profit=it.attach_take_profit, active_from=active_from,
        forced=it.forced,
    )


def open_orders(s: Session, account_id: int) -> list[OrderRow]:
    return list(s.scalars(select(OrderRow).where(OrderRow.account_id == account_id, OrderRow.status == "pending")
                          .order_by(OrderRow.id)))


_TYPE_TEXT = {"market": "市价", "limit": "限价", "stop": "止损", "take_profit": "止盈"}


def describe(row: OrderRow) -> str:
    """给 AI 看的订单描述。"""
    side = SIDE_LABELS.get(row.side, row.side)
    kind = _TYPE_TEXT.get(row.order_type, row.order_type)
    price = ""
    if row.order_type == "limit":
        price = f" 限价 {row.limit_price}"
    elif row.order_type in ("stop", "take_profit"):
        price = f" 触发价 {row.stop_price}"
    tif = f"长期有效（至 {row.expires_on}）" if row.tif == "gtc" else "当日有效"
    oco = f"，与 #{row.oco_group} 同组" if row.oco_group and row.oco_group != row.id else ""
    forced = "【强制平仓，不可撤销】" if row.forced else ""
    return f"#{row.id} {forced}{side} {row.symbol} {row.qty - row.filled_qty} 股 {kind}{price}，{tif}{oco}"
