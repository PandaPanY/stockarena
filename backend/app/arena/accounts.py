"""账户状态在数据库与交易引擎之间的转换。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import AccountRow, CashLedgerRow, NavSnapshotRow, PositionRow
from app.trading.portfolio import AccountState
from app.trading.types import LedgerEntry, Position


def load_state(session: Session, account: AccountRow) -> AccountState:
    rows = session.scalars(select(PositionRow).where(PositionRow.account_id == account.id))
    return AccountState(
        cash=account.cash,
        positions={r.symbol: Position(r.symbol, r.qty, r.avg_cost, r.last_buy_date, r.last_buy_qty, r.opened_date)
                   for r in rows},
    )


def save_state(session: Session, account: AccountRow, state: AccountState) -> None:
    account.cash = state.cash
    session.execute(delete(PositionRow).where(PositionRow.account_id == account.id))
    session.flush()
    for p in state.positions.values():
        session.add(PositionRow(account_id=account.id, symbol=p.symbol, qty=p.qty, avg_cost=p.avg_cost,
                                last_buy_date=p.last_buy_date, last_buy_qty=p.last_buy_qty,
                                opened_date=p.opened_date))


def append_ledger(session: Session, account_id: int, trade_date: date, entries: list[LedgerEntry],
                  start_balance: Decimal) -> Decimal:
    """写入资金流水，balance_after 逐笔累加。返回最终余额（应与账户现金一致）。"""
    balance = start_balance
    for e in entries:
        balance += e.amount
        session.add(CashLedgerRow(account_id=account_id, trade_date=trade_date, type=e.type, amount=e.amount,
                                  balance_after=balance, symbol=e.symbol, order_id=e.ref_order_id, note=e.note))
    return balance


def write_nav(session: Session, account_id: int, d: date, state: AccountState, prices: dict[str, Decimal],
              benchmark_close: Decimal | None) -> Decimal:
    nav = state.nav(prices)
    row = session.scalars(select(NavSnapshotRow).where(
        NavSnapshotRow.account_id == account_id, NavSnapshotRow.date == d)).first() or NavSnapshotRow(
        account_id=account_id, date=d)
    row.nav, row.cash, row.market_value = nav, state.cash, state.market_value(prices)
    row.benchmark_close, row.positions_count = benchmark_close, len(state.positions)
    row.long_value, row.short_value = state.long_value(prices), state.short_value(prices)
    session.add(row)
    return nav


def reconcile(session: Session, account: AccountRow) -> Decimal:
    """K2 对账：返回 流水合计 - 账户现金 的差额（应为 0）。"""
    total = sum(session.scalars(select(CashLedgerRow.amount).where(CashLedgerRow.account_id == account.id)),
                Decimal("0"))
    return total - account.cash
