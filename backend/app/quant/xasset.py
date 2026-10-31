"""跨资产 ETF 配置研究（预注册：docs/prereg/2026-10-31-cross-asset.md，只按预注册的定义实现）。

命令（服务器上运行，结果写入 data/research/xasset.json）：
  uv run python -m app.quant.xasset dev        开发期：4 个候选 + 2 个对照，挑选与主要检验
  uv run python -m app.quant.xasset holdout    留出期：只在开发期通过时运行，只运行一次
  uv run python -m app.quant.xasset forward    前瞻期：满 250 个交易日时给出评估结论，之前只显示进度
  uv run python -m app.quant.xasset library    开发期与留出期都通过后：把选中的方案加入策略库（参数冻结）并开始实时跟踪

每个方案是若干「只持有一只 ETF」的成员策略，用组合引擎（PortfolioTargeter + allocation.py）分配资金；
从 2018-01-01 起连续运行一次，各期间的指标从同一条净值曲线上截取。
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import date

import numpy as np

from app.quant.allocation import AllocSettings
from app.quant.backtest import BacktestResult, PortfolioTargeter, run_backtest
from app.quant.evaluate import paired_test, window_report
from app.quant.panel import Panel
from app.quant.spec import StrategySpec, parse_spec

logger = logging.getLogger(__name__)

UNIVERSE = "cn_etf_core"
ASSETS = {"510300.SH": "A 股", "511010.SH": "国债", "518880.SH": "黄金", "513100.SH": "美股"}
STOCK, BOND = "510300.SH", "511010.SH"
START = date(2018, 1, 1)
DEV = (date(2018, 1, 1), date(2024, 12, 31))
HOLDOUT_START = date(2025, 1, 1)
FORWARD_START = date(2026, 11, 2)       # 预注册提交（2026-10-31）后的第一个交易日
FORWARD_DAYS = 250
CASH = 1_000_000
EVERY = 20
LOOKBACK = 120
TREND_WINDOW = 200
SEED = 20261031
N_BOOT = 10_000
P_MAX = 0.05 / 4                        # 4 个候选
NEIGHBORS = ({"lookback": 60}, {"lookback": 250}, {"every": 10}, {"every": 60})
KEYS = ("annualized_return", "volatility", "sharpe", "max_drawdown", "cumulative_return")

CANDIDATES = {
    "A": "固定等权",
    "B": "波动率倒数",
    "C": "风险平价（等风险贡献）",
    "D": "等权 + 200 日均线趋势过滤",
}
BENCHMARKS = {"csi300": "沪深300 持有", "60_40": "股债 60/40"}


def hold(symbol: str, every: int = EVERY, trend: bool = False) -> StrategySpec:
    """只持有一只 ETF 的成员策略；trend=True 时收盘价低于 200 日均线就不持有（这部分资金留作现金）。"""
    d = {"universe": {"types": ["etf"], "symbols": [symbol], "min_listed_days": 0, "min_amount": 0},
         "factors": [{"name": "momentum", "params": {"window": 20}}],
         "selection": {"top_n": 1}, "weighting": {"method": "equal", "max_weight": 1.0},
         "rebalance": {"every_days": every, "min_trade_pct": 0.02}, "risk": {"exposure": 0.98}}
    if trend:
        d["filters"] = [{"name": "ma_bias", "params": {"window": TREND_WINDOW}, "op": ">", "value": 0}]
    return parse_spec(d)


def plan(key: str, lookback: int = LOOKBACK, every: int = EVERY) -> tuple[list[tuple[StrategySpec, float]],
                                                                          AllocSettings]:
    """方案的 (成员, 资金配置)。"""
    if key == "csi300":
        return [(hold(STOCK, every), 1.0)], AllocSettings()
    if key == "60_40":
        return [(hold(STOCK, every), 0.6), (hold(BOND, every), 0.4)], AllocSettings()
    members = [(hold(s, every, trend=key == "D"), 1.0) for s in ASSETS]
    method = {"A": "fixed", "B": "inverse_vol", "C": "erc", "D": "fixed"}[key]
    return members, AllocSettings(method=method, lookback=lookback, every=every)


def run(panel: Panel, key: str, end: date, **kw) -> tuple[BacktestResult, PortfolioTargeter]:
    """连续运行一个方案。成本固定为系统默认值（预注册写定），不受用户在页面上改过的成本设置影响。"""
    from app.quant import costs

    members, alloc = plan(key, **kw)
    targeter = PortfolioTargeter(members, alloc)
    with costs.fixed(costs.TradeCosts()):
        return run_backtest(None, panel, START, end, CASH, targeter=targeter), targeter


def _brief(rep: dict) -> dict:
    out = {k: rep.get(k) for k in KEYS}
    out |= {"start": rep.get("start"), "end": rep.get("end"), "days": rep.get("days"),
            "avg_exposure": rep.get("avg_exposure"), "turnover": rep.get("turnover"),
            "cost_ratio": rep.get("cost_ratio"),
            "yearly": {y["year"]: y.get("return") for y in rep.get("yearly") or []}}
    return out


def _avg_weights(targeter: PortfolioTargeter, start: date, end: date) -> dict | None:
    """动态配置（B、C）在期间内的平均资金权重。"""
    log = [x for x in targeter.alloc_log if start.isoformat() <= x["date"] <= end.isoformat()]
    if not log:
        return None
    w = np.array([x["weights"] for x in log]).mean(axis=0)
    return {name: round(float(v), 4) for name, v in zip(ASSETS.values(), w, strict=True)}


def evaluate_dev(panel: Panel) -> dict:
    """开发期：计算 4 个候选与 2 个对照，按开发期夏普挑选，做主要检验。"""
    a, b = DEV
    end = min(b, panel.dates[-1])
    reps: dict[str, dict] = {}
    rows: dict[str, dict] = {}
    for key in (*CANDIDATES, *BENCHMARKS):
        res, tg = run(panel, key, end)
        reps[key] = window_report(res, a, end, monthly=True)
        rows[key] = _brief(reps[key]) | {"title": CANDIDATES.get(key) or BENCHMARKS[key]}
        if key in ("B", "C"):
            rows[key]["avg_weights"] = _avg_weights(tg, a, end)
        logger.info("开发期 %s：夏普 %s", key, reps[key].get("sharpe"))
    chosen = max(CANDIDATES, key=lambda k: reps[k].get("sharpe") if reps[k].get("sharpe") is not None else -9)
    c, base = reps[chosen], reps["60_40"]
    test = paired_test(c, base, n_boot=N_BOOT, seed=SEED)
    checks = {
        "sharpe_above_60_40": c.get("sharpe") is not None and base.get("sharpe") is not None
        and c["sharpe"] > base["sharpe"],
        "significant": test is not None and test["p"] <= P_MAX,
        "drawdown_below_60_40": c.get("max_drawdown") is not None and base.get("max_drawdown") is not None
        and c["max_drawdown"] < base["max_drawdown"],
    }
    neighbors = []
    for kw in NEIGHBORS:
        if "lookback" in kw and chosen not in ("B", "C"):
            continue                    # 固定权重没有估计窗口
        res, _ = run(panel, chosen, end, **kw)
        neighbors.append({"params": kw} | _brief(window_report(res, a, end)))
    return {
        "window": [a.isoformat(), end.isoformat()], "results": rows, "chosen": chosen,
        "chosen_title": CANDIDATES[chosen],
        "test_vs_60_40": test, "p_max": P_MAX,
        "test_vs_csi300": paired_test(c, reps["csi300"], n_boot=N_BOOT, seed=SEED),
        "checks": checks, "passed": all(checks.values()), "neighbors": neighbors,
    }


def evaluate_holdout(panel: Panel, dev: dict) -> dict:
    """留出期：只计算选中的方案与两个对照（只运行一次，由调用方保证）。"""
    end = panel.dates[-1]
    rows = {}
    for key in (dev["chosen"], *BENCHMARKS):
        res, _ = run(panel, key, end)
        rows[key] = _brief(window_report(res, HOLDOUT_START, end))
    h, d = rows[dev["chosen"]], dev["results"][dev["chosen"]]
    checks = {
        "sharpe_kept": h["sharpe"] is not None and d["sharpe"] is not None and h["sharpe"] >= 0.5 * d["sharpe"],
        "drawdown_bounded": h["max_drawdown"] is not None and h["max_drawdown"] <= 1.5 * d["max_drawdown"],
    }
    return {"window": [HOLDOUT_START.isoformat(), end.isoformat()], "results": rows, "checks": checks,
            "passed": all(checks.values())}


def evaluate_forward(panel: Panel, dev: dict) -> dict:
    """前瞻期：从 FORWARD_START 前一个交易日的收盘起算；满 FORWARD_DAYS 个交易日时给出评估结论。"""
    days = [d for d in panel.dates if d >= FORWARD_START]
    out: dict = {"start": FORWARD_START.isoformat(), "days": len(days), "need": FORWARD_DAYS}
    if not days:
        return out | {"final": False}
    final = len(days) >= FORWARD_DAYS
    end = days[FORWARD_DAYS - 1] if final else days[-1]
    base = panel.dates[panel.locate(FORWARD_START, "after") - 1]       # 前一个交易日收盘
    rows = {}
    for key in (dev["chosen"], *BENCHMARKS):
        res, _ = run(panel, key, end)
        rows[key] = _brief(window_report(res, base, end))
    out |= {"window": [base.isoformat(), end.isoformat()], "results": rows, "final": final}
    if final:
        c, idx = rows[dev["chosen"]], rows["csi300"]
        out["checks"] = {
            "volatility_half": c["volatility"] is not None and idx["volatility"] is not None
            and c["volatility"] <= 0.5 * idx["volatility"],
            "drawdown_half": c["max_drawdown"] is not None and idx["max_drawdown"] is not None
            and c["max_drawdown"] <= 0.5 * idx["max_drawdown"],
        }
        out["passed"] = all(out["checks"].values())
    return out


# ---------------------------------------------------------------- 加入策略库

PREREG_DOC = "docs/prereg/2026-10-31-cross-asset.md"
LIBRARY_NAME = "资产配置（波动率倒数）"
MEMBER_NAMES = {"510300.SH": "资产配置·沪深300", "511010.SH": "资产配置·国债",
                "518880.SH": "资产配置·黄金", "513100.SH": "资产配置·纳指"}


def add_to_library(s, user_id: int, follow_cash: float | None = CASH):
    """把选中的方案 B 加入策略库：4 个只持有一只 ETF 的成员策略（公共、不设研究模型、不参加每晚优化）
    + 一个波动率倒数配置的组合，都标记为预注册方案、参数冻结；follow_cash 不为 None 时开始实时跟踪。

    会逐个提交成员策略。已经存在（同名的预注册组合）时直接返回，不重复创建。前瞻期评估用本模块的代码计算，与策略库中的记录无关。
    """
    from decimal import Decimal

    from sqlalchemy import select

    from app.core.timeutils import utcnow
    from app.models.quant import QuantPortfolioRow
    from app.quant import costs, live
    from app.quant.service import create_strategy

    for pf in s.scalars(select(QuantPortfolioRow).where(QuantPortfolioRow.user_id == user_id,
                                                        QuantPortfolioRow.name == LIBRARY_NAME,
                                                        QuantPortfolioRow.status == "active")):
        if (pf.settings or {}).get("frozen"):
            return pf
    origin = {"kind": "prereg", "doc": PREREG_DOC, "research": "docs/research/2026-10-31-cross-asset.md",
              "at": utcnow().isoformat(timespec="seconds")}
    members = []
    for symbol, name in MEMBER_NAMES.items():
        with costs.fixed(costs.TradeCosts()):
            st = create_strategy(
                s, name=name, style="asset_alloc", universe_name=UNIVERSE, user_id=user_id, is_public=True,
                spec=hold(symbol).model_dump(),
                description=f"跨资产配置的成员：只持有 {symbol}（{ASSETS[symbol]}），"
                            f"资金比例由组合「{LIBRARY_NAME}」决定。预注册研究的方案，参数冻结。",
                title="预注册方案", reason="跨资产配置预注册研究（2026-10-31）选中的方案")
        st.origin = origin
        s.commit()                      # create_strategy 回测前会回滚未提交的事务，逐个提交
        members.append({"strategy_id": st.id, "weight": 1.0})
    _, alloc = plan("B")
    pf = QuantPortfolioRow(
        user_id=user_id, name=LIBRARY_NAME, members=members,
        description="国债、黄金、A 股、美股 4 只 ETF，按过去 120 个交易日波动率的倒数分配资金（每 20 个交易日更新），"
                    "平均约 73% 国债。低波动、小回撤的稳健配置，收益主要来自国债，今后年化大概率在 3%～5%。"
                    "预注册研究的方案（开发期夏普 1.21、最大回撤 4.7%），成员与配置冻结；前瞻期约 2027 年 11 月评估。",
        settings={"alloc": alloc.to_dict(), "frozen": {"doc": PREREG_DOC, "at": origin["at"]}})
    s.add(pf)
    s.flush()
    if follow_cash is not None:
        live.follow_portfolio(s, pf, user_id, Decimal(str(follow_cash)))
    return pf


# ---------------------------------------------------------------- 命令行


def _path():
    from app.core.config import get_settings

    return get_settings().data_dir / "research" / "xasset.json"


def _load() -> dict:
    p = _path()
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}


def _save(data: dict) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _panel() -> Panel:
    from app.db.session import get_sessionmaker
    from app.quant.data import get_panel

    with get_sessionmaker()() as s:
        return get_panel(s, UNIVERSE, START, date.today())


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO)
    stage = argv[0] if argv else ""
    data = _load()
    if stage == "dev":
        if data.get("dev"):
            print("开发期已经运行过（结果在 xasset.json），不重复运行")
            return 1
        data["dev"] = evaluate_dev(_panel())
    elif stage == "holdout":
        if not (data.get("dev") or {}).get("passed"):
            print("开发期没有运行或没有通过，按预注册不看留出期")
            return 1
        if data.get("holdout"):
            print("留出期只看一次，已经运行过（结果在 xasset.json）")
            return 1
        data["holdout"] = evaluate_holdout(_panel(), data["dev"])
    elif stage == "library":
        if not ((data.get("dev") or {}).get("passed") and (data.get("holdout") or {}).get("passed")):
            print("开发期与留出期都通过后才能加入策略库")
            return 1
        from app.db.session import get_sessionmaker
        from app.models import DEFAULT_USER_ID

        with get_sessionmaker()() as s:
            pf = add_to_library(s, DEFAULT_USER_ID)
            s.commit()
            print(f"组合「{pf.name}」id={pf.id}，跟踪账户 {pf.follow_account_id}")
        return 0
    elif stage == "forward":
        if not data.get("dev"):
            print("开发期还没有运行")
            return 1
        prev = data.get("forward") or {}
        if prev.get("final"):
            print("前瞻期已经评估过（结果在 xasset.json），不重复评估")
            return 1
        data["forward"] = evaluate_forward(_panel(), data["dev"])
    else:
        print(__doc__)
        return 2
    _save(data)
    print(json.dumps(data[stage], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
