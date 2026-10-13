"""决策上下文：同一轮所有选手共享的行情快照 + 每个账户各自的账户信息。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

import pandas as pd

from app.market.types import DailyBar, Instrument
from app.trading.market_rules import closable_qty, get_rules
from app.trading.params import RuleParams
from app.trading.portfolio import AccountState

TABLE_HEADER = "代码,名称,类型,收盘,涨跌%,5日%,20日%,成交额(亿),MA5,MA20,RSI14,状态"
TABLE_HEADER_LOT = "代码,名称,类型,每手,收盘,涨跌%,5日%,20日%,成交额(亿),MA5,MA20,RSI14,状态"
TYPE_LABELS = {"stock": "股票", "etf": "ETF", "lof": "LOF"}


@dataclass
class MarketSnapshot:
    data_date: date
    fill_date: date
    table: str
    hash: str
    benchmark: dict
    ref_bars: dict[str, DailyBar]                  # 每个标的截至 data_date 的最后一根日线
    instruments: dict[str, Instrument]
    names: dict[str, str] = field(default_factory=dict)


def _pct(x: float | None) -> str:
    return "-" if x is None or pd.isna(x) else f"{x * 100:+.2f}%"


def rsi(closes: pd.Series, period: int = 14) -> float | None:
    if len(closes) <= period:
        return None
    delta = closes.diff().dropna().tail(period)
    gain = delta.clip(lower=0).mean()
    loss = (-delta.clip(upper=0)).mean()
    if loss == 0:
        return 100.0
    return float(100 - 100 / (1 + gain / loss))


def indicators(history: pd.DataFrame) -> dict:
    """history 为前复权日线（按日期升序）。"""
    c = history["close"] if not history.empty else pd.Series(dtype=float)
    last = c.iloc[-1] if len(c) else None

    def chg(n: int) -> float | None:
        return float(last / c.iloc[-1 - n] - 1) if len(c) > n and c.iloc[-1 - n] else None

    return {
        "chg5": chg(5),
        "chg20": chg(20),
        "ma5": float(c.tail(5).mean()) if len(c) >= 5 else None,
        "ma20": float(c.tail(20).mean()) if len(c) >= 20 else None,
        "rsi14": rsi(c),
    }


def _fmt_price(x: float | Decimal | None, inst: Instrument) -> str:
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return "-"
    # 港股 10 港元以下的股票价位更细，显示三位小数
    hk_fine = inst.market == "HK" and float(x) < 10
    places = 3 if hk_fine or (inst.market != "HK" and inst.tick < Decimal("0.01")) else 2
    return f"{float(x):.{places}f}"


def build_market_table(
    instruments: dict[str, Instrument], ref_bars: dict[str, DailyBar], histories: dict[str, pd.DataFrame],
    data_date: date,
) -> str:
    # 港股每只股票的每手股数不同，需要告诉 AI
    with_lot = any(i.market == "HK" for i in instruments.values())
    rows = [TABLE_HEADER_LOT if with_lot else TABLE_HEADER]
    for symbol, inst in instruments.items():
        lot = [str(inst.lot_size)] if with_lot else []
        bar = ref_bars.get(symbol)
        ind = indicators(histories.get(symbol, pd.DataFrame(columns=["close"])))
        if bar is None:
            rows.append(",".join([symbol, inst.name, TYPE_LABELS.get(inst.type, inst.type), *lot,
                                  "-", "-", "-", "-", "-", "-", "-", "-", "无行情"]))
            continue
        status = ""
        chg1: float | None = None
        if bar.date != data_date or bar.suspended:
            status = "停牌"
        else:
            if bar.prev_close:
                chg1 = float(bar.close / bar.prev_close - 1)
            down, up = get_rules(inst.market).price_limits(inst, bar.prev_close)
            if up is not None and bar.close >= up:
                status = "涨停"
            elif down is not None and bar.close <= down:
                status = "跌停"
        rows.append(",".join([
            symbol, inst.name, TYPE_LABELS.get(inst.type, inst.type), *lot, _fmt_price(bar.close, inst),
            _pct(chg1), _pct(ind["chg5"]), _pct(ind["chg20"]), f"{float(bar.amount) / 1e8:.2f}",
            _fmt_price(ind["ma5"], inst), _fmt_price(ind["ma20"], inst),
            "-" if ind["rsi14"] is None else f"{ind['rsi14']:.0f}", status,
        ]))
    return "\n".join(rows)


def snapshot_hash(table: str, benchmark: dict, data_date: date) -> str:
    payload = f"{data_date}\n{sorted(benchmark.items())}\n{table}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def benchmark_info(name: str, history: pd.DataFrame) -> dict:
    ind = indicators(history)
    closes = history["close"] if not history.empty else pd.Series(dtype=float)
    chg1 = float(closes.iloc[-1] / closes.iloc[-2] - 1) if len(closes) >= 2 else None
    return {
        "name": name,
        "close": f"{closes.iloc[-1]:.2f}" if len(closes) else "-",
        "chg_1d": _pct(chg1), "chg_5d": _pct(ind["chg5"]), "chg_20d": _pct(ind["chg20"]),
    }


def rules_summary(p: RuleParams) -> dict:
    commission = f"{float(p.commission_rate * 10000):g}‱（单笔最低 {float(p.commission_min):g} 元）"
    return {
        "slippage_bps": f"{float(p.slippage_bps):g}",
        "max_position_pct": f"{float(p.max_position_pct * 100):g}%",
        "max_orders_per_round": p.max_orders_per_round,
        "commission": commission,
        "stamp_duty": f"{float(p.stamp_duty_rate * 100):g}%",
        "leveraged": p.leveraged,
        "allow_short": p.allow_short,
        "allow_margin": p.allow_margin,
        "leverage_cap": f"{float(p.leverage_cap):g}",
    }


def account_context(state: AccountState, initial_cash: Decimal, snap: MarketSnapshot,
                    params: RuleParams | None = None) -> dict:
    params = params or RuleParams()
    prices = {s: b.close for s, b in snap.ref_bars.items()}
    for sym, pos in state.positions.items():
        prices.setdefault(sym, pos.avg_cost)
    nav = state.nav(prices)
    positions = []
    for symbol, pos in sorted(state.positions.items()):
        inst = snap.instruments.get(symbol)
        close = prices[symbol]
        short = pos.qty < 0
        if inst is None:
            closable = abs(pos.qty)
        elif short:
            closable = closable_qty(inst, pos, snap.fill_date, "cover", params)
        else:
            closable = get_rules(inst.market).sellable_qty(inst, pos, snap.fill_date)
        # 空头盈亏 = 卖空均价 / 现价 − 1
        pnl = (pos.avg_cost / close - 1) if short else (close / pos.avg_cost - 1)
        positions.append({
            "symbol": symbol,
            "name": snap.names.get(symbol, inst.name if inst else symbol),
            "qty": pos.qty,
            "sellable": closable,
            "avg_cost": f"{pos.avg_cost:.3f}",
            "close": _fmt_price(close, inst) if inst else str(close),
            "pnl_pct": _pct(float(pnl)) if pos.avg_cost and close else "-",
            "weight": f"{float(close * pos.qty / nav) * 100:.1f}%" if nav else "-",
        })
    leverage_text = ""
    if params.leveraged:
        gross = state.gross_exposure(prices)
        debt = -state.cash if state.cash < 0 else Decimal("0")
        ratio = state.margin_ratio(prices, params)
        name = "维持担保比例" if params.margin_check_method == "collateral_ratio" else "保证金比率"
        leverage_text = (
            f"多头市值 {state.long_value(prices):,.2f}，空头市值 {state.short_value(prices):,.2f}，"
            f"总敞口 {gross:,.2f}（杠杆 {float(gross / nav) if nav > 0 else 0:.2f} 倍，"
            f"上限 {float(params.leverage_cap):g} 倍）；"
            f"融资负债 {debt:,.2f}；{name} {'-' if ratio is None else f'{float(ratio) * 100:.1f}%'}"
            f"（维持线 {float(params.maintenance_margin * 100):g}%，"
            f"强平线 {float(params.liquidation_margin * 100):g}%）")
    return {
        "nav": f"{nav:,.2f}",
        "initial": f"{initial_cash:,.2f}",
        "total_return": _pct(float(nav / initial_cash - 1)) if initial_cash else "-",
        "cash": f"{state.cash:,.2f}",
        "cash_pct": f"{float(state.cash / nav) * 100:.1f}%" if nav else "-",
        "positions": positions,
        "leverage_text": leverage_text,
    }
