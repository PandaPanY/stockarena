"""目标仓位模式：把 AI 给出的目标权重转换为调仓订单（PRD 3.2、附录 6.2）。

约定：目标中未列出的现有持仓视为目标权重 0（清仓）。权重为负表示空头（需开启做空）。
- 不允许融资：多头权重合计不超过 1；总敞口（|权重| 之和）不超过杠杆上限，超出时按比例缩放
- 由多翻空（或由空翻多）拆成两笔：先平仓，再反向开仓
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, Decimal

from app.market.types import DailyBar, Instrument
from app.trading.market_rules import get_rules
from app.trading.matching import BPS
from app.trading.params import RuleParams
from app.trading.portfolio import AccountState
from app.trading.types import OrderIntent

WEIGHT_TOLERANCE = Decimal("0.01")


@dataclass
class TargetConversion:
    intents: list[OrderIntent] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _floor_lots(qty: Decimal, lot: int) -> int:
    n = int(qty.to_integral_value(rounding=ROUND_FLOOR))
    return n - n % lot


def targets_to_intents(
    targets: dict[str, Decimal],
    account: AccountState,
    instruments: dict[str, Instrument],
    ref_bars: dict[str, DailyBar],
    params: RuleParams,
    reasons: dict[str, str] | None = None,
) -> TargetConversion:
    reasons = reasons or {}
    out = TargetConversion()
    weights: dict[str, Decimal] = {}
    for symbol, w in targets.items():
        if symbol not in instruments:
            out.notes.append(f"{symbol} 不在股票池内，已忽略")
            continue
        if w < 0 and not params.allow_short:
            out.notes.append(f"{symbol} 权重为负（未开启做空），按 0 处理")
            w = Decimal("0")
        if abs(w) > params.max_position_pct:
            out.notes.append(f"{symbol} 权重 {w} 超过单票上限 {params.max_position_pct}，已截断")
            w = params.max_position_pct if w > 0 else -params.max_position_pct
        weights[symbol] = w
    longs = sum((w for w in weights.values() if w > 0), Decimal("0"))
    if not params.allow_margin and longs > 1 + WEIGHT_TOLERANCE:
        out.notes.append(f"多头权重合计 {longs} 超过 1（未开启融资），已按比例缩放")
        weights = {s: (w / longs if w > 0 else w) for s, w in weights.items()}
    gross = sum((abs(w) for w in weights.values()), Decimal("0"))
    cap = params.leverage_cap
    if gross > cap + WEIGHT_TOLERANCE:
        out.notes.append(f"总敞口（|权重| 之和）{gross} 超过杠杆上限 {cap:g}，已按比例缩放")
        weights = {s: w * cap / gross for s, w in weights.items()}

    prices = {s: b.close for s, b in ref_bars.items()}
    nav = account.nav(prices)
    investable = nav * (1 - params.cash_buffer_pct)
    threshold = nav * params.rebalance_threshold_pct
    slip = params.slippage_bps / BPS

    closes: list[OrderIntent] = []
    opens: list[OrderIntent] = []
    for symbol in list(dict.fromkeys([*account.positions, *weights])):
        inst = instruments.get(symbol)
        bar = ref_bars.get(symbol)
        if inst is None or bar is None or bar.close <= 0:
            continue
        pos = account.position(symbol)
        held = pos.qty if pos else 0
        w = weights.get(symbol, Decimal("0"))
        reason = reasons.get(symbol, "目标仓位调整" if w != 0 else "目标仓位为 0，清仓")
        rules = get_rules(inst.market)
        step = rules.buy_step(inst)
        size = _floor_lots(investable * abs(w) / (bar.close * (1 + slip)), step)
        target = size if w > 0 else -size
        if target == held:
            continue
        # 方向改变（或目标权重为 0）：先把现有仓位全部平掉。
        # 目标权重大于 0、只是不足一手（取整后为 0）时不算清仓，按普通减仓处理、受门槛限制（与快速回测一致）
        if held != 0 and (w == 0 or (w > 0) != (held > 0)):
            closes.append(OrderIntent(symbol, "sell" if held > 0 else "cover", abs(held), reason=reason))
            held = 0
            if target == 0:
                continue
        diff = target - held
        # 门槛按「目标金额 − 当前市值」判断（不用取整后的股数差：高价股一手的金额可能就超过门槛，
        # 会因整手取整反复微调），与快速回测一致；开启 rebalance_new_free 时新建仓不受门槛限制
        gap_value = abs(investable * w - held * bar.close)
        if diff == 0 or (gap_value < threshold and not (params.rebalance_new_free and held == 0)):
            continue
        opening = abs(target) > abs(held)
        long_side = w > 0 if w != 0 else held > 0
        side = {(True, True): "buy", (True, False): "sell", (False, True): "short", (False, False): "cover"}[
            (long_side, opening)]
        qty = abs(diff)
        if opening:
            if qty < rules.min_buy(inst):
                continue
            opens.append(OrderIntent(symbol, side, qty, reason=reason))
        else:
            qty -= qty % step
            if qty > 0:
                closes.append(OrderIntent(symbol, side, qty, reason=reason))
    out.intents = closes + opens
    return out
