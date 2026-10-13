"""基线选手：不使用 AI 的对照策略（PRD 3.6）。输出目标仓位；返回 None 表示本轮不调仓。"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from decimal import Decimal

from app.agent.context import MarketSnapshot
from app.trading.portfolio import AccountState

BASELINE_TYPES = {
    "buy_hold_index": ("买入持有基准", "首轮全仓买入基准指数对应的 ETF 并一直持有（不受单票上限约束）"),
    "equal_weight": ("等权买入持有", "首轮把资金等权分配到股票池所有可交易标的，之后不再调仓（不受单轮订单数约束）"),
    "random": ("随机交易", "每轮有 30% 概率随机选 5 个标的各配 18%，用于衡量「运气」水平"),
}

# 基准指数 → 可交易的跟踪 ETF
BENCHMARK_ETF = {
    "000016.SH": "510050.SH",
    "000300.SH": "510300.SH",
    "000905.SH": "510500.SH",
    "399006.SZ": "159915.SZ",
    "HSI.HK": "02800.HK",
    "NDX.US": "QQQ.US",
    "INX.US": "SPY.US",
}


@dataclass
class BaselineDecision:
    targets: dict[str, Decimal] | None
    analysis: str
    # 基线是对照组，部分风控约束对其不适用：买入持有基准需要全仓单一 ETF，等权组合需要一轮下几十笔单
    param_overrides: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)        # 额外写入决策记录的信息（量化选手的版本号等）


def _tradeable(snap: MarketSnapshot) -> list[str]:
    return [s for s, b in snap.ref_bars.items()
            if s in snap.instruments and b.date == snap.data_date and not b.suspended]


def decide_baseline(kind: str, state: AccountState, snap: MarketSnapshot, benchmark_symbol: str,
                    seed: str) -> BaselineDecision:
    if kind == "buy_hold_index":
        etf = BENCHMARK_ETF.get(benchmark_symbol)
        if state.positions:
            return BaselineDecision(None, "继续持有基准 ETF")
        if etf is None or etf not in snap.instruments:
            return BaselineDecision(None, f"股票池中没有基准 {benchmark_symbol} 对应的 ETF，无法执行")
        return BaselineDecision({etf: Decimal("1")}, f"全仓买入基准 ETF {etf}",
                                param_overrides={"max_position_pct": Decimal("1")})

    if kind == "equal_weight":
        if state.positions:
            return BaselineDecision(None, "继续持有，不调仓")
        symbols = _tradeable(snap)
        if not symbols:
            return BaselineDecision(None, "没有可交易标的")
        w = (Decimal("0.99") / len(symbols)).quantize(Decimal("0.0001"))
        return BaselineDecision({s: w for s in symbols}, f"等权买入 {len(symbols)} 个标的，各 {w:.2%}",
                                param_overrides={"max_orders_per_round": len(symbols) + 10})

    if kind == "random":
        rng = random.Random(seed)
        if state.positions and rng.random() >= 0.3:
            return BaselineDecision(None, "随机决定：本轮不调仓")
        symbols = _tradeable(snap)
        picks = rng.sample(symbols, k=min(5, len(symbols)))
        return BaselineDecision({s: Decimal("0.18") for s in picks}, f"随机选中：{', '.join(picks)}")

    raise ValueError(f"未知基线类型：{kind}")
