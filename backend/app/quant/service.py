"""策略工坊业务逻辑：设置、版本评估、升级、初始策略库（PRD 3.8）。"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, fields
from datetime import date

import numpy as np
import yaml
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.timeutils import utcnow
from app.models import DEFAULT_USER_ID
from app.models.quant import QuantStrategyRow, QuantVersionRow
from app.quant.data import get_panel, universe_members
from app.quant.evaluate import Gates, Windows, curves, diagnostics, evaluate, robustness
from app.quant.panel import Panel
from app.quant.spec import StrategySpec, describe, parse_spec

logger = logging.getLogger(__name__)

MAX_TRIALS = 500         # 新建前试算次数的记录上限
DEFAULT_UNIVERSE = "cn_csi300_hist"      # 全时点：含历史上被剔除和已退市的成分股（2026-10-09 起）
# 快速体验：上证50（当前成分）+ 核心 ETF，几分钟准备好，用于新安装先试用；有幸存者偏差，不能通过「可实盘检查」
QUICK_UNIVERSE = "cn_quick"


class LabError(Exception):
    pass


@dataclass
class LabSettings:
    train_start: date = date(2018, 1, 1)
    train_end: date = date(2022, 12, 31)
    valid_start: date = date(2023, 1, 1)
    valid_end: date = date(2024, 12, 31)
    holdout_start: date = date(2025, 1, 1)
    initial_cash: float = 1_000_000
    max_drawdown: float = 0.25
    robust_keep: float = 0.7
    max_turnover: float = 20.0
    candidates: int = 3
    rounds: int = 5
    fold_win_rate: float = 0.5         # 开发期逐年对比中至少跑赢冠军的年份比例
    p_max: float = 0.05                # 月度配对检验的显著性门槛（同轮多个候选时按候选数收紧；2026-10-03 由 0.2 收紧）
    min_holdings: int = 20             # 股票策略的最少持仓数（分散个股运气；只选 ETF 的策略不受限）
    # 显著性门槛按「当前冠军以来累计检验过的候选数」收紧（v1.9）：同一个冠军被挑战的次数越多，
    # 偶然显著的机会越大；关闭时只按同一轮的候选数收紧
    cumulative_tests: bool = True
    # 程序调参：v1.6 实测只在训练区间调参会明显过拟合（训练-验证夏普差中位数 0.72 vs 0.09），默认关闭
    tune: bool = False
    tune_budget: int = 30              # 每个候选调参的最多回测次数

    @classmethod
    def from_dict(cls, d: dict | None) -> LabSettings:
        out = cls()
        for f in fields(cls):
            if d and d.get(f.name) is not None:
                v = d[f.name]
                if isinstance(getattr(out, f.name), date):
                    v = date.fromisoformat(str(v))
                elif isinstance(getattr(out, f.name), bool):
                    v = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes")
                elif isinstance(getattr(out, f.name), int) and not isinstance(getattr(out, f.name), bool):
                    v = int(v)
                else:
                    v = float(v)
                setattr(out, f.name, v)
        out.validate()
        return out

    def validate(self) -> None:
        if not self.train_start < self.train_end < self.valid_start <= self.valid_end < self.holdout_start:
            raise LabError("区间顺序必须是：训练开始 < 训练结束 < 验证开始 ≤ 验证结束 < 留出开始")
        if (self.train_end - self.train_start).days < 365:
            raise LabError("训练区间至少一年")
        if (self.valid_end - self.valid_start).days < 180:
            raise LabError("验证区间至少半年")
        if not 1 <= self.candidates <= 5 or not 1 <= self.rounds <= 50:
            raise LabError("每轮候选数 1～5，每次优化轮数 1～50")
        if not 0 <= self.fold_win_rate <= 1 or not 0 < self.p_max <= 1 or not 5 <= self.tune_budget <= 200:
            raise LabError("逐年胜率 0～1，显著性门槛 0～1，调参回测次数 5～200")
        if not 0 <= self.min_holdings <= 50:
            raise LabError("最少持仓数 0～50")

    def to_dict(self) -> dict:
        return {k: (v.isoformat() if isinstance(v, date) else v) for k, v in asdict(self).items()}

    def gates(self) -> Gates:
        return Gates(max_drawdown=self.max_drawdown, robust_keep=self.robust_keep, max_turnover=self.max_turnover,
                     fold_win_rate=self.fold_win_rate, p_max=self.p_max, min_holdings=self.min_holdings)

    def windows(self, last: date) -> Windows:
        return Windows(train=(self.train_start, self.train_end), valid=(self.valid_start, self.valid_end),
                       holdout=(self.holdout_start, last) if last > self.holdout_start else None)


def settings_of(st: QuantStrategyRow) -> LabSettings:
    return LabSettings.from_dict(st.settings)


def panel_for(s: Session, st: QuantStrategyRow) -> Panel:
    cfg = settings_of(st)
    return get_panel(s, st.universe_name, cfg.train_start, date.today())


# ---------------------------------------------------------------- 评估
# 注意：回测耗时较长，调用方应先计算、再在短事务中写入，避免长时间持有 SQLite 写锁


def evaluate_spec(s: Session, st: QuantStrategyRow, spec: StrategySpec, with_robustness: bool = True,
                  panel: Panel | None = None) -> dict:
    """回测并统计各区间；返回可直接写入版本行的字段。"""
    return evaluate_with(panel or panel_for(s, st), settings_of(st), spec, with_robustness)


def evaluate_with(panel: Panel, cfg: LabSettings, spec: StrategySpec, with_robustness: bool = True) -> dict:
    w = cfg.windows(panel.dates[-1])
    res, rep = evaluate(spec, panel, w, cfg.initial_cash)
    # 诊断（回撤区间、盈亏最多的标的）用开发期单独回测，与升级检验的区间一致
    rep["dev"]["diagnostics"] = diagnostics(_dev_only(spec, panel, w, cfg), panel)
    out = {"train": rep["train"], "valid": rep["valid"], "dev": rep["dev"], "holdout": rep.get("holdout") or {},
           "curves": curves(res) | {"train_end": cfg.train_end.isoformat(), "valid_end": cfg.valid_end.isoformat()},
           "score": rep["dev"].get("sharpe"), "robustness": {}}
    if with_robustness:
        out["robustness"] = robustness(spec, panel, w, rep["dev"].get("sharpe"))
    return out


def _dev_only(spec, panel, w, cfg):
    from app.quant.backtest import run_backtest

    return run_backtest(spec, panel, w.dev[0], w.dev[1], cfg.initial_cash)


PREVIEW_KEYS = ("annualized_return", "cumulative_return", "sharpe", "max_drawdown", "calmar", "excess_return",
                "volatility", "turnover", "cost_ratio", "avg_holdings", "win_rate", "trades")


def preview(s: Session, spec: dict, universe_name: str = DEFAULT_UNIVERSE, settings: dict | None = None) -> dict:
    """试算：只回测开发期、不保存版本，也不返回留出期结果。

    编辑规则时可以反复试算；如果每改一次都看留出期，改到留出期好看为止，留出期就失去了检验意义。
    """
    from app.quant.backtest import run_backtest
    from app.quant.evaluate import window_report

    cfg = LabSettings.from_dict(settings)
    parsed = parse_spec(spec)
    try:
        panel = get_panel(s, universe_name, cfg.train_start, date.today())
    except ValueError as e:
        raise LabError(str(e)) from None
    w = cfg.windows(panel.dates[-1])
    res = run_backtest(parsed, panel, w.dev[0], w.dev[1], cfg.initial_cash)
    rep = window_report(res, *w.dev)
    warnings = []
    n = holdings_of(parsed)
    if n is not None and n < cfg.min_holdings:
        warnings.append(f"股票策略至少持有 {cfg.min_holdings} 只（现为 {n} 只），否则不能升级为冠军")
    if (rep.get("max_drawdown") or 0) > cfg.max_drawdown:
        warnings.append(f"开发期最大回撤 {rep['max_drawdown']:.1%} 超过上限 {cfg.max_drawdown:.0%}")
    if (rep.get("turnover") or 0) > cfg.max_turnover:
        warnings.append(f"年化换手 {rep['turnover']:.1f} 倍超过上限 {cfg.max_turnover:g} 倍")
    return {"window": {"start": w.dev[0].isoformat(), "end": w.dev[1].isoformat()},
            "dev": {k: rep.get(k) for k in PREVIEW_KEYS} | {"yearly": rep.get("yearly")},
            "curves": curves(res), "rules": describe(parsed.model_dump()), "spec": parsed.model_dump(),
            "warnings": warnings}


def holdings_of(spec: StrategySpec) -> int | None:
    """股票策略的持仓数（用于最少持仓门槛）；只选 ETF 的策略返回 None。"""
    return spec.selection.top_n if "stock" in spec.universe.types else None


def trial_scores(s: Session, st: QuantStrategyRow) -> list[float]:
    """该策略至今所有尝试（版本）的得分；另存的策略还包括来源策略在另存时已有的尝试（沿来源逐级追溯）。"""
    out: list[float] = []
    cur, upto, seen = st, None, set()
    while cur is not None and cur.id not in seen:
        seen.add(cur.id)
        q = select(QuantVersionRow.score).where(QuantVersionRow.strategy_id == cur.id)
        if upto is not None:
            q = q.where(QuantVersionRow.id <= upto)
        out += [x for x in s.scalars(q) if x is not None]
        origin = cur.origin or {}
        out += [float(x) for x in origin.get("trial_scores") or [] if x is not None]     # 新建前的试算
        upto = origin.get("max_version_id")
        cur = s.get(QuantStrategyRow, origin["strategy_id"]) if origin.get("strategy_id") else None
    return out


def dsr_of(s: Session, st: QuantStrategyRow, v: QuantVersionRow) -> float | None:
    """按该策略至今的尝试次数折算的夏普（DSR）；另存的策略连同来源的尝试一起算。"""
    from app.quant.evaluate import deflated_sharpe

    sharpes = trial_scores(s, st)
    if not v.dev:
        return None
    var = float(np.var(sharpes, ddof=1)) if len(sharpes) > 1 else None
    return deflated_sharpe(v.dev, len(sharpes), var)


def next_number(s: Session, strategy_id: int) -> int:
    return (s.scalar(select(func.max(QuantVersionRow.number)).where(QuantVersionRow.strategy_id == strategy_id))
            or 0) + 1


def add_version(s: Session, st: QuantStrategyRow, spec: StrategySpec, *, source: str, result: dict,
                title: str = "", rationale: str = "", parent_id: int | None = None,
                run_id: int | None = None) -> QuantVersionRow:
    v = QuantVersionRow(strategy_id=st.id, number=next_number(s, st.id), parent_id=parent_id, run_id=run_id,
                        source=source, title=title[:128], rationale=rationale, spec=spec.model_dump(),
                        status="candidate", **result)
    s.add(v)
    s.flush()
    return v


def promote(s: Session, st: QuantStrategyRow, v: QuantVersionRow, reason: str) -> None:
    if st.champion_id and st.champion_id != v.id:
        old = s.get(QuantVersionRow, st.champion_id)
        if old is not None:
            old.status = "retired"
    v.status, v.decision, v.promoted_at = "champion", reason, utcnow()
    st.champion_id = v.id


def reevaluate(s: Session, st: QuantStrategyRow) -> int:
    """数据修正或更新后重新回测该策略的所有版本（保留状态与结论），返回版本数。"""
    panel, cfg = panel_for(s, st), settings_of(st)
    versions = list(s.scalars(select(QuantVersionRow).where(QuantVersionRow.strategy_id == st.id)))
    results = [(v, evaluate_with(panel, cfg, parse_spec(v.spec), with_robustness=bool(v.robustness)))
               for v in versions]
    for v, r in results:
        if (v.train or {}).get("tuning"):
            r["train"]["tuning"] = v.train["tuning"]        # 调参记录不随重新回测丢失
        if (v.dev or {}).get("vs_champion"):
            r["dev"]["vs_champion"] = v.dev["vs_champion"]   # 升级检验当时与冠军的对比
        for k, val in r.items():
            setattr(v, k, val)
    return len(results)


def champion_reports(s: Session, st: QuantStrategyRow) -> dict | None:
    if not st.champion_id:
        return None
    v = s.get(QuantVersionRow, st.champion_id)
    return {"train": v.train, "valid": v.valid, "dev": v.dev, "robustness": v.robustness} if v else None


# ---------------------------------------------------------------- 创建与初始策略库


def load_seeds() -> list[dict]:
    path = get_settings().config_dir / "strategies.yaml"
    if not path.exists():
        return []
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("strategies") or []


def create_strategy(s: Session, *, name: str, style: str, description: str, spec: dict,
                    universe_name: str = DEFAULT_UNIVERSE, provider_id: int | None = None, model: str | None = None,
                    settings: dict | None = None, auto_optimize: bool = False, title: str = "初始策略",
                    reason: str = "初始版本", user_id: int = DEFAULT_USER_ID,
                    is_public: bool = False, trial_scores: list[float] | None = None) -> QuantStrategyRow:
    """新建策略并回测初始版本。

    trial_scores：新建前在对话框里试算过的规则的开发期夏普（每试算一次一个）。试算只看开发期，但反复改到开发期好看
    本身就是在开发期上做选择，所以计入测试次数：初始版本的升级检验门槛按次数收紧，DSR 也把它们算作尝试。
    """
    from app.quant import costs

    cfg = LabSettings.from_dict(settings)
    parsed = parse_spec(spec)
    try:
        _, bench, market = universe_members(universe_name)
    except ValueError as e:
        raise LabError(str(e)) from None
    with costs.use(user_id):            # 按策略所有者的交易成本回测
        result = evaluate_with(get_panel(s, universe_name, cfg.train_start, date.today()), cfg, parsed)
    s.rollback()        # 结束读事务，下面的写入在短事务中完成
    st = QuantStrategyRow(name=name, style=style, description=description.strip(), market=market,
                          universe_name=universe_name, benchmark=bench.symbol, provider_id=provider_id, model=model,
                          settings=cfg.to_dict(), auto_optimize=auto_optimize, user_id=user_id, is_public=is_public)
    s.add(st)
    s.flush()
    v = add_version(s, st, parsed, source="initial", title=title, result=result)
    promote(s, st, v, reason)
    trials = [float(x) for x in (trial_scores or []) if x is not None][:MAX_TRIALS]
    if trial_scores:
        st.origin = {"kind": "new", "prior_tests": len(trial_scores), "trial_scores": trials,
                     "base_version_id": v.id, "at": utcnow().isoformat()}
    return st


def prior_tests(s: Session, st: QuantStrategyRow, version_id: int | None) -> int:
    """已经与该版本（作为冠军）比较过的候选数；另存的策略在初始版本上还要加上来源中已比较过的次数。"""
    if version_id is None:
        return 0
    n = len(s.scalars(select(QuantVersionRow.id).where(
        QuantVersionRow.strategy_id == st.id, QuantVersionRow.parent_id == version_id,
        QuantVersionRow.id != version_id)).all())
    origin = st.origin or {}
    if origin.get("base_version_id") == version_id:
        n += int(origin.get("prior_tests") or 0)
    return n


def fork_strategy(s: Session, src: QuantStrategyRow, v: QuantVersionRow, *, name: str,
                  description: str | None = None, copy_notes: bool = True, provider_id: int | None = None,
                  model: str | None = None, auto_optimize: bool = False,
                  user_id: int = DEFAULT_USER_ID) -> QuantStrategyRow:
    """另存为新策略：以来源策略的某个版本为初始版本，建立独立的策略（版本历史、优化、跟踪都各自独立）。

    - 新策略属于 user_id（可以另存别人的公共策略），不公开；按新所有者的交易成本重新回测。
    - 检验区间、本金与门槛沿用来源策略；半时点股票池（只保留作对比）改用对应的全时点股票池，并重新回测。
    - 为免「另存一次就把检验次数清零」：新策略的升级检验继承来源中已与该版本比较过的候选数，
      按尝试次数折算的夏普（DSR）也把来源在另存时已有的尝试算进去。
    """
    from app.quant.pit import HIST_OF

    if v.strategy_id != src.id:
        raise LabError("版本不属于该策略")
    origin = {
        "strategy_id": src.id, "strategy_name": src.name, "version_id": v.id, "version_number": v.number,
        "version_status": v.status, "universe_name": src.universe_name,
        "prior_tests": prior_tests(s, src, v.id),
        "max_version_id": s.scalar(select(func.max(QuantVersionRow.id)).where(
            QuantVersionRow.strategy_id == src.id)),
        "at": utcnow().isoformat(),
    }
    notes = list(src.research_notes or []) if copy_notes else []
    spec, universe = dict(v.spec), HIST_OF.get(src.universe_name, src.universe_name)
    st = create_strategy(
        s, name=name, style=src.style, description=src.description if description is None else description,
        spec=spec, universe_name=universe, provider_id=provider_id, model=model, settings=dict(src.settings or {}),
        auto_optimize=auto_optimize, user_id=user_id,
        title=f"另存自「{origin['strategy_name']}」v{origin['version_number']}",
        reason=f"另存自「{origin['strategy_name']}」v{origin['version_number']}")
    st.origin = origin | {"base_version_id": st.champion_id}
    st.research_notes = notes
    return st


def seed_library(s: Session, provider_id: int | None = None, model: str | None = None,
                 styles: list[str] | None = None, user_id: int = DEFAULT_USER_ID) -> list[QuantStrategyRow]:
    """按 config/strategies.yaml 创建该用户尚未创建的风格策略。管理员创建的为公共策略（所有用户可查看、另存）。"""
    existing = {r.style for r in s.scalars(select(QuantStrategyRow).where(
        QuantStrategyRow.status == "active", QuantStrategyRow.user_id == user_id))}
    created = []
    for seed in load_seeds():
        if seed["style"] in existing or (styles and seed["style"] not in styles):
            continue
        created.append(create_strategy(s, name=seed["title"], style=seed["style"], description=seed["description"],
                                       spec=seed["spec"], provider_id=provider_id, model=model, user_id=user_id,
                                       is_public=user_id == DEFAULT_USER_ID))
        s.commit()
    return created


# ---------------------------------------------------------------- 展示


def version_brief(v: QuantVersionRow) -> dict:
    def pick(rep: dict) -> dict:
        return {k: rep.get(k) for k in ("annualized_return", "cumulative_return", "sharpe", "calmar", "max_drawdown",
                                        "excess_return", "turnover", "days")}

    return {"id": v.id, "number": v.number, "status": v.status, "source": v.source, "title": v.title,
            "dev": pick(v.dev or {}) | {"vs_champion": (v.dev or {}).get("vs_champion")},
            "rationale": v.rationale, "decision": v.decision, "score": v.score, "parent_id": v.parent_id,
            "run_id": v.run_id, "created_at": v.created_at.isoformat(),
            "promoted_at": v.promoted_at.isoformat() if v.promoted_at else None,
            "train": pick(v.train), "valid": pick(v.valid), "holdout": pick(v.holdout),
            "robustness": v.robustness}


def version_detail(v: QuantVersionRow) -> dict:
    try:
        rules = describe(v.spec)
    except ValueError as e:
        rules = [f"规格无法解析：{e}"]
    return version_brief(v) | {"spec": v.spec, "rules": rules, "train_full": v.train, "valid_full": v.valid,
                               "dev_full": {k: x for k, x in (v.dev or {}).items() if k != "monthly"},
                               "holdout_full": v.holdout, "curves": v.curves}


def spec_text(spec: dict) -> str:
    return json.dumps(spec, ensure_ascii=False, separators=(",", ":"))


def research_quality(s: Session, st: QuantStrategyRow) -> dict:
    """研究过程是否有效（v1.9）：只用于评估「AI 优化有没有用」，不参与任何挑选。

    - 回测过拟合概率 PBO：在全部版本的开发期月度收益上做 CSCV（见 evaluate.pbo_cscv），≈0.5 说明在拟合噪音；
    - 开发期夏普与留出期夏普的秩相关：开发期越好的版本留出期是否也越好（> 0 才说明改进能延续）；
    - 滚动选择检验（walk-forward）：逐年只用之前的数据挑版本，看下一年是否比初始版本 / 平均水平好。
    """
    import pandas as pd

    from app.quant.evaluate import pbo_cscv, walk_forward

    versions = [v for v in s.scalars(select(QuantVersionRow).where(QuantVersionRow.strategy_id == st.id))
                if v.dev]
    series = {v.number: dict((m[0], m[1]) for m in (v.dev or {}).get("monthly") or []) for v in versions}
    df = pd.DataFrame({k: v for k, v in series.items() if v}).dropna()
    pairs = [((v.dev or {}).get("sharpe"), (v.holdout or {}).get("sharpe")) for v in versions]
    pairs = [(a, b) for a, b in pairs if a is not None and b is not None]
    rank_corr = None
    if len(pairs) >= 5:
        x = pd.DataFrame(pairs, columns=["dev", "holdout"])
        rank_corr = round(float(x["dev"].rank().corr(x["holdout"].rank())), 3)
    tests = prior_tests(s, st, st.champion_id)
    full = {v.number: {**dict((m[0], m[1]) for m in (v.dev or {}).get("monthly") or []),
                       **dict((m[0], m[1]) for m in (v.holdout or {}).get("monthly") or [])} for v in versions}
    seed = min((v.number for v in versions), default=None)
    wf = walk_forward({k: x for k, x in full.items() if x}, seed,
                      holdout_year=settings_of(st).holdout_start.year) if seed is not None else None
    return {"versions": len(versions), "months": int(df.shape[0]),
            "pbo": pbo_cscv(df.to_numpy()) if df.shape[1] >= 4 else None,
            "rank_corr": rank_corr, "rank_n": len(pairs), "tests_since_champion": tests, "walk_forward": wf}
