"""组合层资金配置（PRD 3.9 方向 B）：在多风格组合的成员之间怎样分配资金。

- fixed：固定权重（成员设置的权重）；
- inverse_vol：资金权重 ∝ 风险预算 / 成员过去 lookback 日的年化波动；
- erc：风险平价（等风险贡献）：按成员收益协方差，使各成员对组合波动的贡献与风险预算成比例；
- vol_target：组合预估波动高于目标时按比例降低总仓位（不加杠杆，最低保留 min_exposure）。

成员设置的权重作为「风险预算」。成员收益用各成员单独运行的回测净值计算，统一从行情矩阵第
MEMBER_START 行开始回测（回测与实时跟踪使用同一个全量矩阵，结果完全一致）；每个时点只用该日及以前的收益。
配置每 every 个交易日更新一次（与成员自己的调仓周期无关）。
"""

from __future__ import annotations

import json
from collections import OrderedDict
from dataclasses import asdict, dataclass

import numpy as np

from app.quant.panel import Panel
from app.quant.spec import StrategySpec

METHODS = {"fixed": "固定权重", "inverse_vol": "波动率倒数", "erc": "风险平价"}
MEMBER_START = 260              # 成员收益从行情矩阵第 260 行（约开始后一年，因子预热完成）起算
ANNUAL = 252


class AllocError(ValueError):
    pass


@dataclass
class AllocSettings:
    method: str = "fixed"
    lookback: int = 120         # 估计波动 / 协方差用的交易日数
    every: int = 20             # 每隔多少个交易日更新一次配置
    vol_target: float | None = None
    min_exposure: float = 0.3   # 目标波动降仓时的最低总仓位

    @classmethod
    def from_dict(cls, d: dict | None) -> AllocSettings:
        d = d or {}
        out = cls(method=str(d.get("method") or "fixed"), lookback=int(d.get("lookback") or 120),
                  every=int(d.get("every") or 20),
                  vol_target=float(d["vol_target"]) if d.get("vol_target") else None,
                  min_exposure=float(d.get("min_exposure") if d.get("min_exposure") is not None else 0.3))
        out.validate()
        return out

    def validate(self) -> None:
        if self.method not in METHODS:
            raise AllocError(f"未知配置方法 {self.method}，可用：{'、'.join(METHODS)}")
        if not 20 <= self.lookback <= 500 or not 1 <= self.every <= 120:
            raise AllocError("估计窗口 20～500 个交易日，更新间隔 1～120 个交易日")
        if self.vol_target is not None and not 0.02 <= self.vol_target <= 0.5:
            raise AllocError("目标波动 2%～50%")
        if not 0 <= self.min_exposure <= 1:
            raise AllocError("最低总仓位 0～1")

    @property
    def dynamic(self) -> bool:
        return self.method != "fixed" or self.vol_target is not None

    def to_dict(self) -> dict:
        return asdict(self)

    def describe(self) -> str:
        text = METHODS[self.method] + (f"（{self.lookback} 日估计，每 {self.every} 个交易日更新）"
                                       if self.method != "fixed" else "")
        if self.vol_target:
            text += f"，目标波动 {self.vol_target:.0%}（最低仓位 {self.min_exposure:.0%}）"
        return text


_cache: OrderedDict[tuple, np.ndarray] = OrderedDict()


def member_returns(specs: list[StrategySpec], panel: Panel) -> np.ndarray:
    """各成员单独运行的日收益 (T, K)，MEMBER_START 行之前为 NaN。"""
    from app.quant.backtest import run_backtest

    T = panel.shape[0]
    i0 = min(MEMBER_START, T - 2)
    out = np.full((T, len(specs)), np.nan)
    for k, sp in enumerate(specs):
        from app.quant.costs import current as current_costs

        key = (json.dumps(sp.model_dump(), sort_keys=True), panel.dates[0], panel.dates[-1], len(panel.symbols),
               id(panel), current_costs().signature())      # 不同用户的交易成本设置不同
        r = _cache.get(key)
        if r is None:
            res = run_backtest(sp, panel, panel.dates[i0], panel.dates[-1], 1_000_000)
            nav = res.nav
            r = np.full(T, np.nan)
            r[i0 + 1:i0 + len(nav)] = nav[1:] / nav[:-1] - 1
            _cache[key] = r
            while len(_cache) > 64:
                _cache.popitem(last=False)
        out[:, k] = r
    return out


def _erc(cov: np.ndarray, budget: np.ndarray, iters: int = 500) -> np.ndarray:
    """风险预算组合：w_i · (Σw)_i ∝ budget_i（迭代求解）。"""
    w = budget / np.sqrt(np.diag(cov))
    w /= w.sum()
    for _ in range(iters):
        marginal = cov @ w
        new = budget / np.maximum(marginal, 1e-12)
        new /= new.sum()
        if np.abs(new - w).max() < 1e-9:
            w = new
            break
        w = 0.5 * w + 0.5 * new
    return w / w.sum()


def allocate(returns: np.ndarray, i: int, budget: np.ndarray, cfg: AllocSettings) -> tuple[np.ndarray, float, str]:
    """第 i 日收盘后的成员资金权重（和为 1）与总仓位缩放系数。returns：member_returns 的结果。"""
    b = np.asarray(budget, dtype=float)
    b = b / b.sum()
    window = returns[max(i - cfg.lookback + 1, 0):i + 1]
    window = window[np.isfinite(window).all(axis=1)]
    # 数据不足或某个成员几乎没有波动（尚未建仓）时退回固定权重，避免除以接近 0 的波动
    if len(window) < max(20, cfg.lookback // 2):
        return b, 1.0, "成员收益数据不足，使用固定权重"
    vol = window.std(axis=0, ddof=1) * np.sqrt(ANNUAL)
    if (vol < 0.01).any():
        return b, 1.0, "有成员波动过低（可能尚未建仓），使用固定权重"
    cov = np.cov(window.T) * ANNUAL + np.eye(len(b)) * 1e-8
    if cfg.method == "inverse_vol":
        w = b / vol
        w = w / w.sum()
    elif cfg.method == "erc":
        w = _erc(cov, b)
    else:
        w = b
    scale = 1.0
    note = ""
    if cfg.vol_target:
        pv = float(np.sqrt(w @ cov @ w))
        scale = float(np.clip(cfg.vol_target / pv, cfg.min_exposure, 1.0)) if pv > 0 else 1.0
        note = f"组合预估波动 {pv:.1%}"
    return w, scale, note
