"""策略笔记与定期复盘（PRD 4.4.4）。

- 每个参赛账户有自己的策略笔记（按账户而不是按选手保存）：同一个选手在不同赛道各自进化，
  回放赛道的经验不会泄漏到实时赛道。
- 笔记只增不改：复盘、手动编辑、回滚都会生成新版本；最新版本即当前生效版本，每条决策记录使用的版本号。
- 复盘在每个交易日收盘结算之后、当天决策之前触发，只使用截至该日收盘的数据，因此历史回放不会看到未来。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agent.prompts import render
from app.agent.reflection import (
    MAX_CHANGES,
    MAX_STRATEGY_CHARS,
    is_no_change,
    load_reflection_template,
    parse_reflection,
)
from app.agent.runner import DecisionOutcome, run_llm_json
from app.arena.accounts import load_state
from app.arena.deps import market_service, secret_box
from app.core.config import get_settings
from app.db.session import get_sessionmaker
from app.llm import ProviderConfig, calc_cost, create_client
from app.market.markets import get_market
from app.market.universe import BENCHMARK_NAMES
from app.models import (
    AccountRow,
    AgentRow,
    DecisionRow,
    EventRow,
    FillRow,
    InstrumentRow,
    LLMProviderRow,
    NavSnapshotRow,
    OrderRow,
    ReflectionRow,
    RoundRow,
    StrategyVersionRow,
    TrackRow,
)
from app.trading.market_rules import get_rules
from app.trading.params import RuleParams
from app.trading.types import SIDE_LABELS

logger = logging.getLogger(__name__)

REFLECTION_CONCURRENCY = 3
MAX_TRADES = 40
MAX_NAV_ROWS = 30
MAX_DECISION_NOTES = 8


class StrategyError(ValueError):
    """业务规则错误，message 直接展示给用户。"""


# ====================================================================== 版本


def current_version(s: Session, account_id: int) -> StrategyVersionRow | None:
    return s.scalars(select(StrategyVersionRow).where(StrategyVersionRow.account_id == account_id)
                     .order_by(StrategyVersionRow.version.desc()).limit(1)).first()


def add_version(s: Session, account_id: int, content: str, source: str, changes: str = "",
                data_date: date | None = None, reflection_id: int | None = None) -> StrategyVersionRow:
    content = content.strip()
    if not content:
        raise StrategyError("策略笔记不能为空")
    if len(content) > MAX_STRATEGY_CHARS:
        raise StrategyError(f"策略笔记不能超过 {MAX_STRATEGY_CHARS} 字")
    last = current_version(s, account_id)
    row = StrategyVersionRow(account_id=account_id, version=(last.version if last else 0) + 1, content=content,
                             source=source, changes=changes, data_date=data_date, reflection_id=reflection_id)
    s.add(row)
    s.flush()
    return row


def init_versions(s: Session, track: TrackRow) -> None:
    """赛道启动时：把初始策略写为各账户的第 1 版（账户级的 strategy_seed 优先于选手设置）。"""
    for acc in s.scalars(select(AccountRow).where(AccountRow.track_id == track.id)):
        agent = s.get(AgentRow, acc.agent_id)
        seed = acc.strategy_seed if acc.strategy_seed is not None else agent.initial_strategy
        if agent.kind == "llm" and seed.strip() and current_version(s, acc.id) is None:
            note = "进化实验继承的策略" if acc.strategy_seed is not None else "选手设置的初始策略"
            add_version(s, acc.id, seed, "initial", note, track.start_date)


def rollback(s: Session, account_id: int, version: int) -> StrategyVersionRow:
    old = s.scalars(select(StrategyVersionRow).where(StrategyVersionRow.account_id == account_id,
                                                     StrategyVersionRow.version == version)).first()
    if old is None:
        raise StrategyError("版本不存在")
    return add_version(s, account_id, old.content, "rollback", f"回滚到第 {version} 版")


def decision_context(s: Session, account_id: int) -> tuple[dict | None, int | None]:
    """决策 prompt 中的策略笔记部分与版本号。"""
    sv = current_version(s, account_id)
    if sv is None:
        return None, None
    updated = sv.data_date or sv.created_at.date()
    return {"version": sv.version, "content": sv.content, "updated": str(updated)}, sv.version


STRATEGY_SECTION = """

## 你的策略笔记（第 {version} 版，{updated} 更新）
以下是你根据过往交易复盘自己总结的策略，请按它执行，并在复盘时持续改进；与交易规则冲突时以交易规则为准。
<strategy>
{content}
</strategy>"""


# ====================================================================== 是否需要复盘


def _first_nav_date(s: Session, account_id: int) -> date | None:
    return s.scalar(select(func.min(NavSnapshotRow.date)).where(NavSnapshotRow.account_id == account_id))


def period_anchor(s: Session, account_id: int, d: date) -> date | None:
    """复盘区间起点（不含）：上一次复盘的日期，没有复盘过则为初始净值日期。"""
    last = s.scalar(select(func.max(ReflectionRow.data_date)).where(ReflectionRow.account_id == account_id,
                                                                    ReflectionRow.data_date < d))
    return last or _first_nav_date(s, account_id)


def _days_between(s: Session, account_id: int, after: date, until: date) -> int:
    return s.scalar(select(func.count()).select_from(NavSnapshotRow).where(
        NavSnapshotRow.account_id == account_id, NavSnapshotRow.date > after, NavSnapshotRow.date <= until)) or 0


def is_due(s: Session, acc: AccountRow, agent: AgentRow, d: date) -> bool:
    enabled = acc.reflect_override if acc.reflect_override is not None else agent.reflect_enabled
    if agent.kind != "llm" or not enabled or acc.status != "active":
        return False
    done = s.scalars(select(ReflectionRow.id).where(ReflectionRow.account_id == acc.id,
                                                    ReflectionRow.data_date == d)).first()
    if done is not None:
        return False
    anchor = period_anchor(s, acc.id, d)
    every = acc.reflect_every_override or agent.reflect_every
    return anchor is not None and _days_between(s, acc.id, anchor, d) >= max(every, 1)


# ====================================================================== 复盘上下文


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{x * 100:+.2f}%"


def _ret(a, b) -> float | None:
    return float(b / a - 1) if a else None


@dataclass
class ReflectionJob:
    account_id: int
    agent_name: str
    data_date: date
    period_start: date
    trigger: str
    provider: ProviderConfig
    model: str
    params: dict
    json_mode: bool
    timeout_s: int
    system: str
    user: str
    stats: dict
    base_version: int | None
    template_version: int
    base_content: str = ""
    track_id: int = 0
    notes: list[str] = field(default_factory=list)


def llm_config(s: Session, agent: AgentRow) -> tuple[ProviderConfig, str, dict, bool]:
    cfg, model, _, json_mode = llm_config_for(s, agent.provider_id, agent.model)
    return cfg, model, dict(agent.params or {}), json_mode


def llm_config_for(s: Session, provider_id: int, model: str | None) -> tuple[ProviderConfig, str, dict, bool]:
    """按供应商读取调用配置（评审模型等不属于任何选手的调用也用它）。"""
    provider = s.get(LLMProviderRow, provider_id)
    if provider is None:
        raise StrategyError("模型供应商不存在")
    api_key = secret_box().decrypt(provider.api_key_enc) if provider.api_key_enc else None
    cfg = ProviderConfig(provider.protocol, provider.base_url, api_key, provider.price_in_per_m,
                         provider.price_out_per_m)
    return cfg, model or provider.default_model, {}, provider.supports_json_mode


def _ranking(s: Session, track: TrackRow, me: int, anchor: date, d: date) -> list[str]:
    rows = []
    for acc in s.scalars(select(AccountRow).where(AccountRow.track_id == track.id)):
        agent = s.get(AgentRow, acc.agent_id)
        navs = dict(s.execute(select(NavSnapshotRow.date, NavSnapshotRow.nav).where(
            NavSnapshotRow.account_id == acc.id, NavSnapshotRow.date <= d)).all())
        if not navs:
            continue
        last = navs[max(navs)]
        base = navs.get(anchor) or navs[min(navs)]
        total = _ret(track.initial_cash, last)
        rows.append((total or 0, f"{agent.name}{'（你）' if acc.id == me else ''}：区间 {_pct(_ret(base, last))}"
                                 f" / 累计 {_pct(total)}"))
    rows.sort(key=lambda r: r[0], reverse=True)
    return [f"第 {i} 名 {text}" for i, (_, text) in enumerate(rows, 1)]


def build_job(s: Session, track: TrackRow, acc: AccountRow, agent: AgentRow, d: date, trigger: str) -> ReflectionJob:
    anchor = period_anchor(s, acc.id, d)
    if anchor is None or anchor >= d:
        raise StrategyError("还没有可复盘的交易日")
    days = _days_between(s, acc.id, anchor, d)
    if days == 0:
        raise StrategyError("上次复盘之后还没有新的交易日")
    svc = market_service(s, track.market)

    # ---------- 净值与收益（只用 d 及之前的数据）
    navs = list(s.scalars(select(NavSnapshotRow).where(NavSnapshotRow.account_id == acc.id,
                                                       NavSnapshotRow.date <= d).order_by(NavSnapshotRow.date)))
    first, last = navs[0], navs[-1]
    base = next((n for n in reversed(navs) if n.date <= anchor), first)
    period = [n for n in navs if n.date > anchor]
    peak, drawdown = float(base.nav), 0.0
    for n in period:
        peak = max(peak, float(n.nav))
        drawdown = min(drawdown, float(n.nav) / peak - 1)
    nav_rows, prev = [], base
    for n in period[-MAX_NAV_ROWS:]:
        nav_rows.append(f"{n.date},{n.nav:,.0f},{_pct(_ret(prev.nav, n.nav))},"
                        f"{_pct(_ret(prev.benchmark_close, n.benchmark_close))}")
        prev = n

    # ---------- 成交
    fills = list(s.scalars(select(FillRow).where(FillRow.account_id == acc.id, FillRow.trade_date > anchor,
                                                 FillRow.trade_date <= d).order_by(FillRow.id)))
    symbols = sorted({f.symbol for f in fills} | set(load_state(s, acc).positions))
    latest = svc.latest_bars(symbols, d) if symbols else {}
    names = {r.symbol: r.name for r in s.scalars(select(InstrumentRow).where(InstrumentRow.symbol.in_(symbols)))}
    side_label = SIDE_LABELS
    trades = []
    for f in fills[-MAX_TRADES:]:
        order = s.get(OrderRow, f.order_id)
        bar = latest.get(f.symbol)
        since = _pct(_ret(f.price, bar.close)) if bar else "-"
        pnl = f"{f.realized_pnl:+,.0f}" if f.realized_pnl is not None else "-"
        reason = (order.reason or "").replace("\n", " ").replace(",", "，")[:50] if order else ""
        trades.append(f"{f.trade_date},{side_label.get(f.side, f.side)},{f.symbol} {names.get(f.symbol, '')},"
                      f"{f.qty},{f.price},{pnl},{since},{reason}")
    closed = [f for f in fills if f.side in ("sell", "cover") and f.realized_pnl is not None]

    rejected = []
    for o in s.scalars(select(OrderRow).where(OrderRow.account_id == acc.id, OrderRow.placed_date > anchor,
                                              OrderRow.placed_date <= d,
                                              OrderRow.status.in_(["rejected", "expired"]))
                       .order_by(OrderRow.id.desc()).limit(10)):
        what = "被拒绝" if o.status == "rejected" else "未成交"
        rejected.append(f"{o.placed_date} {side_label.get(o.side, o.side)} {o.symbol} {o.qty} 股 {what}：{o.note}")

    # ---------- 不交易的对照：区间开始时的持仓一直不动，到 d 日的收益（按成交倒推期初持仓，忽略期间送转）
    state = load_state(s, acc)
    start_qty = {sym: pos.qty for sym, pos in state.positions.items()}
    for f in fills:
        # 倒推期初持仓（带符号）：买入、平空使持仓增加，卖出、卖空使持仓减少
        start_qty[f.symbol] = start_qty.get(f.symbol, 0) - (f.qty if f.side in ("buy", "cover") else -f.qty)
    start_qty = {sym: q for sym, q in start_qty.items() if q != 0}
    hold_return = None
    if all(sym in latest for sym in start_qty):
        at_anchor = svc.latest_bars(list(start_qty), anchor) if start_qty else {}
        if all(sym in at_anchor for sym in start_qty):
            cash0 = base.nav - sum((at_anchor[sym].close * q for sym, q in start_qty.items()), Decimal("0"))
            held = cash0 + sum((latest[sym].close * q for sym, q in start_qty.items()), Decimal("0"))
            hold_return = _ret(base.nav, held)

    # ---------- 持仓
    positions = []
    for sym, pos in sorted(state.positions.items()):
        bar = latest.get(sym)
        close = bar.close if bar else pos.avg_cost
        positions.append(f"{sym},{names.get(sym, sym)},{float(close * pos.qty / last.nav) * 100:.1f}%,"
                         f"{_pct(_ret(pos.avg_cost, close))}")

    # ---------- 决策
    dec_rows = s.execute(select(DecisionRow, RoundRow).join(RoundRow, DecisionRow.round_id == RoundRow.id).where(
        DecisionRow.account_id == acc.id, RoundRow.data_date > anchor, RoundRow.data_date <= d)
        .order_by(DecisionRow.id)).all()
    decisions = []
    for dec, rnd in dec_rows[-MAX_DECISION_NOTES:]:
        text = (dec.analysis or dec.error or "").replace("\n", " ")
        decisions.append(f"{rnd.data_date}：{text[:150]}{'…' if len(text) > 150 else ''}")

    stats = {
        "days": days,
        "period_return": _pct(_ret(base.nav, last.nav)),
        "bench_period_return": _pct(_ret(base.benchmark_close, last.benchmark_close)),
        "total_return": _pct(_ret(track.initial_cash, last.nav)),
        "bench_total_return": _pct(_ret(first.benchmark_close, last.benchmark_close)),
        "period_drawdown": _pct(drawdown),
        "nav": f"{last.nav:,.0f}",
        "cash_pct": f"{float(last.cash / last.nav) * 100:.1f}%" if last.nav else "-",
        "buy_amount": f"{sum(f.amount for f in fills if f.side in ('buy', 'cover')):,.0f}",
        "sell_amount": f"{sum(f.amount for f in fills if f.side in ('sell', 'short')):,.0f}",
        "fees": f"{sum(f.total_fee for f in fills):,.0f}",
        "closed": len(closed),
        "wins": sum(1 for f in closed if f.realized_pnl > 0),
        "realized": f"{sum(f.realized_pnl for f in closed):+,.0f}" if closed else "0",
        "decisions": len(dec_rows),
        "decisions_ok": sum(1 for dec, _ in dec_rows if dec.status == "ok"),
        "rejected": sum(1 for o in s.scalars(select(OrderRow.id).where(
            OrderRow.account_id == acc.id, OrderRow.placed_date > anchor, OrderRow.placed_date <= d,
            OrderRow.status == "rejected"))),
        "trades": len(fills),
        "hold_return": _pct(hold_return),
        "turnover": f"{float(sum(f.amount for f in fills) / base.nav) * 100:.0f}%" if base.nav else "-",
    }
    actual = _ret(base.nav, last.nav)
    stats["trading_alpha"] = _pct(actual - hold_return) if actual is not None and hold_return is not None else "-"
    for key, a, b in (("period_excess", base, last), ("total_excess", first, last)):
        mine = _ret(a.nav if key == "period_excess" else track.initial_cash, b.nav)
        bench = _ret(a.benchmark_close, b.benchmark_close)
        stats[key] = _pct(mine - bench) if mine is not None and bench is not None else "-"

    sv = current_version(s, acc.id)
    last_ok = s.scalars(select(ReflectionRow).where(ReflectionRow.account_id == acc.id, ReflectionRow.status == "ok",
                                                    ReflectionRow.data_date < d)
                        .order_by(ReflectionRow.id.desc()).limit(1)).first()
    tpl = load_reflection_template(get_settings().config_dir)
    spec = get_market(track.market)
    ctx = {
        "market_name": spec.name, "currency": track.currency, "max_chars": MAX_STRATEGY_CHARS,
        "rules_lines": get_rules(track.market).describe(RuleParams(**track.rule_params)),
        "max_changes": MAX_CHANGES,
        "now_text": f"{d} 收盘后", "period_text": f"{anchor} 收盘后 ~ {d} 收盘",
        "benchmark_name": BENCHMARK_NAMES.get(track.benchmark_symbol, track.benchmark_symbol),
        "strategy": sv.content if sv else "", "strategy_version": sv.version if sv else 0,
        "last_review": {"date": str(last_ok.data_date), "text": last_ok.review} if last_ok and last_ok.review else None,
        "stats": stats, "nav_rows": nav_rows, "ranking": _ranking(s, track, acc.id, anchor, d),
        "trades": trades, "rejected": rejected, "positions": positions, "decisions": decisions,
    }
    system, user = render(tpl.system, tpl.user, ctx)
    if track.anonymize:
        from app.arena.cycle import make_anonymizer  # 避免循环导入

        insts = svc.get_instruments(track.symbols)
        anon = make_anonymizer(svc, track, {k: v for k, v in insts.items() if k in track.symbols}, d)
        system, user = anon.text(system), anon.text(user)
    cfg, model, params, json_mode = llm_config(s, agent)
    return ReflectionJob(
        account_id=acc.id, agent_name=agent.name, data_date=d, period_start=anchor, trigger=trigger, provider=cfg,
        model=model, params=params, json_mode=json_mode, timeout_s=track.decision_timeout_s, system=system,
        user=user, stats=stats, base_version=sv.version if sv else None, template_version=tpl.version,
        base_content=sv.content if sv else "", track_id=track.id,
    )


# ====================================================================== 执行


def _prepare_due(track_id: int, d: date) -> list[ReflectionJob]:
    with get_sessionmaker()() as s:
        track = s.get(TrackRow, track_id)
        jobs = []
        for acc in s.scalars(select(AccountRow).where(AccountRow.track_id == track_id)):
            agent = s.get(AgentRow, acc.agent_id)
            if is_due(s, acc, agent, d):
                jobs.append(build_job(s, track, acc, agent, d, "schedule"))
        return jobs


def _prepare_manual(account_id: int) -> ReflectionJob:
    with get_sessionmaker()() as s:
        acc = s.get(AccountRow, account_id)
        track = s.get(TrackRow, acc.track_id)
        agent = s.get(AgentRow, acc.agent_id)
        if agent.kind != "llm":
            raise StrategyError("基线选手没有策略笔记")
        if track.last_settled_date is None:
            raise StrategyError("赛道还没有结算过的交易日")
        return build_job(s, track, acc, agent, track.last_settled_date, "manual")


async def _run(job: ReflectionJob, sem: asyncio.Semaphore) -> DecisionOutcome:
    async with sem:
        try:
            limit = MAX_CHANGES if job.base_content else None      # 写第一版时不限修改条数

            def parse(text: str):
                return parse_reflection(text, limit)

            return await run_llm_json(create_client(job.provider), model=job.model, system=job.system, user=job.user,
                                      params=job.params, json_mode=job.json_mode, timeout_s=job.timeout_s, parse=parse)
        except Exception as e:  # 单个选手异常不能影响其他选手
            logger.exception("选手 %s 复盘异常", job.agent_name)
            return DecisionOutcome(status="error", error=f"{type(e).__name__}: {e}")


def _persist(job: ReflectionJob, res: DecisionOutcome) -> str:
    with get_sessionmaker()() as s:
        parsed = res.parsed if res.status == "ok" else None
        row = ReflectionRow(
            account_id=job.account_id, data_date=job.data_date, period_start=job.period_start, trigger=job.trigger,
            status=res.status, model=res.model or job.model, template_version=job.template_version,
            base_version=job.base_version, stats=job.stats, review=parsed.review if parsed else None,
            changes=parsed.changes if parsed else None, prompt_system=job.system, prompt_user=job.user,
            raw_response=res.raw, reasoning=res.reasoning, error=res.error, attempts=res.attempts,
            tokens_in=res.tokens_in, tokens_out=res.tokens_out, cost=calc_cost(job.provider, res.tokens_in,
                                                                             res.tokens_out),
            latency_ms=res.latency_ms,
        )
        s.add(row)
        s.flush()
        if parsed is None:
            msg = f"{job.agent_name} 复盘失败：{res.error}"
            level = "warning"
        elif parsed.strategy.strip() == job.base_content.strip():
            msg = f"{job.agent_name} 复盘完成：策略笔记未修改（仍为第 {job.base_version or 0} 版）"
            level = "info"
        else:
            changes = parsed.changes if not is_no_change(parsed.changes) or not job.base_content else ""
            sv = add_version(s, job.account_id, parsed.strategy, "reflection", changes or "（未说明修改内容）",
                             job.data_date, row.id)
            row.new_version = sv.version
            msg = f"{job.agent_name} 复盘完成：策略笔记更新到第 {sv.version} 版"
            level = "info"
        s.add(EventRow(track_id=job.track_id, account_id=job.account_id, trade_date=job.data_date, type="reflection",
                       level=level, message=msg, detail={"reflection_id": row.id}))
        s.commit()
        return msg


async def run_due_reflections(track_id: int, d: date) -> list[str]:
    """收盘结算后执行到期的复盘（由日终流程调用，调用方持有赛道锁）。"""
    jobs = await asyncio.to_thread(_prepare_due, track_id, d)
    if not jobs:
        return []
    logger.info("赛道 %s：%s 复盘开始，%d 个选手", track_id, d, len(jobs))
    sem = asyncio.Semaphore(REFLECTION_CONCURRENCY)
    results = await asyncio.gather(*(_run(job, sem) for job in jobs))
    return [await asyncio.to_thread(_persist, job, res) for job, res in zip(jobs, results, strict=True)]


async def reflect_now(account_id: int) -> str:
    """手动复盘：以赛道最近结算日为复盘时点（调用方持有赛道锁）。"""
    try:
        job = await asyncio.to_thread(_prepare_manual, account_id)
    except StrategyError as e:
        return f"无法复盘：{e}"
    res = await _run(job, asyncio.Semaphore(1))
    return await asyncio.to_thread(_persist, job, res)
