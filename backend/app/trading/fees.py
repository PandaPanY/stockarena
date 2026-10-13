"""交易费用计算。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.core.money import round_money
from app.market.types import Instrument
from app.trading.params import RuleParams
from app.trading.types import Side


def calc_fees_cn(inst: Instrument, side: Side, qty: int, price: Decimal, p: RuleParams) -> dict[str, Decimal]:
    """A股费用：佣金（双边，有最低收费）；股票另收印花税（卖出）与过户费（双边），ETF 免收。"""
    amount = price * qty
    fees = {"commission": max(round_money(amount * p.commission_rate), p.commission_min)}
    if inst.type == "stock":
        fees["transfer_fee"] = round_money(amount * p.transfer_fee_rate)
        if side == "sell":
            fees["stamp_duty"] = round_money(amount * p.stamp_duty_rate)
    return fees


# A股个人投资者股息红利差别化个人所得税（财税〔2015〕101 号）：持股 ≤1 个月 20%、1 个月～1 年 10%、>1 年免征。
# 实际在卖出时按持有期补扣；这里在除息日按「建仓至今」的持有期扣除（之后继续持有超过 1 年时会略多扣，偏保守）。
# 证券投资基金（ETF 等）分红免征个人所得税。
CN_DIVIDEND_TAX = ((30, Decimal("0.2")), (365, Decimal("0.1")))


def dividend_tax_rate(inst: Instrument | None, opened: date | None, ex_date: date, p: RuleParams) -> Decimal:
    """除息日应扣的红利税率。未开启按持有期计算时使用统一税率 dividend_tax_rate。"""
    if not p.cn_dividend_tax_by_holding or inst is None or inst.market != "CN":
        return p.dividend_tax_rate
    if inst.type != "stock":
        return Decimal("0")
    if opened is None:
        return CN_DIVIDEND_TAX[1][1]
    days = (ex_date - opened).days
    for limit, rate in CN_DIVIDEND_TAX:
        if days <= limit:
            return rate
    return Decimal("0")


def estimate_buy_cost(inst: Instrument, qty: int, price: Decimal, p: RuleParams) -> Decimal:
    return price * qty + sum(calc_fees_cn(inst, "buy", qty, price, p).values(), Decimal("0"))
