"""账户状态：现金、持仓（多头 / 空头）、成交入账、除权除息、融资融券计息、保证金比率。

资金变动全部生成流水（LedgerEntry）。约定（PRD 4.6.5）：
- 净值 NAV = 现金 + Σ 多头市值 − Σ 空头市值；持仓数量带符号，因此 NAV = 现金 + Σ 价格 × 数量
- 融券卖出所得计入现金，同时计为空头负债；开启融资时现金可以为负，负值部分即融资负债
- 总敞口 = Σ 多头市值 + Σ |空头市值|；杠杆率 = 总敞口 / NAV
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_FLOOR, Decimal

from app.core.money import ZERO, round_money
from app.market.types import CorporateAction
from app.trading.params import RuleParams
from app.trading.types import SIDE_LABELS, Fill, LedgerEntry, Position


@dataclass
class AccountState:
    cash: Decimal
    positions: dict[str, Position] = field(default_factory=dict)

    def position(self, symbol: str) -> Position | None:
        return self.positions.get(symbol)

    def _check_prices(self, prices: dict[str, Decimal]) -> None:
        missing = set(self.positions) - set(prices)
        if missing:
            raise KeyError(f"缺少持仓标的价格：{sorted(missing)}")

    def market_value(self, prices: dict[str, Decimal]) -> Decimal:
        """持仓净市值（多头为正、空头为负）。"""
        return sum((prices[s] * p.qty for s, p in self.positions.items()), ZERO)

    def long_value(self, prices: dict[str, Decimal]) -> Decimal:
        return sum((prices[s] * p.qty for s, p in self.positions.items() if p.qty > 0), ZERO)

    def short_value(self, prices: dict[str, Decimal]) -> Decimal:
        """空头市值（正数）。"""
        return sum((prices[s] * -p.qty for s, p in self.positions.items() if p.qty < 0), ZERO)

    def gross_exposure(self, prices: dict[str, Decimal]) -> Decimal:
        return self.long_value(prices) + self.short_value(prices)

    def nav(self, prices: dict[str, Decimal]) -> Decimal:
        """净值 = 现金 + 多头市值 − 空头市值。prices 必须包含所有持仓标的的价格。"""
        self._check_prices(prices)
        return self.cash + self.market_value(prices)

    def leverage(self, prices: dict[str, Decimal]) -> Decimal | None:
        nav = self.nav(prices)
        return self.gross_exposure(prices) / nav if nav > 0 else None

    def buying_power(self, prices: dict[str, Decimal], p: RuleParams) -> Decimal:
        """还能用于买入（含费用）的金额。

        不允许融资：只能用自有现金，融券卖出所得不能用于买入（现金 − 空头市值）；
        同时受杠杆上限约束：净值 × 杠杆上限 − 现有总敞口。
        """
        nav = self.nav(prices)
        room = nav * p.leverage_cap - self.gross_exposure(prices)
        if p.allow_margin:
            return max(room, ZERO)
        return max(min(self.cash - self.short_value(prices), room), ZERO)

    def short_room(self, prices: dict[str, Decimal], p: RuleParams) -> Decimal:
        """还能新开的空头市值。"""
        if not p.allow_short:
            return ZERO
        return max(self.nav(prices) * p.leverage_cap - self.gross_exposure(prices), ZERO)

    # ---------- 成交 ----------

    def apply_fill(self, fill: Fill) -> list[LedgerEntry]:
        label = SIDE_LABELS.get(fill.side, fill.side)
        incoming = fill.side in ("sell", "short")
        entries = [LedgerEntry("trade", fill.amount if incoming else -fill.amount, fill.symbol,
                               f"{label} {fill.qty}@{fill.price}", fill.order_id)]
        if fill.total_fee:
            entries.append(LedgerEntry("fee", -fill.total_fee, fill.symbol,
                                       ",".join(f"{k}={v}" for k, v in fill.fees.items()), fill.order_id))
        pos = self.positions.get(fill.symbol)
        if fill.side in ("buy", "short"):
            if pos is not None and (pos.qty < 0 if fill.side == "buy" else pos.qty > 0):
                raise ValueError(f"{fill.symbol} 持有反向仓位，需先平仓再{label}")
            if pos is None:
                pos = Position(fill.symbol, 0, ZERO, opened_date=fill.trade_date)
                self.positions[fill.symbol] = pos
            held = abs(pos.qty)
            if fill.side == "buy":       # 成本含买入费用
                pos.avg_cost = (pos.avg_cost * held + fill.amount + fill.total_fee) / (held + fill.qty)
                pos.qty += fill.qty
            else:                        # 空头均价为扣除费用后的卖空所得
                pos.avg_cost = (pos.avg_cost * held + fill.amount - fill.total_fee) / (held + fill.qty)
                pos.qty -= fill.qty
            if pos.last_buy_date == fill.trade_date:
                pos.last_buy_qty += fill.qty
            else:
                pos.last_buy_date, pos.last_buy_qty = fill.trade_date, fill.qty
        else:
            long_close = fill.side == "sell"
            if pos is None or (pos.qty < fill.qty if long_close else -pos.qty < fill.qty):
                raise ValueError(f"{label}数量超过持仓：{fill.symbol}")
            if long_close:
                fill.realized_pnl = round_money((fill.price - pos.avg_cost) * fill.qty - fill.total_fee)
                pos.qty -= fill.qty
            else:
                fill.realized_pnl = round_money((pos.avg_cost - fill.price) * fill.qty - fill.total_fee)
                pos.qty += fill.qty
            if pos.qty == 0:
                del self.positions[fill.symbol]
        self.cash += fill.cash_delta
        return entries

    # ---------- 除权除息 ----------

    def apply_corporate_action(self, action: CorporateAction, ex_date: date,
                               dividend_tax_rate: Decimal = ZERO) -> list[LedgerEntry]:
        """在除权除息日开盘前处理：现金分红入账（空头补偿分红）、送转股调整持仓数量与成本。"""
        pos = self.positions.get(action.symbol)
        if pos is None or pos.qty == 0:
            return []
        entries: list[LedgerEntry] = []
        old_qty = abs(pos.qty)
        short = pos.qty < 0
        if action.cash_per_share > 0:
            gross = round_money(action.cash_per_share * old_qty)
            if short:
                # 空头需向出借方补偿等额分红（按税前金额），空头均价同步下调
                self.cash -= gross
                pos.avg_cost = pos.avg_cost - gross / old_qty
                entries.append(LedgerEntry("dividend", -gross, action.symbol,
                                           f"空头补偿分红 每股{action.cash_per_share} × {old_qty}"))
            else:
                net = round_money(gross * (1 - dividend_tax_rate))
                self.cash += net
                pos.avg_cost = pos.avg_cost - net / old_qty
                entries.append(LedgerEntry("dividend", net, action.symbol,
                                           f"现金分红 每股{action.cash_per_share} × {old_qty}（税前 {gross}）"))
        if action.shares_per_share != 0:
            # 送转 / 拆股为正；合股（反向拆股）为负。零碎股向下取整（简化处理，不补现金）
            new_qty = int((old_qty * (1 + action.shares_per_share)).to_integral_value(rounding=ROUND_FLOOR))
            if new_qty > 0 and new_qty != old_qty:
                pos.qty = -new_qty if short else new_qty
                pos.avg_cost = pos.avg_cost * old_qty / new_qty
                # 送转股与原持仓同步可卖（简化处理）
                label = "送转/拆股" if new_qty > old_qty else "合股"
                entries.append(LedgerEntry("adjust", ZERO, action.symbol,
                                           f"{label} 每股{action.shares_per_share:+}：{old_qty} → {new_qty} 股"
                                           + ("（空头）" if short else "")))
        return entries

    # ---------- 融资融券计息 ----------

    def accrue_financing(self, prices: dict[str, Decimal], p: RuleParams, days: int) -> list[LedgerEntry]:
        """按自然日计提融资利息（负现金部分）与融券费用（空头市值），收盘后调用。"""
        if days <= 0:
            return []
        entries: list[LedgerEntry] = []
        debt = -self.cash if self.cash < 0 else ZERO
        if debt > 0 and p.margin_interest_rate > 0:
            interest = round_money(debt * p.margin_interest_rate * days / p.day_count)
            if interest > 0:
                self.cash -= interest
                note = f"融资利息：负债 {debt:.2f} × {p.margin_interest_rate:%} × {days}/{p.day_count}"
                entries.append(LedgerEntry("interest", -interest, None, note))
        for sym, pos in sorted(self.positions.items()):
            if pos.qty >= 0 or p.borrow_fee_rate <= 0:
                continue
            value = prices[sym] * -pos.qty
            fee = round_money(value * p.borrow_fee_rate * days / p.day_count)
            if fee > 0:
                self.cash -= fee
                entries.append(LedgerEntry("borrow_fee", -fee, sym,
                                           f"融券费：市值 {value:.2f} × {p.borrow_fee_rate:%} × {days}/{p.day_count}"))
        return entries

    # ---------- 保证金 ----------

    def margin_ratio(self, prices: dict[str, Decimal], p: RuleParams) -> Decimal | None:
        """保证金比率；没有负债（无融资、无空头）时返回 None。

        equity_ratio：净值 / 总敞口；collateral_ratio（A股维持担保比例）：总资产 / 总负债。
        """
        short = self.short_value(prices)
        debt = -self.cash if self.cash < 0 else ZERO
        if short == 0 and debt == 0:
            return None
        if p.margin_check_method == "collateral_ratio":
            assets = max(self.cash, ZERO) + self.long_value(prices)
            return assets / (debt + short)
        gross = self.gross_exposure(prices)
        return self.nav(prices) / gross if gross > 0 else None
