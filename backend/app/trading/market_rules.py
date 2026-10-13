"""按市场区分的交易规则（PRD 附录 6.3）。交易引擎、风控、撮合只通过这里访问市场差异。"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Protocol

from app.core.money import ceil_money, round_money, round_to_tick
from app.market.types import Instrument
from app.trading import rules_cn
from app.trading.fees import calc_fees_cn
from app.trading.params import RuleParams
from app.trading.types import Position, Side

ONE = Decimal("1")


class MarketRules(Protocol):
    market: str

    def tick(self, inst: Instrument, price: Decimal) -> Decimal: ...
    def price_limits(self, inst: Instrument, prev_close: Decimal | None) -> tuple[Decimal | None, Decimal | None]: ...
    def sellable_qty(self, inst: Instrument, pos: Position | None, on: date) -> int: ...
    def validate_qty(self, inst: Instrument, side: Side, qty: int, sellable: int) -> str | None: ...
    def buy_step(self, inst: Instrument) -> int: ...
    def min_buy(self, inst: Instrument) -> int: ...
    def calc_fees(
        self, inst: Instrument, side: Side, qty: int, price: Decimal, p: RuleParams
    ) -> dict[str, Decimal]: ...
    def describe(self, p: RuleParams) -> list[str]: ...


def _pct(x: Decimal) -> str:
    return f"{float(x * 100):g}%"


def _with_platform_fee(fees: dict[str, Decimal], p: RuleParams) -> dict[str, Decimal]:
    if p.platform_fee > 0:
        fees["platform_fee"] = p.platform_fee
    return fees


class CNRules:
    market = "CN"

    def tick(self, inst: Instrument, price: Decimal) -> Decimal:
        return inst.tick

    def price_limits(self, inst, prev_close):
        return rules_cn.price_limits(inst, prev_close)

    def sellable_qty(self, inst, pos, on):
        return rules_cn.sellable_qty(inst, pos, on)

    def validate_qty(self, inst, side, qty, sellable):
        return rules_cn.validate_qty(inst, side, qty, sellable)

    def buy_step(self, inst):
        return 1 if inst.board == "star" else inst.lot_size

    def min_buy(self, inst):
        return rules_cn.STAR_MIN_BUY if inst.board == "star" else inst.lot_size

    def calc_fees(self, inst, side, qty, price, p):
        return _with_platform_fee(calc_fees_cn(inst, side, qty, price, p), p)

    def describe(self, p: RuleParams) -> list[str]:
        return [
            _scope(p),
            "股票买入数量须为 100 股的整数倍；科创板（688 开头）买入不少于 200 股；卖出零股须一次性卖出。",
            "T+1：当天买入的股票下一交易日才能卖出（跨境、黄金、债券 ETF 为 T+0）。",
            "涨跌停限制：主板 ±10%，ST ±5%，创业板 / 科创板 ±20%；一字涨停无法买入，一字跌停无法卖出，停牌无法交易。",
            f"费用：佣金 {float(p.commission_rate * 10000):g}‱（单笔最低 {float(p.commission_min):g} 元）；"
            f"股票卖出印花税 {_pct(p.stamp_duty_rate)}；ETF 免印花税。",
            *leverage_lines(p, "CN"),
        ]


# 港交所价位表（第一部分：股票）⚠️ 港交所计划分阶段收窄部分价位，实现以最新价位表为准
_HK_SPREADS: list[tuple[Decimal, Decimal]] = [
    (Decimal("0.25"), Decimal("0.001")),
    (Decimal("0.50"), Decimal("0.005")),
    (Decimal("10"), Decimal("0.01")),
    (Decimal("20"), Decimal("0.02")),
    (Decimal("100"), Decimal("0.05")),
    (Decimal("200"), Decimal("0.1")),
    (Decimal("500"), Decimal("0.2")),
    (Decimal("1000"), Decimal("0.5")),
    (Decimal("2000"), Decimal("1")),
    (Decimal("5000"), Decimal("2")),
]


def hk_tick(price: Decimal) -> Decimal:
    for upper, tick in _HK_SPREADS:
        if price < upper:
            return tick
    return Decimal("5")


class HKRules:
    market = "HK"

    def tick(self, inst, price):
        return hk_tick(price)

    def price_limits(self, inst, prev_close):
        return None, None          # 港股没有涨跌停（不模拟市调机制）

    def sellable_qty(self, inst, pos, on):
        return pos.qty if pos else 0   # T+0

    def validate_qty(self, inst, side, qty, sellable):
        if qty <= 0:
            return "数量必须为正整数"
        if side == "sell" and qty > sellable:
            return f"可卖数量不足（可卖 {sellable}）"
        if qty % inst.lot_size == 0 or (side == "sell" and qty == sellable):
            return None
        return f"数量须为每手股数 {inst.lot_size} 的整数倍"

    def buy_step(self, inst):
        return inst.lot_size

    def min_buy(self, inst):
        return inst.lot_size

    def calc_fees(self, inst, side, qty, price, p):
        amount = price * qty
        settlement = min(max(round_money(amount * p.hk_settlement_fee_rate), p.hk_settlement_fee_min),
                         p.hk_settlement_fee_max)
        fees = {
            "commission": max(round_money(amount * p.commission_rate), p.commission_min),
            "trading_levy": round_money(amount * p.hk_trading_levy_rate),
            "afrc_levy": round_money(amount * p.hk_afrc_levy_rate),
            "trading_fee": round_money(amount * p.hk_trading_fee_rate),
            "settlement_fee": settlement,
        }
        if inst.stamp_duty and inst.type == "stock":
            fees["stamp_duty"] = ceil_money(amount * p.hk_stamp_duty_rate, ONE)   # 向上取整到整数港元
        return _with_platform_fee(fees, p)

    def describe(self, p: RuleParams) -> list[str]:
        return [
            _scope(p),
            "买卖数量须为该股票「每手股数」的整数倍（每只股票不同，见行情表的「每手」列），不支持碎股。",
            "T+0：当天买入当天即可卖出；港股没有涨跌停限制。停牌无法交易。",
            f"费用：佣金 {float(p.commission_rate * 10000):g}‱（单笔最低 {float(p.commission_min):g} 港元）"
            + (f"＋每笔平台费 {float(p.platform_fee):g} 港元" if p.platform_fee else "")
            + f"；股票买卖双方各付印花税 {_pct(p.hk_stamp_duty_rate)}（ETF 免）；另有交易征费、交易费、结算费。",
            *leverage_lines(p, "HK"),
        ]


class USRules:
    market = "US"

    def tick(self, inst, price):
        return Decimal("0.01") if price >= 1 else Decimal("0.0001")

    def price_limits(self, inst, prev_close):
        return None, None          # 不模拟 LULD 与熔断

    def sellable_qty(self, inst, pos, on):
        return pos.qty if pos else 0   # T+0

    def validate_qty(self, inst, side, qty, sellable):
        if qty <= 0:
            return "数量必须为正整数（不支持碎股）"
        if side == "sell" and qty > sellable:
            return f"可卖数量不足（可卖 {sellable}）"
        return None

    def buy_step(self, inst):
        return 1

    def min_buy(self, inst):
        return 1

    def calc_fees(self, inst, side, qty, price, p):
        amount = price * qty
        fees: dict[str, Decimal] = {}
        commission = max(round_money(amount * p.commission_rate), p.commission_min) if p.commission_rate else Decimal(
            "0")
        if commission:
            fees["commission"] = commission
        if side == "sell":
            fees["sec_fee"] = ceil_money(amount * p.us_sec_fee_rate)
            fees["finra_taf"] = min(ceil_money(p.us_taf_per_share * qty), p.us_taf_max)
        return _with_platform_fee(fees, p)

    def describe(self, p: RuleParams) -> list[str]:
        commission = (f"佣金 {float(p.commission_rate * 10000):g}‱（单笔最低 {float(p.commission_min):g} 美元）"
                      if p.commission_rate else "免佣金")
        return [
            _scope(p),
            "最小交易单位 1 股，不支持碎股。T+0：当天买入当天即可卖出。没有涨跌停限制（不模拟熔断）。",
            f"费用：{commission}；卖出时收取 SEC 费与 FINRA 交易活动费（金额很小）。",
            *leverage_lines(p, "US"),
        ]


def _scope(p: RuleParams) -> str:
    if not p.allow_short and not p.allow_margin:
        return "只能交易股票池中的标的；只能做多，不允许融资和卖空。"
    allowed = "、".join(x for x, on in (("融资买入", p.allow_margin), ("融券卖空", p.allow_short)) if on)
    banned = "、".join(x for x, on in (("融资", p.allow_margin), ("卖空", p.allow_short)) if not on)
    return f"只能交易股票池中的标的；允许{allowed}" + (f"，不允许{banned}。" if banned else "。")


def leverage_lines(p: RuleParams, market: str) -> list[str]:
    """做空 / 融资开启时的规则说明（给 AI 看）。"""
    if not p.leveraged:
        return []
    lines = []
    if p.allow_short:
        t1 = "；A股当日融券卖出的证券当日不能平空" if market == "CN" and p.cn_short_t1 else ""
        lines.append(
            "卖空：side 为 short 开空（融券卖出），cover 平空（买券还券）；空头持仓的数量显示为负数。"
            "持有多头时不能直接开空（须先卖出），持有空头时不能直接买入（须先平空）"
            f"{t1}。卖空数量规则与买入相同。")
        lines.append(f"空头每天按市值计提年化 {_pct(p.borrow_fee_rate)} 的融券费；"
                     "持有空头期间遇到分红，需补偿等额现金。")
    if p.allow_margin:
        lines.append("融资：现金不足时可以借款买入（现金显示为负数即融资负债），"
                     f"按年化 {_pct(p.margin_interest_rate)} 每天计息。")
    else:
        lines.append("不允许融资：买入只能使用自有现金，卖空所得资金不能用于买入。")
    ratio = ("维持担保比例 = 总资产 / 总负债（融资负债 + 空头市值）" if p.margin_check_method == "collateral_ratio"
             else "保证金比率 = 净值 / 总敞口")
    lines.append(
        f"杠杆：总敞口（多头市值 + 空头市值）不超过净值的 {float(p.leverage_cap):g} 倍。{ratio}，"
        f"低于 {_pct(p.maintenance_margin)} 会收到追保通知，下一次检查时仍不足或低于 {_pct(p.liquidation_margin)} "
        "将被强制平仓（按市值从大到小平仓，以下一交易日开盘价成交）。")
    if not p.allow_leveraged_etf:
        lines.append("不允许交易杠杆 / 反向 ETF。")
    return lines


_LEVERAGED_ETF = re.compile(r"杠杆|反向|两倍|二倍|三倍|做空|[23]x|ultra|inverse|bull|bear", re.IGNORECASE)


def is_leveraged_etf(inst: Instrument) -> bool:
    """按名称识别杠杆 / 反向 ETF（股票池中暂无这类品种，作为开关的兜底检查）。"""
    return inst.type == "etf" and bool(_LEVERAGED_ETF.search(inst.name or ""))


def closable_qty(inst: Instrument, pos: Position | None, on: date, side: str, p: RuleParams) -> int:
    """平仓方向（sell 平多 / cover 平空）在 on 日可平的数量。"""
    if side == "sell":
        return get_rules(inst.market).sellable_qty(inst, pos, on) if pos is not None and pos.qty > 0 else 0
    if pos is None or pos.qty >= 0:
        return 0
    qty = -pos.qty
    if inst.market == "CN" and p.cn_short_t1 and pos.last_buy_date == on:
        qty -= pos.last_buy_qty
    return max(qty, 0)


def validate_side_qty(inst: Instrument, side: str, qty: int, closable: int) -> str | None:
    """数量规则：开空按买入规则（整手）校验，平空按卖出规则（可零股一次性平掉）校验。"""
    msg = get_rules(inst.market).validate_qty(inst, "buy" if side in ("buy", "short") else "sell", qty, closable)
    if msg and side == "cover":
        msg = msg.replace("可卖数量", "可平空数量").replace("卖出", "平空")
    elif msg and side == "short":
        msg = msg.replace("买入", "卖空")
    return msg


_RULES: dict[str, MarketRules] = {"CN": CNRules(), "HK": HKRules(), "US": USRules()}


def get_rules(market: str) -> MarketRules:
    try:
        return _RULES[market]
    except KeyError as e:
        raise ValueError(f"未知市场：{market}") from e


def round_price(inst: Instrument, price: Decimal, direction: str) -> Decimal:
    """按该市场在此价位的最小变动单位取整。"""
    rules = get_rules(inst.market)
    return round_to_tick(price, rules.tick(inst, price), direction)  # type: ignore[arg-type]
