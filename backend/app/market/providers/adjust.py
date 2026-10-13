"""由除权除息事件推算复权因子与交易所前收盘价（港股、美股、A股 ETF 共用）。

新浪港股 / 美股的复权表给出「拆合股因子」和「累计派息」两列：
- 拆合股：相邻两行因子之比即为送转（拆股）比例
- 派息：相邻两行累计值之差除以当行因子，即为当时每股派息
本模块把它们转换成 CorporateAction，再在不复权日线上逐日计算后复权因子。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pandas as pd

from app.core.money import round_to_tick, to_decimal
from app.market.types import CorporateAction, DailyBar, Instrument


def _d(value) -> Decimal:
    return to_decimal(float(value)) if not isinstance(value, str) else Decimal(value)


def actions_from_factor_table(symbol: str, table: pd.DataFrame, factor_col: str, cum_cash_col: str,
                              source: str) -> list[CorporateAction]:
    """table：每行一个变动日（任意顺序），factor_col 为拆合股因子（越近越大或相等），cum_cash_col 为累计派息。"""
    if table.empty:
        return []
    rows = sorted(
        ((pd.Timestamp(r[0]).date(), _d(r[1]), _d(r[2] or 0))
         for r in table[["date", factor_col, cum_cash_col]].itertuples(index=False)),
        key=lambda x: x[0],
    )
    actions = []
    for prev, cur in zip(rows, rows[1:], strict=False):
        d, f, cum = cur
        _, f_prev, cum_prev = prev
        if f <= 0 or f_prev <= 0:
            continue
        cash = (cum - cum_prev) / f
        shares = f / f_prev - 1
        if cash > 0 or shares != 0:
            actions.append(CorporateAction(symbol=symbol, ex_date=d, cash_per_share=cash.quantize(Decimal("0.000001")),
                                           shares_per_share=shares.quantize(Decimal("0.000001")), source=source))
    return actions


def build_bars(inst: Instrument, raw: pd.DataFrame, actions: list[CorporateAction], start: date, end: date,
               tick_for=None, places: Decimal | None = None) -> list[DailyBar]:
    """raw：完整（或足够早）的不复权日线，列 date/open/high/low/close/volume[/amount]。

    因子从 raw 的第一天以 1 起算，所以必须传入同样起点的历史，才能保证多次增量拉取的因子基准一致。
    """
    if raw.empty:
        return []
    df = raw.copy()
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df = df.sort_values("date").reset_index(drop=True)
    by_date = {a.ex_date: a for a in actions}
    tick_for = tick_for or (lambda _p: inst.tick)
    # 数据源的价格是浮点数，带有 462.60001 这类误差，按市场精度取整
    px = (lambda v: _d(v).quantize(places)) if places else _d

    bars: list[DailyBar] = []
    factor = Decimal("1")
    prev_close: Decimal | None = None
    for row in df.itertuples(index=False):
        d = row.date
        close = px(row.close)
        ref_prev = prev_close
        act = by_date.get(d)
        if act is not None and prev_close is not None:
            ref = (prev_close - act.cash_per_share) / (1 + act.shares_per_share)
            if ref > 0:
                factor = factor * prev_close / ref
                # 只有除权日的参考价是计算出来的，需要取整到价位；平常日子就是昨收
                ref_prev = round_to_tick(ref, tick_for(ref), "nearest")
        if start <= d <= end:
            amount = getattr(row, "amount", None)
            volume = int(float(row.volume or 0))
            bars.append(DailyBar(
                symbol=inst.symbol, date=d, open=px(row.open), high=px(row.high), low=px(row.low), close=close,
                volume=volume,
                amount=_d(amount) if amount is not None and not pd.isna(amount) else close * volume,
                prev_close=ref_prev,
                adj_factor=factor,
            ))
        prev_close = close
    return bars
