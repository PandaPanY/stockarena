"""幸存者偏差有多大：同一套规则在半时点股票池与全时点股票池上的回测对比。

命令（服务器上运行，结果写入 data/research/pit_compare.json）：
  uv run python -m app.quant.pit_compare

对比对象：在用（未归档）且股票池有全时点版本的策略的冠军版本，以及跟踪中的多风格组合。
另算一条「等权持有当天全部成分股」的基准，直接反映股票池本身的偏差。
"""

from __future__ import annotations

import json
import logging
from datetime import date

import numpy as np
import pandas as pd
from sqlalchemy import select

from app.analytics.metrics import nav_metrics
from app.core.config import get_settings
from app.db.session import get_sessionmaker
from app.quant.pit import HIST_OF

logger = logging.getLogger(__name__)

HALF_OF = {v: k for k, v in HIST_OF.items()}
KEYS = ("annualized_return", "sharpe", "max_drawdown", "volatility")


def _metrics(nav: pd.Series, bench: pd.Series, start: date, end: date) -> dict:
    m = (nav.index >= start) & (nav.index <= end)
    if m.sum() < 2:
        return {}
    met = nav_metrics(nav[m], bench[m])
    bm = nav_metrics(bench[m], None)
    out = {k: round(float(met[k]), 4) for k in KEYS if met.get(k) is not None}
    out["bench_return"] = round(float(bm["annualized_return"]), 4)
    out["bench_sharpe"] = round(float(bm["sharpe"]), 4)
    return out


def _pair(uni: str) -> tuple[tuple[str, str], tuple[str, str]]:
    """(("half", 半时点), ("full", 全时点))：策略切换到全时点股票池之后也能重算。"""
    half = uni if uni in HIST_OF else HALF_OF[uni]
    return ("half", half), ("full", HIST_OF[half])


def _windows(cfg, end: date) -> dict[str, tuple[date, date]]:
    return {"dev": (cfg.train_start, cfg.valid_end), "holdout": (cfg.holdout_start, end)}


def equal_weight(panel, cfg, end: date) -> dict:
    """等权持有当天全部成分股（每天再平衡，不计费用）：股票池本身的收益，最直接反映幸存者偏差。"""
    stock = panel.types == "stock"
    member = panel.member if panel.member is not None else np.ones(panel.shape, bool)
    ret = panel.adj_close[1:] / panel.adj_close[:-1] - 1
    ok = member[:-1] & stock & panel.has_bar[:-1] & np.isfinite(ret)
    ew = np.where(ok, ret, 0).sum(axis=1) / np.maximum(ok.sum(axis=1), 1)
    nav = pd.Series(np.concatenate([[1.0], np.cumprod(1 + ew)]), index=pd.Index(panel.dates))
    bench = pd.Series(panel.bench, index=pd.Index(panel.dates))
    counts = ok.sum(axis=1)
    out = {w: _metrics(nav, bench, a, b) for w, (a, b) in _windows(cfg, end).items()}
    out["members_2018"] = int(counts[panel.locate(date(2018, 1, 2), "after")])
    return out


def run() -> dict:
    from app.models.quant import QuantPortfolioRow, QuantStrategyRow, QuantVersionRow
    from app.quant.backtest import run_backtest
    from app.quant.data import get_panel
    from app.quant.portfolio import alloc_of, members_of, run_portfolio
    from app.quant.service import settings_of
    from app.quant.spec import parse_spec

    out: dict = {"strategies": [], "portfolios": [], "universes": {}}
    with get_sessionmaker()() as s:
        strategies = [st for st in s.scalars(select(QuantStrategyRow).where(QuantStrategyRow.status == "active"))
                      if (st.universe_name in HIST_OF or st.universe_name in HALF_OF) and st.champion_id]
        panels: dict[str, object] = {}

        def panel_of(uni: str, cfg):
            if uni not in panels:
                panels[uni] = get_panel(s, uni, cfg.train_start, date.today())
            return panels[uni]

        for st in strategies:
            cfg = settings_of(st)
            ver = s.get(QuantVersionRow, st.champion_id)
            spec = parse_spec(ver.spec)
            row = {"id": st.id, "name": st.name, "version": ver.number, "universe": st.universe_name}
            for kind, uni in _pair(st.universe_name):
                panel = panel_of(uni, cfg)
                end = panel.dates[-1]
                res = run_backtest(spec, panel, cfg.train_start, end, cfg.initial_cash)
                row[kind] = {w: _metrics(res.nav_series(), res.bench_series(), a, b)
                             for w, (a, b) in _windows(cfg, end).items()}
            out["strategies"].append(row)
            logger.info("对比完成：%s", st.name)
        for pf in s.scalars(select(QuantPortfolioRow)):
            members = members_of(s, pf)
            uni0 = members[0].strategy.universe_name if members else ""
            if len(members) < 2 or (uni0 not in HIST_OF and uni0 not in HALF_OF):
                continue
            cfg = settings_of(members[0].strategy)
            row = {"id": pf.id, "name": pf.name, "tracking": bool(pf.follow_account_id)}
            for kind, uni in _pair(uni0):
                panel = panel_of(uni, cfg)
                end = panel.dates[-1]
                res = run_portfolio(members, panel, cfg.train_start, end, cfg.initial_cash, alloc=alloc_of(pf))
                row[kind] = {w: _metrics(res.nav_series(), res.bench_series(), a, b)
                             for w, (a, b) in _windows(cfg, end).items()}
            out["portfolios"].append(row)
        cfg = settings_of(strategies[0]) if strategies else None
        if cfg:
            for uni in sorted(set(panels)):
                p = panels[uni]
                out["universes"][uni] = equal_weight(p, cfg, p.dates[-1]) | {"symbols": len(p.symbols)}
    path = get_settings().data_dir / "research" / "pit_compare.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(run(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
