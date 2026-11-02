"""三风格稳健加入国债和黄金（预注册：docs/prereg/2026-11-02-defense-sleeve.md，只按预注册的定义实现）。

命令（服务器上运行，结果写入 data/research/defense.json）：
  uv run python -m app.quant.defense dev        开发期：3 个候选 + 对照，挑选与主要检验
  uv run python -m app.quant.defense holdout    留出期：只在开发期通过时运行，只运行一次
  uv run python -m app.quant.defense forward    前瞻期：满 250 个交易日时给出评估结论，之前只显示进度
  uv run python -m app.quant.defense library    开发期与留出期都通过后：作为新组合加入策略库（冻结）并开始实时跟踪

风格部分固定为三风格稳健在预注册时的三个冠军版本（从数据库按版本 id 读取规则），避险部分是只持有一只 ETF 的成员；
各方案按固定的成员资金权重用组合引擎运行，从 2018-01-01 起连续运行一次，各期间的指标从同一条净值曲线上截取。
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import date

from app.quant.backtest import BacktestResult, PortfolioTargeter, run_backtest
from app.quant.evaluate import paired_test, window_report
from app.quant.panel import Panel
from app.quant.spec import StrategySpec, parse_spec
from app.quant.xasset import _brief, hold

logger = logging.getLogger(__name__)

UNIVERSE = "cn_csi300_hist"
STYLE_VERSIONS = {"价值投资": 53, "ETF 轮动": 56, "防御稳健": 104}      # 三风格稳健的冠军版本 id（预注册写定）
BOND, GOLD = "511010.SH", "518880.SH"
START = date(2018, 1, 1)
DEV = (date(2018, 1, 1), date(2024, 12, 31))
HOLDOUT_START = date(2025, 1, 1)
FORWARD_START = date(2026, 11, 3)       # 预注册提交（2026-11-02 20:44）后的第一个交易日
FORWARD_DAYS = 250
CASH = 1_000_000
SEED = 20261102
N_BOOT = 10_000
P_MAX = 0.05 / 3                        # 3 个候选
NEIGHBOR_SHIFT = 0.10                   # 邻近比例：避险部分整体 ±10 个百分点

# 方案：(风格部分, 国债, 黄金) 的资金比例
CANDIDATES = {"A": (0.80, 0.00, 0.20), "B": (0.70, 0.15, 0.15), "C": (0.50, 0.25, 0.25)}
BASE = "base"
TITLES = {"A": "风格 80% + 黄金 20%", "B": "风格 70% + 国债 15% + 黄金 15%",
          "C": "风格 50% + 国债 25% + 黄金 25%", BASE: "三风格稳健原样"}


def mix(key: str, shift: float = 0.0) -> tuple[float, float, float]:
    """方案的资金比例；shift 把避险部分整体加减若干个百分点（国债与黄金的比例不变）。"""
    style, bond, gold = (1.0, 0.0, 0.0) if key == BASE else CANDIDATES[key]
    defense = bond + gold
    if shift and defense:
        new = min(max(defense + shift, 0.0), 1.0)
        bond, gold, style = bond / defense * new, gold / defense * new, 1.0 - new
    return style, bond, gold


def plan(styles: list[StrategySpec], key: str, shift: float = 0.0) -> list[tuple[StrategySpec, float]]:
    """方案的成员与固定资金权重：风格部分由三个成员平分，避险部分各是一只 ETF。"""
    style, bond, gold = mix(key, shift)
    members = [(sp, style / len(styles)) for sp in styles]
    members += [(hold(sym), w) for sym, w in ((BOND, bond), (GOLD, gold)) if w > 0]
    return members


def run(panel: Panel, styles: list[StrategySpec], key: str, end: date, shift: float = 0.0) -> BacktestResult:
    """连续运行一个方案。成本固定为系统默认值（预注册写定），不受用户在页面上改过的成本设置影响。"""
    from app.quant import costs

    targeter = PortfolioTargeter(plan(styles, key, shift))
    with costs.fixed(costs.TradeCosts()):
        return run_backtest(None, panel, START, end, CASH, targeter=targeter)


def evaluate_dev(panel: Panel, styles: list[StrategySpec]) -> dict:
    """开发期：计算 3 个候选与对照，按开发期夏普挑选，与对照做主要检验。"""
    a, b = DEV
    end = min(b, panel.dates[-1])
    reps: dict[str, dict] = {}
    rows: dict[str, dict] = {}
    for key in (*CANDIDATES, BASE):
        reps[key] = window_report(run(panel, styles, key, end), a, end, monthly=True)
        rows[key] = _brief(reps[key]) | {"title": TITLES[key], "mix": mix(key)}
        logger.info("开发期 %s：夏普 %s", key, reps[key].get("sharpe"))
    chosen = max(CANDIDATES, key=lambda k: reps[k].get("sharpe") if reps[k].get("sharpe") is not None else -9)
    c, base = reps[chosen], reps[BASE]
    test = paired_test(c, base, n_boot=N_BOOT, seed=SEED)
    checks = {
        "sharpe_above_base": c.get("sharpe") is not None and base.get("sharpe") is not None
        and c["sharpe"] > base["sharpe"],
        "significant": test is not None and test["p"] <= P_MAX,
        "drawdown_below_base": c.get("max_drawdown") is not None and base.get("max_drawdown") is not None
        and c["max_drawdown"] < base["max_drawdown"],
    }
    neighbors = [{"shift": sh, "mix": mix(chosen, sh)}
                 | _brief(window_report(run(panel, styles, chosen, end, sh), a, end))
                 for sh in (-NEIGHBOR_SHIFT, NEIGHBOR_SHIFT)]
    return {
        "window": [a.isoformat(), end.isoformat()], "versions": STYLE_VERSIONS, "results": rows,
        "chosen": chosen, "chosen_title": TITLES[chosen], "test_vs_base": test, "p_max": P_MAX,
        "checks": checks, "passed": all(checks.values()), "neighbors": neighbors,
    }


def evaluate_holdout(panel: Panel, styles: list[StrategySpec], dev: dict) -> dict:
    """留出期：只计算选中的方案与对照（只运行一次，由调用方保证）。"""
    end = panel.dates[-1]
    rows = {key: _brief(window_report(run(panel, styles, key, end), HOLDOUT_START, end))
            for key in (dev["chosen"], BASE)}
    h, d = rows[dev["chosen"]], dev["results"][dev["chosen"]]
    checks = {
        "sharpe_kept": h["sharpe"] is not None and d["sharpe"] is not None and h["sharpe"] >= 0.5 * d["sharpe"],
        "drawdown_bounded": h["max_drawdown"] is not None and h["max_drawdown"] <= 1.5 * d["max_drawdown"],
    }
    return {"window": [HOLDOUT_START.isoformat(), end.isoformat()], "results": rows, "checks": checks,
            "passed": all(checks.values())}


def evaluate_forward(panel: Panel, styles: list[StrategySpec], dev: dict) -> dict:
    """前瞻期：从 FORWARD_START 前一个交易日的收盘起算；满 FORWARD_DAYS 个交易日时给出评估结论。"""
    days = [d for d in panel.dates if d >= FORWARD_START]
    out: dict = {"start": FORWARD_START.isoformat(), "days": len(days), "need": FORWARD_DAYS}
    if not days:
        return out | {"final": False}
    final = len(days) >= FORWARD_DAYS
    end = days[FORWARD_DAYS - 1] if final else days[-1]
    base_day = panel.dates[panel.locate(FORWARD_START, "after") - 1]      # 前一个交易日收盘
    rows = {key: _brief(window_report(run(panel, styles, key, end), base_day, end))
            for key in (dev["chosen"], BASE)}
    out |= {"window": [base_day.isoformat(), end.isoformat()], "results": rows, "final": final}
    if final:
        c, b = rows[dev["chosen"]], rows[BASE]
        out["checks"] = {
            "volatility_lower": c["volatility"] is not None and b["volatility"] is not None
            and c["volatility"] < b["volatility"],
            "drawdown_not_worse": c["max_drawdown"] is not None and b["max_drawdown"] is not None
            and c["max_drawdown"] <= b["max_drawdown"],
        }
        out["passed"] = all(out["checks"].values())
    return out


# ---------------------------------------------------------------- 加入策略库

PREREG_DOC = "docs/prereg/2026-11-02-defense-sleeve.md"
RESEARCH_DOC = "docs/research/2026-11-02-defense-sleeve.md"
LIBRARY_NAME = "三风格稳健＋避险"


def add_to_library(s, user_id: int, chosen: str, follow_cash: float | None = CASH):
    """把选中的方案作为新组合加入策略库：三个风格冠军版本的冻结副本 + 国债、黄金成员（公共、不设研究模型、
    不参加每晚优化，都标记为预注册方案）+ 固定权重的组合；follow_cash 不为 None 时开始实时跟踪。

    不改动现有的三风格稳健（它的跟踪账户绑定了 AI 交易复核的评估）。会逐个提交成员策略。
    已经存在（同名的预注册组合）时直接返回，不重复创建。
    """
    from decimal import Decimal

    from sqlalchemy import select

    from app.core.timeutils import utcnow
    from app.models.quant import QuantPortfolioRow, QuantStrategyRow, QuantVersionRow
    from app.quant import costs, live
    from app.quant.service import create_strategy

    for pf in s.scalars(select(QuantPortfolioRow).where(QuantPortfolioRow.user_id == user_id,
                                                        QuantPortfolioRow.name == LIBRARY_NAME,
                                                        QuantPortfolioRow.status == "active")):
        if (pf.settings or {}).get("frozen"):
            return pf
    origin = {"kind": "prereg", "doc": PREREG_DOC, "research": RESEARCH_DOC,
              "at": utcnow().isoformat(timespec="seconds")}
    style, bond, gold = mix(chosen)
    entries = []
    for name, vid in STYLE_VERSIONS.items():
        v = s.get(QuantVersionRow, vid)
        src = s.get(QuantStrategyRow, v.strategy_id)
        entries.append((f"避险组合·{name}", src.style, v.spec, style / len(STYLE_VERSIONS),
                        f"「{LIBRARY_NAME}」的风格成员：{name} v{v.number} 的冻结副本。"))
    for sym, label, w in ((BOND, "国债", bond), (GOLD, "黄金", gold)):
        if w > 0:
            entries.append((f"避险组合·{label}", "asset_alloc", hold(sym).model_dump(), w,
                            f"「{LIBRARY_NAME}」的避险成员：只持有 {sym}（{label}）。"))
    members = []
    for name, style_key, spec, w, desc in entries:
        with costs.fixed(costs.TradeCosts()):
            st = create_strategy(s, name=name, style=style_key, universe_name=UNIVERSE, user_id=user_id,
                                 is_public=True, spec=spec, description=desc + "预注册研究的方案，参数冻结。",
                                 title="预注册方案",
                                 reason="三风格稳健加入避险资产的预注册研究（2026-11-02）选中的方案")
        st.origin = origin
        s.commit()                      # create_strategy 回测前会回滚未提交的事务，逐个提交
        members.append({"strategy_id": st.id, "weight": round(w, 4)})
    pf = QuantPortfolioRow(
        user_id=user_id, name=LIBRARY_NAME, members=members,
        description=f"三风格稳健（价值投资、ETF 轮动、防御稳健）加入国债和黄金：{TITLES[chosen]}，固定权重。"
                    "预注册研究的方案，成员与比例冻结；前瞻期约 2027 年 11 月评估。",
        settings={"frozen": {"doc": PREREG_DOC, "at": origin["at"]}})
    s.add(pf)
    s.flush()
    if follow_cash is not None:
        live.follow_portfolio(s, pf, user_id, Decimal(str(follow_cash)))
    return pf


# ---------------------------------------------------------------- 命令行


def _path():
    from app.core.config import get_settings

    return get_settings().data_dir / "research" / "defense.json"


def _load() -> dict:
    p = _path()
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}


def _save(data: dict) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _inputs() -> tuple[Panel, list[StrategySpec]]:
    from app.db.session import get_sessionmaker
    from app.models.quant import QuantVersionRow
    from app.quant.data import get_panel

    with get_sessionmaker()() as s:
        styles = [parse_spec(s.get(QuantVersionRow, vid).spec) for vid in STYLE_VERSIONS.values()]
        return get_panel(s, UNIVERSE, START, date.today()), styles


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO)
    stage = argv[0] if argv else ""
    data = _load()
    if stage == "dev":
        if data.get("dev"):
            print("开发期已经运行过（结果在 defense.json），不重复运行")
            return 1
        data["dev"] = evaluate_dev(*_inputs())
    elif stage == "holdout":
        if not (data.get("dev") or {}).get("passed"):
            print("开发期没有运行或没有通过，按预注册不看留出期")
            return 1
        if data.get("holdout"):
            print("留出期只看一次，已经运行过（结果在 defense.json）")
            return 1
        data["holdout"] = evaluate_holdout(*_inputs(), data["dev"])
    elif stage == "library":
        if not ((data.get("dev") or {}).get("passed") and (data.get("holdout") or {}).get("passed")):
            print("开发期与留出期都通过后才能加入策略库")
            return 1
        from app.db.session import get_sessionmaker
        from app.models import DEFAULT_USER_ID

        with get_sessionmaker()() as s:
            pf = add_to_library(s, DEFAULT_USER_ID, data["dev"]["chosen"])
            s.commit()
            print(f"组合「{pf.name}」id={pf.id}，跟踪账户 {pf.follow_account_id}")
        return 0
    elif stage == "forward":
        if not data.get("dev"):
            print("开发期还没有运行")
            return 1
        if (data.get("forward") or {}).get("final"):
            print("前瞻期已经评估过（结果在 defense.json），不重复评估")
            return 1
        data["forward"] = evaluate_forward(*_inputs(), data["dev"])
    else:
        print(__doc__)
        return 2
    _save(data)
    print(json.dumps(data[stage], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
