"""AI 交易复核（规则 + AI 复核，两个账户并行）。

- 「规则账户」就是组合原有的实时跟踪账户；「AI 复核账户」在同一个赛道中并行运行，本金相同。
- AI 复核账户不另算调仓节奏：每轮直接复用规则账户的决策（同一份成员状态、同样的目标权重与加减仓门槛），
  只对**本账户将要新买入**的股票做复核；被否决的股票目标权重置 0，这部分资金留作现金。卖出与其他调整与规则完全相同。
- 复核信息：股票所属行业、哪些成员策略选中、近期涨跌与波动、最近 30 天公告标题（巨潮）、最近新闻标题（东方财富）。
  只在发现明确的重大风险时否决；模型调用失败时按规则执行并记录（不让 AI 故障改变交易）。
- AI 复核账户第一次决策时（还没有持仓），复制规则账户当前的目标持仓并全部复核，之后与规则账户同步调仓。

两个账户唯一的区别就是 AI 的判断，比较两条净值就能看出 AI 是加分还是减分；全部是之后实时发生的交易，
不存在「模型记得历史行情」的问题（文本信号阶段 0 的记忆泄漏检验（预注册文档已归档，未包含在本仓库））。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import date, timedelta
from decimal import Decimal

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.context import MarketSnapshot
from app.baselines import BaselineDecision
from app.db.session import get_sessionmaker
from app.llm.base import ProviderConfig
from app.models import AccountRow, AgentRow, DecisionRow, RoundRow, TrackRow
from app.models.quant import QuantPortfolioRow
from app.trading.portfolio import AccountState

logger = logging.getLogger(__name__)

BATCH = 10
CONCURRENCY = 2
ANN_DAYS = 30
NEWS_DAYS = 14
MAX_ANN, MAX_NEWS = 12, 8
TIMEOUT_S = 600

SYSTEM = """你是 A股交易复核员。规则策略（多风格量化组合）打算在下一个交易日开盘**新买入**下面这些股票。
请逐只检查是否存在**明确的重大风险**，有则否决这笔买入，否则放行。

应当否决的情形（有明确依据时）：立案调查或行政处罚、退市风险或被实施 ST、审计意见非标准、重大财务造假嫌疑、
业绩大幅预亏或暴雷、控股股东或实控人大额减持 / 质押平仓风险、重大诉讼仲裁或债务违约、重大安全事故、
主要业务被禁止或吊销资质、重大资产被冻结等。
不要因为以下原因否决：短期涨跌、估值高低、一般性利空、行业景气度、你对未来走势的主观看法——这些已经由规则策略的因子处理。
没有找到明确依据时一律放行。

对每只股票输出：decision（approve 放行 / veto 否决）、risk（0～3，0 无风险，3 重大风险）、
reason（一句话，引用具体公告或新闻）。
只输出 JSON：{"items": [{"symbol": "600000.SH", "decision": "approve", "risk": 0, "reason": "..."}]}"""


# ---------------------------------------------------------------- 账户与开关


def is_review_agent(agent: AgentRow) -> bool:
    return bool((agent.params or {}).get("ai_review"))


def rule_agent(s: Session, portfolio_id: int) -> AgentRow | None:
    """组合的规则选手（排除 AI 复核选手与自选倾斜选手）。"""
    for a in s.scalars(select(AgentRow).where(AgentRow.kind == "quant", AgentRow.quant_portfolio_id == portfolio_id)
                       .order_by(AgentRow.id)):
        if not is_review_agent(a) and not (a.params or {}).get("watch_tilt"):
            return a
    return None


def settings(pf: QuantPortfolioRow) -> dict:
    return dict((pf.settings or {}).get("review") or {})


def enable(s: Session, pf: QuantPortfolioRow, provider_id: int, model: str | None = None) -> AccountRow:
    """为跟踪中的组合开启 AI 复核：在规则账户所在赛道中加入一个 AI 复核账户（本金相同）。"""
    from app.arena.lifecycle import ArenaError
    from app.models import LLMProviderRow

    if not pf.follow_account_id:
        raise ArenaError("请先开始实时跟踪（AI 复核账户与规则账户在同一个赛道中并行）")
    rule_acc = s.get(AccountRow, pf.follow_account_id)
    track = s.get(TrackRow, rule_acc.track_id) if rule_acc else None
    if track is None or track.status not in ("running", "paused"):
        raise ArenaError("规则账户的实时跟踪没有在运行")
    if s.get(LLMProviderRow, provider_id) is None:
        raise ArenaError("模型供应商不存在")
    cur = settings(pf)
    if cur.get("enabled") and cur.get("account_id"):
        acc = s.get(AccountRow, cur["account_id"])
        if acc is not None and acc.status == "active":
            raise ArenaError("已开启 AI 复核")
    agent = next((a for a in s.scalars(select(AgentRow).where(AgentRow.kind == "quant",
                                                              AgentRow.quant_portfolio_id == pf.id))
                  if is_review_agent(a)), None)
    if agent is None:
        agent = AgentRow(name=f"组合·{pf.name}·AI复核", kind="quant", quant_portfolio_id=pf.id, user_id=pf.user_id,
                         params={"ai_review": True},
                         description=f"多风格组合「{pf.name}」+ AI 交易复核：与规则账户同步调仓，AI 可否决新买入")
        s.add(agent)
    agent.provider_id, agent.model = provider_id, model
    s.flush()
    # 启动后的赛道不允许加选手（保证公平）；这里是有意设计的「孪生账户」对照，由本函数直接创建账户
    acc = s.scalars(select(AccountRow).where(AccountRow.track_id == track.id, AccountRow.agent_id == agent.id)).first()
    if acc is None:
        acc = AccountRow(track_id=track.id, agent_id=agent.id, cash=track.initial_cash, status="active")
        s.add(acc)
        s.flush()
    else:
        acc.status = "active"
    from app.core.timeutils import utcnow

    pf.settings = {**(pf.settings or {}), "review": {"enabled": True, "account_id": acc.id, "agent_id": agent.id,
                                                     "provider_id": provider_id, "model": model,
                                                     "since": cur.get("since") or utcnow().date().isoformat()}}
    return acc


def disable(s: Session, pf: QuantPortfolioRow) -> None:
    cur = settings(pf)
    acc = s.get(AccountRow, cur["account_id"]) if cur.get("account_id") else None
    if acc is not None:
        acc.status = "stopped"
    pf.settings = {**(pf.settings or {}), "review": {**cur, "enabled": False}}


# ---------------------------------------------------------------- 决策


def _rule_context(portfolio_id: int, ai_account_id: int) -> tuple[int | None, AccountState | None, dict | None,
                                                                    bool]:
    """(规则账户 id, 规则账户当前状态, 规则账户最近一次目标, AI 账户是否第一次决策)。"""
    from app.arena.accounts import load_state

    with get_sessionmaker()() as s:
        pf = s.get(QuantPortfolioRow, portfolio_id)
        rule_id = pf.follow_account_id if pf else None
        rule_acc = s.get(AccountRow, rule_id) if rule_id else None
        if rule_acc is None:
            return None, None, None, False
        rule_state = load_state(s, rule_acc)
        last_targets = None
        for d in s.scalars(select(DecisionRow).where(DecisionRow.account_id == rule_id)
                           .order_by(DecisionRow.id.desc()).limit(200)):
            t = (d.parsed or {}).get("targets")
            if t:
                last_targets = t
                break
        first = s.scalars(select(DecisionRow.id).where(DecisionRow.account_id == ai_account_id)
                          .limit(1)).first() is None
    return rule_id, rule_state, last_targets, first


def _price_lines(panel, i: int, j: int) -> str:
    c = panel.adj_close[: i + 1, j]

    def ret(n: int) -> str:
        return f"{c[-1] / c[-1 - n] - 1:+.1%}" if len(c) > n and np.isfinite(c[-1 - n]) and c[-1 - n] > 0 else "-"

    high = np.nanmax(c[-250:]) if len(c) else np.nan
    dd = f"{c[-1] / high - 1:+.1%}" if np.isfinite(high) and high > 0 else "-"
    vol = np.nanstd(np.diff(np.log(c[-21:]))) * np.sqrt(252) if len(c) > 21 else np.nan
    return (f"收盘 {panel.close_ff[i, j]:.2f}；5 日 {ret(5)}、20 日 {ret(20)}、60 日 {ret(60)}；"
            f"距一年高点 {dd}；20 日年化波动 {vol:.0%}" if np.isfinite(vol) else f"收盘 {panel.close_ff[i, j]:.2f}")


def _fetch_text(symbol: str, d: date) -> tuple[list[str], list[str]]:
    """最近的公告标题与新闻标题（失败时返回空列表，不影响复核）。"""
    from app.market.news import EastmoneyNewsProvider
    from app.textsignal.announcements import fetch as fetch_ann

    anns: list[str] = []
    news: list[str] = []
    try:
        items = fetch_ann(symbol, d - timedelta(days=ANN_DAYS), d)
        anns = [f"{a.date} {a.title}" for a in sorted(items, key=lambda a: a.date, reverse=True)[:MAX_ANN]]
    except Exception as e:  # noqa: BLE001 - 数据源失败只影响信息量
        logger.info("复核：%s 公告获取失败：%s", symbol, e)
    try:
        cutoff = d - timedelta(days=NEWS_DAYS)
        items = [n for n in EastmoneyNewsProvider().fetch(symbol) if n.published_at.date() >= cutoff]
        news = [f"{n.published_at:%m-%d} {n.title}" for n in sorted(items, key=lambda n: n.published_at,
                                                                   reverse=True)[:MAX_NEWS]]
    except Exception as e:  # noqa: BLE001
        logger.info("复核：%s 新闻获取失败：%s", symbol, e)
    return anns, news


def _candidates_text(portfolio_id: int, symbols: list[str], snap: MarketSnapshot, picks: dict[str, list[str]]
                     ) -> tuple[dict[str, str], dict[str, str]]:
    """每只候选股票给复核员的材料，以及股票名称。"""
    from app.quant.live import live_panel
    from app.quant.portfolio import members_of

    with get_sessionmaker()() as s:
        pf = s.get(QuantPortfolioRow, portfolio_id)
        members = members_of(s, pf)
        panel, i = live_panel(s, members[0].strategy.universe_name, snap.data_date)
    out, names = {}, {}
    for sym in symbols:
        j = panel.col.get(sym)
        if j is None:
            continue
        names[sym] = panel.names[j]
        ind = (panel.industry_names[panel.industry[i, j]]
               if panel.industry is not None and panel.industry[i, j] >= 0 else "未知")
        anns, news = _fetch_text(sym, snap.data_date)
        lines = [f"### {sym} {panel.names[j]}（{ind}）",
                 f"规则选中：{'、'.join(picks.get(sym, [])) or '组合'}",
                 f"行情：{_price_lines(panel, i, j)}",
                 "最近公告：" + ("；".join(anns) if anns else "无"),
                 "最近新闻：" + ("；".join(news) if news else "无")]
        out[sym] = "\n".join(lines)
    return out, names


def _parse(raw: str) -> list[dict]:
    from app.agent.parse import DecisionParseError

    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        raise DecisionParseError("没有找到 JSON 对象")
    try:
        obj = json.loads(m.group(0))
    except ValueError as e:
        raise DecisionParseError(f"JSON 格式错误：{e}") from None
    items = obj.get("items") if isinstance(obj, dict) else None
    if not isinstance(items, list):
        raise DecisionParseError("缺少 items 列表")
    return [x for x in items if isinstance(x, dict) and x.get("symbol")]


SUBMIT_SPEC_PARAMS = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {"symbol": {"type": "string", "description": "材料中的股票代码，原样填写（如 600000.SH）"},
                       "decision": {"type": "string", "enum": ["approve", "veto"]},
                       "risk": {"type": "integer", "minimum": 0, "maximum": 3}, "reason": {"type": "string"}},
        "required": ["symbol", "decision", "reason"]}}},
    "required": ["items"],
}
MAX_SKILL_CALLS = 8          # 每批（最多 10 只）最多调用技能的次数


_EXCHANGE = re.compile(r"^(SH|SZ|BJ|HK|US)(?=[0-9A-Z])|\.(SH|SZ|BJ|HK|US)$")


def _match_symbol(raw: object, chunk: list[str]) -> str | None:
    """模型提交的代码对应到本批股票。查过技能后模型常改用技能的写法（如 sh600519），平台是 600519.SH，
    对不上的结论会被当成「未复核」按规则执行，所以去掉交易所前缀 / 后缀再比较。"""
    sym = str(raw).strip().upper()
    if sym in chunk:
        return sym
    core = _EXCHANGE.sub("", sym)
    found = [c for c in chunk if _EXCHANGE.sub("", c.upper()) == core]
    return found[0] if len(found) == 1 else None


def _skill_catalog() -> list[dict]:
    from app.skills import tools as skill_tools

    try:
        with get_sessionmaker()() as s:
            return skill_tools.catalog(s)
    except Exception:  # noqa: BLE001 - 技能系统不可用时照常复核
        logger.exception("读取技能清单失败")
        return []


def _market_enabled() -> bool:
    from app.skills import tools as skill_tools

    try:
        with get_sessionmaker()() as s:
            return skill_tools.market_enabled(s)
    except Exception:  # noqa: BLE001
        logger.exception("读取技能市场设置失败")
        return False


def _skills_allowed(portfolio_id: int) -> bool:
    """技能（如 westock，用的是管理员个人的授权）只给管理员的组合使用；其他用户的 AI 复核不调用技能。"""
    from app.auth import core
    from app.models.quant import QuantPortfolioRow

    with get_sessionmaker()() as s:
        pf = s.get(QuantPortfolioRow, portfolio_id)
        return pf is not None and core.is_admin(pf.user_id)


async def review_symbols(texts: dict[str, str], data_date: date, provider: ProviderConfig, model: str,
                         params: dict | None = None, use_skills: bool = True) -> dict:
    """调用模型复核；返回 {"items": {symbol: {decision, risk, reason}}, "errors": [...], tokens, cost, skill_calls}。

    有已安装的技能时用工具模式：模型可以按需阅读技能说明、调用技能查更多资料（如公告全文、业绩预告），
    最后调用 submit_decision 提交；模型不支持工具调用或工具模式失败时，退回一次性复核。
    """
    from app.agent.runner import run_llm_json, run_llm_tools
    from app.llm import ToolSpec, calc_cost, create_client
    from app.skills import tools as skill_tools

    client = create_client(provider)
    p = {"temperature": 0, **(params or {})}
    syms = list(texts)
    sem = asyncio.Semaphore(CONCURRENCY)
    result: dict = {"items": {}, "errors": [], "tokens_in": 0, "tokens_out": 0, "cost": 0.0, "skill_calls": []}
    catalog = await asyncio.to_thread(_skill_catalog) if use_skills else []
    market_on = use_skills and await asyncio.to_thread(_market_enabled)
    submit = ToolSpec("submit_decision", "提交本批股票的复核结论（每只一条）。", SUBMIT_SPEC_PARAMS)
    extra = [*skill_tools.specs(catalog), *(skill_tools.market_specs() if market_on else [])]
    tool_specs = [*extra, submit] if extra else []
    sections = [SYSTEM, skill_tools.catalog_text(catalog), skill_tools.MARKET_TEXT if market_on else ""]
    system_tools = ("\n\n".join(x for x in sections if x) + "\n\n"
                    "查完资料后调用 submit_decision 提交（参数格式与上面的 JSON 相同）。"
                    f"本批最多调用工具 {MAX_SKILL_CALLS} 次，只对需要核实的股票查询。") if extra else ""

    async def one(chunk: list[str]) -> None:
        user = f"数据日期：{data_date}（收盘后），下一交易日开盘买入。\n\n" + "\n\n".join(texts[x] for x in chunk)
        async with sem:
            out = None
            if tool_specs:
                requested = []

                async def execute(name: str, args: dict) -> str:
                    if name in ("search_skill_market", "request_skill_install"):
                        if name == "request_skill_install":
                            if requested:
                                return "错误：本批已经提交过一次安装申请"
                            requested.append(args.get("url"))
                        return await skill_tools.execute_market(name, args, "AI 复核员")
                    return await skill_tools.execute(name, args, "ai:review", f"复核 {data_date}")

                out = await run_llm_tools(client, model=model, system=system_tools, user=user, params=p,
                                          timeout_s=TIMEOUT_S, tools=tool_specs, execute=execute, parse=_parse,
                                          max_calls=MAX_SKILL_CALLS)
                result["skill_calls"] += [{"tool": t["tool"], "args": t["args"], "ms": t["ms"]}
                                          for t in (out.trace or []) if t["tool"] != "submit_decision"]
                if out.status != "ok":
                    result["errors"].append(f"工具模式失败（{out.error}），改用一次性复核")
                    result["tokens_in"] += out.tokens_in
                    result["tokens_out"] += out.tokens_out
                    out = None
            if out is None:
                out = await run_llm_json(client, model=model, system=SYSTEM, user=user, params=p, json_mode=True,
                                         timeout_s=TIMEOUT_S, parse=_parse)
        result["tokens_in"] += out.tokens_in
        result["tokens_out"] += out.tokens_out
        result["cost"] += float(calc_cost(provider, out.tokens_in, out.tokens_out))
        if out.status != "ok":
            result["errors"].append(f"{len(chunk)} 只：{out.error}")
            return
        for it in out.parsed:
            sym = _match_symbol(it["symbol"], chunk)
            if sym is None:
                logger.warning("复核结论的股票代码对不上本批股票：%s", it["symbol"])
                continue
            result["items"][sym] = {"decision": "veto" if str(it.get("decision")).lower() == "veto" else "approve",
                                    "risk": it.get("risk"), "reason": str(it.get("reason") or "")[:200]}

    await asyncio.gather(*(one(syms[k:k + BATCH]) for k in range(0, len(syms), BATCH)))
    return result


async def decide_reviewed(portfolio_id: int, account_id: int, state: AccountState, snap: MarketSnapshot,
                          provider: ProviderConfig | None, model: str | None, params: dict | None = None
                          ) -> BaselineDecision:
    """AI 复核账户的决策：规则账户的决策 − AI 否决的新买入。"""
    from app.quant.live import decide_portfolio

    rule_id, rule_state, last_targets, first = await asyncio.to_thread(_rule_context, portfolio_id, account_id)
    if rule_id is None:
        return BaselineDecision(None, "组合没有在实时跟踪，AI 复核账户本轮不调仓")
    rule = await asyncio.to_thread(decide_portfolio, portfolio_id, rule_id, rule_state, snap)
    targets = rule.targets
    sync = False
    if targets is None and first and not state.positions and last_targets:
        # 第一次决策：复制规则账户当前的目标持仓，之后与规则账户同步调仓
        targets = {k: Decimal(str(v)) for k, v in last_targets.items()}
        sync = True
    if targets is None:
        return BaselineDecision(None, rule.analysis, extra={**rule.extra, "review": {"skipped": "非调仓日"}})
    held = {k for k, p in state.positions.items() if p.qty > 0}
    new_buys = [k for k, w in targets.items() if w > 0 and k not in held]
    picks = _member_picks(rule.extra.get("members") or {}, portfolio_id)
    review: dict = {"model": model, "candidates": len(new_buys), "sync": sync, "items": [], "vetoed": []}
    lines = [rule.analysis] if not sync else [f"AI 复核账户首次建仓：复制规则账户当前的 {len(targets)} 个目标持仓。"]
    if new_buys and provider is not None and model:
        texts, names = await asyncio.to_thread(_candidates_text, portfolio_id, new_buys, snap, picks)
        try:
            use_skills = await asyncio.to_thread(_skills_allowed, portfolio_id)
            res = await review_symbols(texts, snap.data_date, provider, model, params, use_skills=use_skills)
        except Exception as e:  # noqa: BLE001 - 复核失败按规则执行
            logger.exception("AI 复核失败")
            res = {"items": {}, "errors": [f"{type(e).__name__}: {e}"]}
        review |= {k: res[k] for k in ("errors", "tokens_in", "tokens_out", "cost", "skill_calls") if k in res}
        for sym in new_buys:
            it = res["items"].get(sym)
            if it is None:
                review["items"].append({"symbol": sym, "name": names.get(sym, ""), "decision": "approve", "risk": None,
                                        "reason": "未得到复核结果，按规则执行"})
                continue
            review["items"].append({"symbol": sym, "name": names.get(sym, ""), **it})
            if it["decision"] == "veto":
                review["vetoed"].append(sym)
                targets = {k: v for k, v in targets.items() if k != sym}
        lines.append(f"AI 复核（{model}）：新买入 {len(new_buys)} 只，否决 {len(review['vetoed'])} 只"
                     + ("；" + "；".join(f"{x['name'] or x['symbol']}：{x['reason']}" for x in review["items"]
                                       if x["decision"] == "veto") if review["vetoed"] else "")
                     + (f"；复核失败 {len(review['errors'])} 批，相应股票按规则执行" if review.get("errors") else ""))
    elif new_buys:
        review["errors"] = ["未配置复核模型"]
        lines.append("AI 复核：未配置复核模型，按规则执行")
    else:
        lines.append("AI 复核：本轮没有新买入，无需复核")
    return BaselineDecision(targets, "\n".join(lines), param_overrides=rule.param_overrides,
                            extra={**rule.extra, "review": review})


def _member_picks(members: dict, portfolio_id: int) -> dict[str, list[str]]:
    """每只股票由哪些成员策略选中（给复核员参考）。"""
    from app.models.quant import QuantStrategyRow

    out: dict[str, list[str]] = {}
    if not members:
        return out
    with get_sessionmaker()() as s:
        names = {str(st.id): st.name for st in s.scalars(select(QuantStrategyRow).where(
            QuantStrategyRow.id.in_([int(k) for k in members])))}
    for sid, m in members.items():
        for sym in (m.get("weights") or {}):
            out.setdefault(sym, []).append(names.get(sid, sid))
    return out


# ---------------------------------------------------------------- 对比报告


def report(s: Session, pf: QuantPortfolioRow) -> dict:
    """规则账户与 AI 复核账户的净值对比，以及每次否决之后被否决股票的表现。"""
    from app.models import NavSnapshotRow
    from app.quant.live import live_panel
    from app.quant.portfolio import members_of

    cfg = settings(pf)
    out: dict = {"enabled": bool(cfg.get("enabled")), "settings": cfg}
    if not cfg.get("account_id") or not pf.follow_account_id:
        return out
    ai_id, rule_id = cfg["account_id"], pf.follow_account_id

    def navs(acc_id: int) -> dict[str, float]:
        return {d.isoformat(): float(n) for d, n in s.execute(
            select(NavSnapshotRow.date, NavSnapshotRow.nav).where(NavSnapshotRow.account_id == acc_id)
            .order_by(NavSnapshotRow.date))}

    rn, an = navs(rule_id), navs(ai_id)
    common = sorted(set(rn) & set(an))
    if common:
        r0, a0 = rn[common[0]], an[common[0]]
        out["curve"] = [{"date": d, "rule": round(rn[d] / r0, 6), "ai": round(an[d] / a0, 6)} for d in common]
        out["rule_return"] = round(rn[common[-1]] / r0 - 1, 6)
        out["ai_return"] = round(an[common[-1]] / a0 - 1, 6)
        out["since"] = common[0]
    rows = s.execute(select(DecisionRow, RoundRow).join(RoundRow, DecisionRow.round_id == RoundRow.id)
                     .where(DecisionRow.account_id == ai_id).order_by(RoundRow.data_date.desc())).all()
    reviews = []
    for dec, rnd in rows:
        rv = (dec.parsed or {}).get("review") or {}
        for it in rv.get("items", []):
            reviews.append({**it, "date": rnd.data_date.isoformat(),
                            "fill_date": rnd.fill_date.isoformat() if rnd.fill_date else None})
    members = members_of(s, pf)
    after: dict[str, list[float]] = {"veto": [], "approve": []}
    if reviews and members:
        panel, last = live_panel(s, members[0].strategy.universe_name, date.today())
        adj_open = panel.open * panel.factor
        for r in reviews:
            j = panel.col.get(r["symbol"])
            if j is None or not r["fill_date"]:
                continue
            i = panel.locate(date.fromisoformat(r["fill_date"]), "after")
            if i > last or not np.isfinite(adj_open[i, j]) or adj_open[i, j] <= 0:
                continue
            ret = float(panel.adj_close[last, j] / adj_open[i, j] - 1)
            bench = float(panel.bench[last] / panel.bench[max(i - 1, 0)] - 1)
            r["after"], r["after_excess"] = round(ret, 4), round(ret - bench, 4)
            after[r["decision"]].append(ret - bench)
    out["reviews"] = reviews[:500]
    out["summary"] = {
        "reviewed": len(reviews), "vetoed": sum(r["decision"] == "veto" for r in reviews),
        "veto_after_excess": round(float(np.mean(after["veto"])), 4) if after["veto"] else None,
        "approve_after_excess": round(float(np.mean(after["approve"])), 4) if after["approve"] else None,
        "cost": round(sum(float(((d.parsed or {}).get("review") or {}).get("cost") or 0) for d, _ in rows), 4),
        "skill_calls": sum(len(((d.parsed or {}).get("review") or {}).get("skill_calls") or []) for d, _ in rows),
    }
    return out
