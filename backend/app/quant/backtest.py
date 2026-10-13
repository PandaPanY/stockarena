"""快速日频回测（A股规则）。

时序：T 日收盘后计算信号，T+1 日开盘按开盘价 ± 滑点成交，先卖后买。
规则：整手；一字涨停（开盘价在涨停价）不能买、开盘跌停不能卖；停牌不能交易；单笔成交额不超过当日成交额的一定比例；
费用：佣金（最低 5 元）、股票过户费、股票卖出印花税（2023-08-28 起 0.05%，之前 0.1%）。
分红送转：复权因子变化时按比例调整持股数（相当于分红再投资，近似处理）；
红利税（dividend_tax 开启时）：A股股票在除息日按建仓至今的持有期从现金中扣除（≤1 个月 20%、≤1 年 10%、>1 年免征），
与实时交易引擎（cn_dividend_tax_by_holding）一致；ETF 分红免征。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from app.quant.panel import Panel
from app.quant.signal import target_weights
from app.quant.spec import StrategySpec


@dataclass
class CostModel:
    commission_rate: float = 0.00025
    commission_min: float = 5.0
    transfer_rate: float = 0.00001
    stamp_rate: float = 0.0005
    stamp_rate_before: float = 0.001
    stamp_change: date = date(2023, 8, 28)
    slippage_bps: float = 5.0
    participation: float = 0.1
    # 冲击成本（平方根模型）：额外价格偏离 = 系数 × 日波动率 × √(成交额 / 当日成交额)。
    # 资金量小时几乎为 0（100 万买沪深300 成分股约 1～2 个基点），资金量大时显著；用于资金容量分析
    impact_coef: float = 1.0
    # A股股票红利税按持有期扣除（需要行情矩阵中的每股派现 cash_div）
    dividend_tax: bool = False


def dividend_tax_rate(held_days: int) -> float:
    """A股个人投资者股息红利差别化税率（与 app.trading.fees.CN_DIVIDEND_TAX 一致）。"""
    return 0.2 if held_days <= 30 else (0.1 if held_days <= 365 else 0.0)


@dataclass
class Trade:
    date: date
    symbol: str
    side: str            # buy / sell
    qty: float
    price: float
    amount: float
    fee: float
    reason: str          # rebalance / stop
    note: str = ""       # 具体原因：新入选第 3 名、掉出排名、亏损 8% 止损……


@dataclass
class Episode:
    """一次完整持有：从空仓买入到全部卖出（或回测结束时仍持有）。"""

    symbol: str
    entry: date
    exit: date | None            # None 表示回测结束时仍持有
    days: int                    # 持有交易日数
    cost: float                  # 买入总额（含费用）
    proceeds: float              # 卖出总额（扣费用）；仍持有时为期末市值
    exit_reason: str

    @property
    def pnl(self) -> float:
        return self.proceeds - self.cost

    @property
    def ret(self) -> float:
        return self.proceeds / self.cost - 1 if self.cost > 0 else 0.0


@dataclass
class BacktestResult:
    dates: list[date]
    nav: np.ndarray
    bench: np.ndarray
    exposure: np.ndarray                 # 持仓市值 / 净值
    holdings: np.ndarray                 # 持仓数量
    trades: list[Trade]
    initial_cash: float
    final_weights: dict[str, float] = field(default_factory=dict)
    pnl_by_symbol: dict[str, float] = field(default_factory=dict)
    closed: list[tuple[date, float]] = field(default_factory=list)   # 每次清仓的日期与收益率（胜率统计）
    blocked: dict[str, int] = field(default_factory=dict)   # 因涨跌停 / 停牌 / 成交额限制未能成交的次数
    buy_want: float = 0.0          # 计划买入总额（资金容量分析：实际买入 / 计划买入）
    buy_filled: float = 0.0        # 实际买入总额
    lot_blocked: int = 0           # 因资金不足一手而没有买入的次数（资金量太小）
    impact_cost: float = 0.0       # 冲击成本总额
    dividend_tax: float = 0.0      # 红利税总额
    deviation: float = 0.0         # 调仓后实际持仓与目标的平均偏离（½Σ|实际权重−目标权重|，资金太小时买不到一手会变大）
    episodes: list[Episode] = field(default_factory=list)
    rebalances: list[dict] = field(default_factory=list)    # explain=True 时：每次调仓的选股理由与卖出原因

    def nav_series(self) -> pd.Series:
        return pd.Series(self.nav, index=pd.Index(self.dates))

    def bench_series(self) -> pd.Series:
        return pd.Series(self.bench, index=pd.Index(self.dates))


def _limit_price(prev: float, pct: float, tick: float, up: bool) -> float:
    decimals = max(0, round(-math.log10(tick)))
    return round(prev * (1 + pct if up else 1 - pct), decimals)


def _stop_hits(risk, held: np.ndarray, loss: np.ndarray, dd: np.ndarray,
               only: np.ndarray | None = None) -> dict[int, str]:
    """按风控规则检查止损 / 移动止损（收盘判断）。only：只检查这些标的。返回 {下标: 原因}。"""
    out: dict[int, str] = {}
    scope = held if only is None else held & only
    with np.errstate(invalid="ignore"):
        if risk.stop_loss:
            out |= {int(j): f"亏损 {-loss[j]:.1%} 触发止损" for j in np.where(scope & (loss <= -risk.stop_loss))[0]}
        if risk.trailing_stop:
            out |= {int(j): f"从持有期高点回撤 {-dd[j]:.1%} 触发移动止损"
                    for j in np.where(scope & (dd <= -risk.trailing_stop))[0] if int(j) not in out}
    return out


# 多风格组合加减仓的最小比例：成员门槛按权重折算后过小（约 0.2%），会产生大量几千元的补仓单，
# 实盘操作繁琐且受最低佣金影响；0.5% 时五风格组合订单减少约三分之一，表现基本不变（2026-10-04 实测）。
PORTFOLIO_MIN_TRADE = 0.005


def portfolio_min_trade(members) -> float:
    """组合加减仓门槛：max(成员门槛 × 成员权重 的最小值, 0.5%)。新建仓与清仓不受门槛限制。"""
    return max(min(sp.rebalance.min_trade_pct * w for sp, w in members), PORTFOLIO_MIN_TRADE)


class SingleTargeter:
    """单个策略：每 every_days 个交易日按规格调仓；其余交易日只检查止损。"""

    def __init__(self, spec: StrategySpec):
        self.spec = spec
        self.min_trade = spec.rebalance.min_trade_pct
        self.new_free = True        # 新建仓不受 min_trade 限制（与实时引擎 rebalance_new_free 一致）

    def start(self, panel: Panel, i0: int) -> None:
        self.i0 = i0
        self.blocked = np.zeros(panel.shape[1], bool)

    def on_close(self, panel: Panel, i: int, held: np.ndarray, loss: np.ndarray, dd: np.ndarray,
                 explain: bool, rebalances: list[dict]):
        N = panel.shape[1]
        stop_notes = _stop_hits(self.spec.risk, held, loss, dd)
        exits = np.zeros(N, bool)
        exits[list(stop_notes)] = True
        if (i - self.i0) % self.spec.rebalance.every_days == 0:
            self.blocked = exits.copy()
            target, info = target_weights(self.spec, panel, i, held=held, blocked=self.blocked)
            target[exits] = 0.0
            notes = dict(stop_notes)
            if explain:
                notes |= _explain_rebalance(self.spec, panel, i, held, exits, target, info, rebalances)
            return target, None, notes
        if exits.any():
            self.blocked |= exits
            if explain:
                rebalances.append({"date": panel.dates[i].isoformat(), "exec": panel.dates[i + 1].isoformat(),
                                   "kind": "stop", "drops": [{"symbol": panel.symbols[j], "name": panel.names[j],
                                                             "reason": r} for j, r in stop_notes.items()]})
            return np.zeros(N), exits, stop_notes
        return None


class PortfolioTargeter:
    """多风格组合在同一个账户中运行（与实时跟踪的组合选手完全相同的规则，见 live.decide_portfolio）：

    每个成员按自己的调仓周期更新「成员目标权重」，成员规则中的止损按账户持仓成本检查；
    任一成员有变化时，账户整体调到 Σ 成员资金权重 × 成员目标（相当于各成员之间定期再平衡）。
    成员资金权重默认固定为成员设置的权重；alloc 为动态配置时（allocation.py）每 alloc.every 日按成员收益重新计算，
    并可按目标波动缩放总仓位。
    """

    def __init__(self, members: list[tuple[StrategySpec, float]], alloc=None):
        from app.quant.allocation import AllocSettings

        total = sum(w for _, w in members) or 1.0
        self.members = [(sp, w / total) for sp, w in members]
        self.min_trade = portfolio_min_trade(self.members)
        self.new_free = True
        self.alloc = alloc if alloc is not None else AllocSettings()

    def start(self, panel: Panel, i0: int) -> None:
        from app.quant.allocation import member_returns

        self.state: list[dict | None] = [None] * len(self.members)
        self.budget = np.array([w for _, w in self.members])
        self.alloc_w, self.scale, self.alloc_since = self.budget.copy(), 1.0, None
        self.returns = member_returns([sp for sp, _ in self.members], panel) if self.alloc.dynamic else None
        self.alloc_log: list[dict] = []

    def _update_alloc(self, panel: Panel, i: int) -> bool:
        """到更新日时重新计算成员资金权重与总仓位缩放；有变化返回 True。"""
        from app.quant.allocation import allocate

        if self.returns is None:
            return False
        since = self.alloc_since + 1 if self.alloc_since is not None else self.alloc.every
        if since < self.alloc.every:
            self.alloc_since = since
            return False
        self.alloc_since = 0
        w, scale, note = allocate(self.returns, i, self.budget, self.alloc)
        self.alloc_log.append({"date": panel.dates[i].isoformat(), "weights": [round(float(x), 4) for x in w],
                               "scale": round(scale, 4), "note": note})
        if np.allclose(w, self.alloc_w, atol=1e-4) and abs(scale - self.scale) < 1e-4:
            return False
        self.alloc_w, self.scale = w, scale
        return True

    def on_close(self, panel: Panel, i: int, held: np.ndarray, loss: np.ndarray, dd: np.ndarray,
                 explain: bool, rebalances: list[dict]):
        N = panel.shape[1]
        changed = self._update_alloc(panel, i)
        notes: dict[int, str] = {}
        for k, (sp, _) in enumerate(self.members):
            st = self.state[k]
            every = sp.rebalance.every_days
            since = st["since"] + 1 if st else every
            weights = st["weights"] if st else np.zeros(N)
            mine = weights > 0
            exits = _stop_hits(sp.risk, held, loss, dd, only=mine)
            if st is None or since >= every:
                blocked = np.zeros(N, bool)
                blocked[list(exits)] = True
                weights, _ = target_weights(sp, panel, i, held=held, blocked=blocked)
                since, changed = 0, True
            elif exits:
                weights = weights.copy()
                weights[list(exits)] = 0.0
                changed = True
            notes |= exits
            self.state[k] = {"since": since, "weights": weights}
        if not changed:
            return None
        combined = np.zeros(N)
        for w, st in zip(self.alloc_w, self.state, strict=True):
            combined += w * st["weights"]
        return combined * self.scale, None, notes


def run_backtest(spec: StrategySpec | None, panel: Panel, start: date, end: date, initial_cash: float = 1_000_000,
                 cost: CostModel | None = None, explain: bool = False, targeter=None) -> BacktestResult:
    """explain=True 时额外记录每次调仓的选股理由（较慢，只用于展示交易明细）。

    targeter：收盘后如何给出目标（默认按单个策略规格；多风格组合用 PortfolioTargeter，在同一账户中运行）。
    """
    targeter = targeter or SingleTargeter(spec)
    if cost is None:
        from app.quant.costs import current

        cost = current().cost_model()
    i0, i1 = panel.locate(start, "after"), panel.locate(end, "before")
    if i0 > i1:
        raise ValueError(f"{start} ～ {end} 没有交易日")
    targeter.start(panel, i0)
    T, N = panel.shape
    slip = cost.slippage_bps / 1e4
    lot = panel.lot
    is_stock = panel.types == "stock"

    cash = float(initial_cash)
    shares = np.zeros(N)
    cost_basis = np.zeros(N)        # 当前持仓的累计买入成本（元）
    adj_units = np.zeros(N)         # 以复权价计的持仓单位，cost_basis / adj_units 即复权成本价
    peak = np.zeros(N)              # 持有期最高复权收盘价
    episode_cost = np.zeros(N)      # 本轮持有的买入总额（含费用）与卖出总额，用于胜率
    episode_proceeds = np.zeros(N)
    pnl = np.zeros(N)
    trades: list[Trade] = []
    closed: list[tuple[date, float]] = []
    blocked_count: dict[str, int] = {}
    # (目标权重, 只卖出这些标的, 各标的交易原因)
    pending: tuple[np.ndarray, np.ndarray | None, dict[int, str]] | None = None
    entry_i = np.full(N, -1)        # 本轮持有的买入日下标
    episodes: list[Episode] = []
    rebalances: list[dict] = []

    navs, exposures, counts = [], [], []

    def fee(amount: float, j: int, side: str, d: date) -> float:
        f = max(amount * cost.commission_rate, cost.commission_min)
        if is_stock[j]:
            f += amount * cost.transfer_rate
            if side == "sell":
                f += amount * (cost.stamp_rate if d >= cost.stamp_change else cost.stamp_rate_before)
        return f

    def note_block(j: int) -> None:
        blocked_count[panel.symbols[j]] = blocked_count.get(panel.symbols[j], 0) + 1

    from app.quant.factors import compute as _factor

    daily_vol = np.nan_to_num(_factor(panel, "volatility", {"window": 20}), nan=0.3) / math.sqrt(252)
    buy_want = buy_filled = impact_total = div_tax_total = 0.0
    cash_div = panel.cash_div if cost.dividend_tax else None
    lot_blocked = 0
    deviations: list[float] = []

    def impact(j: int, i: int, amount: float) -> float:
        day = panel.amount[i, j]
        if not cost.impact_coef or not np.isfinite(day) or day <= 0 or amount <= 0:
            return 0.0
        return cost.impact_coef * daily_vol[max(i - 1, 0), j] * math.sqrt(min(amount / day, 1.0))

    for i in range(i0, i1 + 1):
        d = panel.dates[i]
        # 1. 分红送转：复权因子变化 → 持股数按比例调整（先按除息前持股数扣红利税）
        if cash_div is not None and i > i0:
            for j in np.where((shares > 0) & (cash_div[i] > 0) & is_stock)[0]:
                held_days = (d - panel.dates[entry_i[j]]).days if entry_i[j] >= 0 else 0
                tax = dividend_tax_rate(held_days) * cash_div[i, j] * shares[j]
                cash -= tax
                pnl[j] -= tax
                div_tax_total += tax
        if i > i0:
            ratio = panel.factor[i] / panel.factor[i - 1]
            changed = (shares > 0) & (np.abs(ratio - 1) > 1e-9)
            if changed.any():
                shares[changed] *= ratio[changed]
        # 1b. 已退市的持仓：之后没有行情、无法交易，按退市前最后一个交易日的收盘价清算
        if panel.delisted is not None:
            for j in np.where((shares > 0) & (panel.delisted <= i))[0]:
                price = float(panel.close_ff[i, j])
                amount = shares[j] * price if np.isfinite(price) else 0.0
                f = fee(amount, j, "sell", d) if amount > 0 else 0.0
                cash += amount - f
                pnl[j] += amount - f
                episode_proceeds[j] += amount - f
                trades.append(Trade(d, panel.symbols[j], "sell", float(shares[j]), price, amount, f, "rebalance",
                                    "退市：按最后交易日收盘价清算"))
                if episode_cost[j] > 0:
                    closed.append((d, episode_proceeds[j] / episode_cost[j] - 1))
                    k = int(entry_i[j]) if entry_i[j] >= 0 else i
                    episodes.append(Episode(panel.symbols[j], panel.dates[k], d, i - k, float(episode_cost[j]),
                                            float(episode_proceeds[j]), "退市清算"))
                shares[j] = cost_basis[j] = adj_units[j] = peak[j] = 0.0
                episode_cost[j] = episode_proceeds[j] = 0.0
                entry_i[j] = -1

        # 2. 执行上一交易日收盘产生的信号
        if pending is not None:
            target, only_sell, notes = pending
            pending = None
            px_open = np.where(panel.has_bar[i], panel.open[i], panel.close_ff[i - 1] if i > 0 else np.nan)
            nav_open = cash + float(np.nansum(shares * np.nan_to_num(px_open)))
            cur_w = np.where(shares > 0, shares * np.nan_to_num(px_open), 0.0) / nav_open
            min_trade = targeter.min_trade
            prev = panel.prev_close[i]
            cap = cost.participation * np.nan_to_num(panel.amount[i])
            # 先卖
            if only_sell is not None:
                sells = np.where(only_sell & (shares > 0))[0]
            else:
                sells = np.where((shares > 0) & ((target == 0) | (cur_w - target > min_trade)))[0]
            for j in sells:
                if not panel.has_bar[i, j]:
                    note_block(j)
                    continue
                o = panel.open[i, j]
                if np.isfinite(prev[j]) and np.isfinite(panel.limit_pct[j]) and \
                        o <= _limit_price(prev[j], panel.limit_pct[j], panel.tick[j], up=False) + panel.tick[j] / 2:
                    note_block(j)
                    continue
                price = o * (1 - slip)
                tgt = 0.0 if only_sell is not None else target[j]
                if tgt <= 0:
                    qty = shares[j]
                else:
                    keep = math.floor(tgt * nav_open / price / lot[j]) * lot[j]
                    qty = shares[j] - keep
                if qty * price > cap[j]:
                    qty = math.floor(cap[j] / price / lot[j]) * lot[j]
                    note_block(j)
                if qty <= 1e-9:
                    continue
                imp = impact(j, i, qty * price)
                if imp:
                    price *= 1 - imp
                    impact_total += qty * o * imp
                amount = qty * price
                f = fee(amount, j, "sell", d)
                cash += amount - f
                pnl[j] += amount - f
                frac = min(qty / shares[j], 1.0)
                cost_basis[j] *= 1 - frac
                adj_units[j] *= 1 - frac
                episode_proceeds[j] += amount - f
                shares[j] -= qty
                full = shares[j] < 1e-6
                note = notes.get(j) or ("调仓卖出" if full else "权重调整：减仓")
                trades.append(Trade(d, panel.symbols[j], "sell", qty, price, amount, f,
                                    "stop" if only_sell is not None else "rebalance", note))
                if full:
                    shares[j] = cost_basis[j] = adj_units[j] = peak[j] = 0.0
                    if episode_cost[j] > 0:
                        closed.append((d, episode_proceeds[j] / episode_cost[j] - 1))
                        k = int(entry_i[j]) if entry_i[j] >= 0 else i
                        episodes.append(Episode(panel.symbols[j], panel.dates[k], d, i - k, float(episode_cost[j]),
                                                float(episode_proceeds[j]), note))
                    episode_cost[j] = episode_proceeds[j] = 0.0
                    entry_i[j] = -1
            # 再买
            if only_sell is None:
                cur_val = shares * np.nan_to_num(px_open)
                want = target * nav_open - cur_val
                new_free = getattr(targeter, "new_free", False)
                buys = [j for j in np.argsort(-want) if target[j] > 0 and want[j] > 0
                        and (want[j] > min_trade * nav_open or (new_free and shares[j] <= 0))]
                for j in buys:
                    if not panel.has_bar[i, j]:
                        note_block(j)
                        continue
                    o = panel.open[i, j]
                    if np.isfinite(prev[j]) and np.isfinite(panel.limit_pct[j]) and \
                            o >= _limit_price(prev[j], panel.limit_pct[j], panel.tick[j], up=True) - panel.tick[j] / 2:
                        note_block(j)
                        continue
                    price = o * (1 + slip)
                    budget = min(want[j], max(cash, 0.0), cap[j])   # 满仓时扣红利税可能使现金略为负
                    buy_want += want[j]
                    if budget < want[j] and budget == cap[j]:
                        note_block(j)
                    imp = impact(j, i, budget)
                    price *= 1 + imp
                    unit = price * (1 + cost.commission_rate + cost.transfer_rate)
                    qty = math.floor(budget / unit / lot[j]) * lot[j]
                    while qty > 0 and qty * price + fee(qty * price, j, "buy", d) > cash:
                        qty -= lot[j]
                    if qty <= 0:
                        if budget > 0 and budget < price * lot[j]:
                            lot_blocked += 1
                        continue
                    amount = qty * price
                    buy_filled += amount
                    impact_total += qty * o * imp
                    f = fee(amount, j, "buy", d)
                    cash -= amount + f
                    pnl[j] -= amount + f
                    adj_price = price * panel.factor[i, j]
                    new = shares[j] <= 0
                    if new:
                        peak[j] = adj_price
                        entry_i[j] = i
                    shares[j] += qty
                    cost_basis[j] += amount + f
                    adj_units[j] += (amount + f) / adj_price
                    episode_cost[j] += amount + f
                    trades.append(Trade(d, panel.symbols[j], "buy", qty, price, amount, f, "rebalance",
                                        notes.get(j) or ("买入" if new else "权重调整：加仓")))
                held_val = shares * np.nan_to_num(px_open)
                nav_now = cash + float(held_val.sum())
                if nav_now > 0:
                    deviations.append(0.5 * float(np.abs(held_val / nav_now - target).sum()))

        # 3. 收盘估值
        value = shares * np.nan_to_num(panel.close_ff[i])
        nav = cash + float(value.sum())
        navs.append(nav)
        exposures.append(float(value.sum()) / nav if nav > 0 else 0.0)
        counts.append(int((shares > 0).sum()))
        held = shares > 0
        adj = panel.adj_close[i]
        peak = np.where(held, np.fmax(peak, np.nan_to_num(adj)), 0.0)

        # 4. 收盘后计算信号（最后一天不再下单）
        if i == i1:
            break
        with np.errstate(divide="ignore", invalid="ignore"):
            entry = np.where(adj_units > 0, cost_basis / adj_units, np.nan)
            loss = adj / entry - 1
            dd = adj / np.where(peak > 0, peak, np.nan) - 1
        pending = targeter.on_close(panel, i, held, loss, dd, explain, rebalances)

    final_value = shares * np.nan_to_num(panel.close_ff[i1])
    pnl += final_value
    for j in np.where(shares > 0)[0]:
        k = int(entry_i[j]) if entry_i[j] >= 0 else i0
        episodes.append(Episode(panel.symbols[j], panel.dates[k], None, i1 - k, float(episode_cost[j]),
                                float(episode_proceeds[j] + final_value[j]), "回测结束时仍持有"))
    nav_last = navs[-1]
    return BacktestResult(
        dates=panel.dates[i0:i1 + 1], nav=np.array(navs), bench=panel.bench[i0:i1 + 1].astype(float),
        exposure=np.array(exposures), holdings=np.array(counts), trades=trades, initial_cash=float(initial_cash),
        final_weights={panel.symbols[j]: float(final_value[j] / nav_last) for j in np.where(final_value > 0)[0]},
        pnl_by_symbol={panel.symbols[j]: float(pnl[j]) for j in np.where(np.abs(pnl) > 1e-6)[0]},
        closed=closed, blocked=blocked_count, episodes=episodes, rebalances=rebalances,
        buy_want=buy_want, buy_filled=buy_filled, lot_blocked=lot_blocked, impact_cost=impact_total,
        dividend_tax=div_tax_total, deviation=float(np.mean(deviations)) if deviations else 0.0,
    )


def _explain_rebalance(spec, panel, i, held, exits, target, info, out: list[dict]) -> dict[int, str]:
    """记录本次调仓的选股理由与卖出原因，返回各标的的交易原因。"""
    from app.quant.explain import drop_reason, picks, timing_text
    from app.quant.signal import eligible, scores

    chosen = [int(j) for j in np.argsort(-target) if target[j] > 0]
    pk = picks(spec, panel, i, chosen, target, blocked=exits)
    notes: dict[int, str] = {}
    for p, j in zip(pk, chosen, strict=True):
        if spec.timing.defensive and panel.symbols[j] == spec.timing.defensive and not info["timing_on"]:
            p["defensive"] = True
            notes[j] = "大盘择时转入防守资产"
        elif not held[j]:
            notes[j] = f"新入选：综合排名第 {p['rank']}/{p['of']} 名"
    drops = []
    dropped = np.where(held & (target <= 0))[0]
    sc = scores(spec, panel, i, eligible(spec, panel, i)) if len(dropped) else None
    for j in dropped:
        r = drop_reason(spec, panel, i, int(j), blocked=bool(exits[j]), sc=sc)
        notes.setdefault(int(j), r)
        drops.append({"symbol": panel.symbols[j], "name": panel.names[j], "reason": notes[int(j)]})
    out.append({"date": panel.dates[i].isoformat(), "exec": panel.dates[i + 1].isoformat(), "kind": "rebalance",
                "eligible": info["eligible"], "timing": timing_text(spec, panel, i), "timing_on": info["timing_on"],
                "picks": pk, "drops": drops,
                "kept": int((held & (target > 0)).sum()), "new": int((~held & (target > 0)).sum())})
    return notes
