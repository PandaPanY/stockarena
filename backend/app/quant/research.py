"""AI 研究循环（PRD 3.8）：AI 阅读冠军的训练区间报告与最近的尝试 → 提出候选规格 → 程序回测检验 → 达标则升级。"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field

import yaml
from sqlalchemy import select

from app.agent.parse import _THINK_RE, DecisionParseError, extract_json
from app.agent.runner import run_llm_json
from app.arena.strategy import llm_config_for
from app.core.config import get_settings
from app.core.timeutils import utcnow
from app.db.session import get_sessionmaker
from app.llm import calc_cost, create_client
from app.models import LLMProviderRow
from app.models.quant import QuantStrategyRow, QuantVersionRow, ResearchRunRow
from app.quant import factor_research, factors
from app.quant.data import universe_members
from app.quant.evaluate import check_gates
from app.quant.service import (
    LabError,
    add_version,
    champion_reports,
    evaluate_spec,
    evaluate_with,
    holdings_of,
    panel_for,
    promote,
    settings_of,
    spec_text,
)
from app.quant.service import prior_tests as service_prior_tests
from app.quant.spec import describe, parse_spec
from app.quant.tune import tune

logger = logging.getLogger(__name__)

TIMEOUT_S = 900
NOTES_KEEP = 40          # 研究笔记最多保留的条数
NOTES_IN_PROMPT = 20     # 放进提示词的最近条数
STATUS_TEXT = {"champion": "升级（当前冠军）", "retired": "曾为冠军", "rejected": "淘汰", "candidate": "候选"}
HISTORY = 8
# 一次只运行一个研究任务：回测占用 CPU，并避免 SQLite 写冲突
_lock = asyncio.Lock()
_cancel: set[int] = set()

def load_prompt() -> dict:
    """研究员提示放在 config/research/default.yaml，便于调整。"""
    path = get_settings().config_dir / "research" / "default.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _round(x: float | None) -> float | None:
    return None if x is None else round(x, 4)


def _pct(x) -> str:
    return "-" if x is None else f"{x * 100:.1f}%"


def _num(x, n: int = 2) -> str:
    return "-" if x is None else f"{x:.{n}f}"


def _factor_doc() -> str:
    lines = []
    for f in factors.catalog():
        ps = "；".join(f"{k} 默认 {p['default']:g}（{p['min']:g}～{p['max']:g}）" for k, p in f["params"].items())
        tags = ("［财报］" if f["fundamental"] else "") + ("［可择时］" if f["timing"] else "")
        lines.append(f"- {f['name']}（{f['title']}）{tags}：{f['description']}" + (f"。参数：{ps}" if ps else ""))
    return "\n".join(lines)


def _report_text(rep: dict) -> str:
    lines = [f"区间 {rep.get('start')} ～ {rep.get('end')}（{rep.get('days')} 个交易日）",
             f"累计收益 {_pct(rep.get('cumulative_return'))}，年化 {_pct(rep.get('annualized_return'))}，"
             f"基准 {_pct(rep.get('benchmark_return'))}，超额 {_pct(rep.get('excess_return'))}",
             f"年化波动 {_pct(rep.get('volatility'))}，夏普 {_num(rep.get('sharpe'))}，"
             f"最大回撤 {_pct(rep.get('max_drawdown'))}，卡玛 {_num(rep.get('calmar'))}",
             f"年化换手 {_num(rep.get('turnover'), 1)} 倍，年化成本 {_pct(rep.get('cost_ratio'))}，"
             f"成交 {rep.get('trades')} 笔，清仓胜率 {_pct(rep.get('win_rate'))}，"
             f"平均持仓 {rep.get('avg_holdings')} 只，平均仓位 {_pct(rep.get('avg_exposure'))}"]
    years = rep.get("yearly") or []
    if years:
        lines.append("分年：" + "；".join(f"{y['year']} {_pct(y['return'])}（基准 {_pct(y['benchmark'])}，"
                                          f"回撤 {_pct(y['max_drawdown'])}）" for y in years))
    dg = rep.get("diagnostics") or {}
    if dg.get("drawdowns"):
        lines.append("最深回撤：" + "；".join(f"{d['start']} → {d['trough']} 深 {_pct(d['depth'])}"
                                         f"（{'恢复于 ' + d['recovered'] if d['recovered'] else '未恢复'}）"
                                         for d in dg["drawdowns"]))
    if dg.get("best"):
        lines.append("盈利最多：" + "、".join(f"{x['name']} {x['pnl'] / 1e4:+.1f} 万" for x in dg["best"]))
        lines.append("亏损最多：" + "、".join(f"{x['name']} {x['pnl'] / 1e4:+.1f} 万" for x in dg["worst"]))
    if dg.get("blocked"):
        lines.append(f"因涨跌停 / 停牌 / 成交额限制未能成交 {dg['blocked']} 次")
    return "\n".join(lines)


def build_prompt(s, st: QuantStrategyRow, champ: QuantVersionRow, n: int) -> str:
    prompt = load_prompt()
    cfg = settings_of(st)
    insts, bench, _ = universe_members(st.universe_name)
    etfs = [f"{i.symbol} {i.name}" for i in insts if i.type in ("etf", "lof")]
    stocks = sum(1 for i in insts if i.type == "stock")
    history = list(s.scalars(select(QuantVersionRow).where(
        QuantVersionRow.strategy_id == st.id, QuantVersionRow.id != champ.id)
        .order_by(QuantVersionRow.id.desc()).limit(HISTORY)))
    hist_lines = []
    for v in reversed(history):
        dev = v.dev or {}
        hist_lines.append(
            f"- v{v.number}「{v.title}」：{v.rationale[:120]} → 开发期夏普 {_num(dev.get('sharpe'))}、"
            f"回撤 {_pct(dev.get('max_drawdown'))}、换手 {_num(dev.get('turnover'), 1)}；"
            f"结果：{STATUS_TEXT.get(v.status, v.status)}"
            + (f"（{v.decision[:80]}）" if v.status == "rejected" and v.decision else "")
            + (f"；程序调参：{'、'.join(tun)}" if (tun := (v.train.get("tuning") or {}).get("changes")) else ""))
    gates = cfg.gates()
    return "\n\n".join([
        f"## 风格：{st.name}\n{st.description}",
        f"## 股票池\n{stocks} 只股票（沪深300 成分股，当前名单）+ {len(etfs)} 只 ETF：{'、'.join(etfs)}\n"
        f"基准：{bench.name}（{bench.symbol}）",
        f"## 因子库\n{_factor_doc()}",
        f"## 因子成绩单（开发期 {cfg.train_start} ～ {cfg.valid_end}，股票池中全部可选股票，"
        f"每 {factor_research.STEP} 个交易日看之后 {factor_research.HORIZON} 日收益）\n"
        "IC 为因子值与之后收益的排名相关系数；|t| ≥ 2 才算统计上可靠；逐年 IC 正负翻转说明因子不稳定。"
        "五分组收益受股票池幸存者偏差影响整体偏高，请看组间差异而非绝对水平。\n"
        + factor_research.scorecard_for_prompt(panel_for(s, st), cfg.train_start, cfg.valid_end,
                                               [(f["name"], f.get("params") or {})
                                                for f in champ.spec.get("factors", [])]),
        f"## {prompt['spec_doc']}",
        f"## 当前冠军 v{champ.number}「{champ.title}」\n规则：\n" + "\n".join(f"- {x}" for x in describe(champ.spec))
        + f"\n规格 JSON：{spec_text(champ.spec)}",
        f"## 冠军的开发期报告（{cfg.train_start} ～ {cfg.valid_end}）\n{_report_text(champ.dev or champ.train)}",
        "## 升级条件\n"
        f"候选与冠军在开发期逐年对比：开发期夏普需高于冠军 {_num((champ.dev or {}).get('sharpe'))}；"
        f"按相同波动换算后，至少 {gates.fold_win_rate:.0%} 的年份跑赢冠军，且月度超额在统计上显著"
        f"（p ≤ {gates.p_max:g}，同一轮候选越多门槛越严）；开发期最大回撤不超过 {gates.max_drawdown:.0%}"
        f"（冠军本身超过时不超过冠军即可）；年化换手不超过 {gates.max_turnover:g} 倍"
        + (f"；股票策略至少持有 {gates.min_holdings} 只" if gates.min_holdings else "")
        + "；参数小幅浮动后依然稳定。小修小补很难通过显著性检验，请提出有实质差别、有经济逻辑的改进。",
        "## 研究笔记（历次优化积累的经验，可能有误，请结合数据判断）\n"
        + ("\n".join(f"- {n['text']}" for n in (st.research_notes or [])[-NOTES_IN_PROMPT:]) or "（暂无）"),
        f"## 冠军开发期的交易诊断\n{trade_diagnosis(champ, st)}",
        "## 最近的尝试\n" + ("\n".join(hist_lines) if hist_lines else "（暂无）"),
        f"## 任务\n请先诊断，再提出 {n} 个候选版本（每个都给出完整规格 JSON）。",
    ])


def trade_diagnosis(champ: QuantVersionRow, st: QuantStrategyRow) -> str:
    """冠军开发期的交易明细摘要：胜率与盈亏比、卖出原因、亏损最多的持有与标的。"""
    from app.quant import details

    try:
        with get_sessionmaker()() as s:
            panel = panel_for(s, s.get(QuantStrategyRow, st.id))
        d = details.details(champ.id, champ.spec, panel, settings_of(st), "dev")
    except Exception as e:  # 诊断只是辅助信息
        return f"（暂不可用：{e}）"
    sm = d["summary"]
    lines = [f"完整持有 {sm['closed']} 次，胜率 {_pct(sm['win_rate'])}，平均盈利 {_pct(sm['avg_win'])}、"
             f"平均亏损 {_pct(sm['avg_loss'])}，平均持有 {sm['avg_days']} 个交易日；成交 {sm['trades']} 笔，费用 "
             f"{sm['fees'] / 1e4:.1f} 万"]
    if sm["exit_reasons"]:
        lines.append("卖出原因：" + "；".join(
            f"{g['reason']} {g['count']} 次（平均 {_pct(g['avg_ret'])}，合计 {g['pnl'] / 1e4:+.1f} 万）"
            for g in sorted(sm["exit_reasons"], key=lambda g: g["pnl"])))
    worst = sorted((e for e in d["episodes"] if not e["open"]), key=lambda e: e["pnl"])[:5]
    if worst:
        lines.append("亏损最多的持有：" + "；".join(
            f"{e['name']} {e['entry']}→{e['exit']} {_pct(e['ret'])}（{e['reason'][:30]}）" for e in worst))
    losers = [x for x in d["symbols"] if x["pnl"] < 0][-5:]
    if losers:
        lines.append("累计亏损最多的标的：" + "、".join(
            f"{x['name']}（{x['episodes']} 次，{x['pnl'] / 1e4:+.1f} 万）" for x in reversed(losers)))
    return "\n".join(lines)


@dataclass
class ParsedResearch:
    analysis: str
    candidates: list[dict] = field(default_factory=list)   # {title, rationale, spec(StrategySpec) | None, error}
    lessons: list[str] = field(default_factory=list)       # 本轮总结的经验，写入长期研究笔记


def _close_brackets(text: str) -> str:
    """修复括号：遇到不匹配的右括号时先补上缺失的右括号（模型常漏写候选对象的 }），末尾补全未闭合的引号和括号。"""
    stack: list[str] = []
    out: list[str] = []
    in_str = escape = False
    for ch in text:
        if in_str:
            out.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if ch not in stack:
                continue                         # 多余的右括号
            while stack[-1] != ch:
                out.append(stack.pop())
            stack.pop()
        out.append(ch)
        if not stack and ch in "}]":
            break                                # 顶层对象结束
    return "".join(out) + ('"' if in_str else "") + "".join(reversed(stack))


def lenient_json(text: str) -> dict:
    try:
        return extract_json(text)
    except DecisionParseError as e:
        if "不完整" not in str(e) and "格式错误" not in str(e):
            raise
        body = _THINK_RE.sub("", text)
        body = body[body.find("{"):].strip().removesuffix("```").strip()
        try:
            obj = json.loads(_close_brackets(body))
        except json.JSONDecodeError:
            raise e from None
        if not isinstance(obj, dict):
            raise e from None
        return obj


def parse_research(text: str) -> ParsedResearch:
    data = lenient_json(text)
    cands = data.get("candidates")
    if not isinstance(cands, list) or not cands:
        raise DecisionParseError("缺少 candidates 列表")
    out = ParsedResearch(analysis=str(data.get("analysis") or "")[:2000],
                         lessons=[str(x).strip()[:150] for x in (data.get("lessons") or [])
                                  if isinstance(x, str | int | float) and str(x).strip()][:3])
    errors = []
    for i, c in enumerate(cands[:5], 1):
        if not isinstance(c, dict) or not isinstance(c.get("spec"), dict):
            errors.append(f"候选 {i} 缺少 spec 对象")
            continue
        try:
            spec = parse_spec(c["spec"])
            out.candidates.append({"title": str(c.get("title") or f"候选 {i}")[:64],
                                   "rationale": str(c.get("rationale") or "")[:1000], "spec": spec})
        except ValueError as e:
            errors.append(f"候选 {i} 规格不合法：{e}")
    if not out.candidates:
        raise DecisionParseError("；".join(errors))
    out.analysis += ("\n（部分候选无效：" + "；".join(errors) + "）") if errors else ""
    return out


# ---------------------------------------------------------------- 运行


def _log(run_id: int, entry: dict | None = None, **fields) -> None:
    with get_sessionmaker()() as s:
        run = s.get(ResearchRunRow, run_id)
        if entry is not None:
            run.log = [*run.log, entry]
        for k, v in fields.items():
            setattr(run, k, v)
        s.commit()


def _round_evaluate(strategy_id: int, run_id: int, parsed: ParsedResearch) -> dict:
    """回测并检验本轮候选（同步，在线程中运行）。先只读计算，最后在一个短事务中写入。返回本轮日志。

    显著性门槛按检验次数收紧（p ≤ p_max / n），避免「多试几个总有一个碰巧显著」：n 默认为当前冠军以来
    累计检验过的候选数（含本轮），关闭 cumulative_tests 时只算本轮候选。
    """
    from app.quant.evaluate import robustness

    with get_sessionmaker()() as s:
        st = s.get(QuantStrategyRow, strategy_id)
        cfg = settings_of(st)
        champ_id = st.champion_id
        champ_rep = champion_reports(s, st)
        panel = panel_for(s, st)
        tried = {spec_text(v.spec) for v in s.scalars(select(QuantVersionRow).where(
            QuantVersionRow.strategy_id == st.id))}
        # 当前冠军以来已经检验过的候选数（累计多重检验校正）
        # 另存的策略在初始版本上继承来源中已比较过的次数（service.prior_tests）
        prior_tests = service_prior_tests(s, st, champ_id) if cfg.cumulative_tests and champ_id else 0
    w = cfg.windows(panel.dates[-1])
    gates = cfg.gates()
    evaluated = []          # [候选, 结果]
    duplicates = []
    for c in parsed.candidates:
        key = spec_text(c["spec"].model_dump())
        if key in tried:
            duplicates.append(c)
            continue
        tried.add(key)
        tuning = None
        if cfg.tune:
            # 程序调参（默认关闭）：只在训练区间微调数值参数
            t = tune(c["spec"], panel, w, gates, cfg.tune_budget, cfg.initial_cash)
            tuning = {"changes": t.changes, "evals": t.evals, "base_score": _round(t.base_score),
                      "score": _round(t.score), "original": c["spec"].model_dump() if t.changes else None}
            if t.changes:
                tuned_key = spec_text(t.spec.model_dump())
                if tuned_key in tried:
                    tuning["note"] = "调参结果与已尝试的版本相同，保留 AI 原始参数"
                else:
                    tried.add(tuned_key)
                    c = {**c, "spec": t.spec}
        result = evaluate_with(panel, cfg, c["spec"], with_robustness=False)
        if tuning is not None:
            result["train"]["tuning"] = tuning
        evaluated.append([c, result])

    n_tests = max(prior_tests + len(evaluated), 1)
    judged = []             # (候选, 结果, 是否通过, 原因)
    for c, result in evaluated:
        cd, pd_ = result["dev"], (champ_rep or {}).get("dev") or {}
        sv, psv = cd.get("sharpe"), pd_.get("sharpe")
        if champ_rep and sv is not None and psv is not None and sv <= psv:
            # 开发期夏普不高于冠军，必然不能升级，不再做稳健性检验，节省时间
            ok, reasons, test = False, [f"开发期夏普 {sv:.2f} 不高于冠军 {psv:.2f}"], None
        else:
            result["robustness"] = robustness(c["spec"], panel, w, sv)
            ok, reasons, test = check_gates({"dev": cd, "robustness": result["robustness"]}, champ_rep, gates,
                                            n_tests=n_tests, holdings=holdings_of(c["spec"]))
        if test is None and champ_rep:
            from app.quant.evaluate import paired_test

            test = paired_test(cd, (champ_rep or {}).get("dev") or {})
        if test is not None:
            cd["vs_champion"] = test | {"champion_id": champ_id, "n_tests": n_tests}
        judged.append((c, result, ok, reasons))

    passed = [e for e in judged if e[2]]
    best = max(passed, key=lambda e: e[1]["dev"].get("sharpe") or -99) if passed else None
    entries = [{"title": c["title"], "status": "duplicate", "reason": "与已尝试过的版本完全相同，跳过"}
               for c in duplicates]
    promoted = None
    with get_sessionmaker()() as s:
        st = s.get(QuantStrategyRow, strategy_id)
        if parsed.lessons:
            # 长期研究笔记：保留最近 NOTES_KEEP 条，跨多次优化提供给 AI
            today = utcnow().date().isoformat()
            notes = list(st.research_notes or []) + [
                {"text": t, "run_id": run_id, "at": today, "source": "ai"} for t in parsed.lessons]
            st.research_notes = notes[-NOTES_KEEP:]
        for e in judged:
            c, result, ok, reasons = e
            v = add_version(s, st, c["spec"], source="ai", title=c["title"], rationale=c["rationale"],
                            parent_id=champ_id, run_id=run_id, result=result)
            v.decision = "；".join(reasons)
            if e is best:
                promote(s, st, v, v.decision)
                promoted = v.number
            else:
                v.status = "rejected"
                if ok:
                    v.decision = f"同轮有更好的候选；{v.decision}"
            vs = (v.dev or {}).get("vs_champion") or {}
            entries.append({"version_id": v.id, "number": v.number, "title": c["title"], "status": v.status,
                            "dev_sharpe": (v.dev or {}).get("sharpe"), "p": vs.get("p"),
                            "excess": vs.get("excess_annual"),
                            "year_wins": f"{vs['year_wins']}/{vs['years']}" if vs else None,
                            "tuning": (v.train.get("tuning") or {}).get("changes") or [],
                            "reason": v.decision})
        s.commit()
    return {"candidates": entries, "promoted": promoted, "lessons": parsed.lessons}


async def run_research(run_id: int) -> str:
    from app.quant import costs

    with get_sessionmaker()() as s:
        run = s.get(ResearchRunRow, run_id)
        st = s.get(QuantStrategyRow, run.strategy_id) if run else None
        owner = st.user_id if st else None
    async with _lock:
        with costs.use(owner):              # 回测按策略所有者的交易成本
            return await _run(run_id)


async def _run(run_id: int) -> str:
    with get_sessionmaker()() as s:
        run = s.get(ResearchRunRow, run_id)
        if run.status == "cancelled":
            return "已取消"
        st = s.get(QuantStrategyRow, run.strategy_id)
        provider = s.get(LLMProviderRow, st.provider_id) if st.provider_id else None
        if provider is None or provider.user_id != st.user_id:      # 只能用策略所有者自己配置的模型
            run.status, run.error = "failed", "未设置研究模型"
            s.commit()
            return run.error
        cfg, model, params, json_mode = llm_config_for(s, st.provider_id, st.model)
        params = {**params, **(st.llm_params or {})}
        run.status, run.progress = "running", "准备数据…"
        s.commit()
        strategy_id, rounds, n = st.id, run.rounds, settings_of(st).candidates
    client = create_client(cfg)
    promoted = 0
    try:
        await asyncio.to_thread(_warm, strategy_id)
        for r in range(1, rounds + 1):
            if run_id in _cancel:
                _cancel.discard(run_id)
                _log(run_id, status="cancelled", progress=f"已取消（完成 {r - 1} 轮）", finished_at=utcnow())
                return "已取消"
            _log(run_id, progress=f"第 {r}/{rounds} 轮：AI 分析中…")
            with get_sessionmaker()() as s:
                st = s.get(QuantStrategyRow, strategy_id)
                prompt = build_prompt(s, st, s.get(QuantVersionRow, st.champion_id), n)
            out = await run_llm_json(client, model=model, system=load_prompt()["system"], user=prompt, params=params,
                                     json_mode=json_mode, timeout_s=TIMEOUT_S, parse=parse_research)
            entry = {"round": r, "status": out.status, "tokens_in": out.tokens_in, "tokens_out": out.tokens_out,
                     "cost": float(calc_cost(cfg, out.tokens_in, out.tokens_out)), "latency_ms": out.latency_ms}
            if out.status != "ok":
                entry["error"] = out.error
                entry["raw_tail"] = (out.raw or "")[-600:]       # 便于排查格式问题
                _log(run_id, entry, rounds_done=r)
                continue
            entry["analysis"] = out.parsed.analysis
            _log(run_id, progress=f"第 {r}/{rounds} 轮：回测 {len(out.parsed.candidates)} 个候选…")
            res = await asyncio.to_thread(_round_evaluate, strategy_id, run_id, out.parsed)
            entry |= res
            promoted += 1 if res["promoted"] else 0
            _log(run_id, entry, rounds_done=r, promoted=promoted)
    except Exception as e:
        logger.exception("策略 %s 研究失败", strategy_id)
        _log(run_id, status="failed", error=f"{type(e).__name__}: {e}", finished_at=utcnow(),
             progress="失败")
        return f"研究失败：{e}"
    summary = f"完成 {rounds} 轮，升级 {promoted} 次"
    _log(run_id, status="done", progress=summary, summary=summary, finished_at=utcnow())
    return summary


def _warm(strategy_id: int) -> None:
    with get_sessionmaker()() as s:
        panel_for(s, s.get(QuantStrategyRow, strategy_id))


def start_run(s, st: QuantStrategyRow, rounds: int | None = None, trigger: str = "manual") -> ResearchRunRow:
    if st.provider_id is None:
        raise LabError("请先为策略设置研究模型（模型供应商与模型）")
    active = s.scalars(select(ResearchRunRow).where(ResearchRunRow.strategy_id == st.id,
                                                    ResearchRunRow.status.in_(("pending", "running")))).first()
    if active:
        raise LabError("该策略已有正在进行的优化")
    run = ResearchRunRow(strategy_id=st.id, trigger=trigger, rounds=rounds or settings_of(st).rounds,
                         progress="排队中…")
    s.add(run)
    s.flush()
    return run


def cancel(run_id: int) -> None:
    _cancel.add(run_id)


def evaluate_manual(s, st: QuantStrategyRow, spec: dict, title: str, rationale: str) -> QuantVersionRow:
    """用户手动编辑的规格：回测并检验，但不自动升级（用户可手动设为冠军）。"""
    parsed = parse_spec(spec)
    result = evaluate_spec(s, st, parsed)
    s.rollback()        # 结束读事务，写入在短事务中完成
    v = add_version(s, st, parsed, source="manual", title=title or "手动编辑", rationale=rationale,
                    parent_id=st.champion_id, result=result)
    ok, reasons, test = check_gates({"dev": v.dev, "robustness": v.robustness}, champion_reports(s, st),
                                    settings_of(st).gates(), holdings=holdings_of(parsed))
    if test is not None:
        v.dev = {**v.dev, "vs_champion": test | {"champion_id": st.champion_id, "n_tests": 1}}
    v.decision = ("可升级：" if ok else "未达升级标准：") + "；".join(reasons)
    return v
