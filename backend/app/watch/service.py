"""自选股监控：AI 盯重大事件，不预测涨跌、不给买卖建议。

每个交易日晚上（21:15，北京时间，排在 A股日终流程与 AI 复核之后）检查一遍，也可以在页面上手动检查：
1. 收集资料：新增的公告（A股，巨潮）、新闻（东方财富）、近 5 个交易日走势与当天涨跌。看过的公告 / 新闻记在
   state 里，不会重复提醒；第一次检查只看最近 7 天。
2. 没有新资料、当天涨跌也不大（< 5%）时不调用 AI。
3. 有新资料时交给 AI（默认用 AI 复核的模型，即本地 Qwen，关闭思考过程；有已安装的技能时可以按需查询，
   如 westock 的公告全文、风险事件），找出用户需要知道的事件，说明发生了什么、意味着什么、影响多大。
4. 重要（level ≥ 2）事件推送微信（「自选股提醒」）。

AI 只读资料、写说明；买卖由用户决定。自选权重只作用于「自选倾斜」账户（见 app/watch/tilt.py）。

多用户：自选股、事件、监控设置都按用户区分；检查用该用户自己配置的模型，结果只推送给该用户。
技能（管理员个人的授权）只在检查管理员的自选股时使用。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.timeutils import utcnow
from app.db.session import get_sessionmaker
from app.models import DEFAULT_USER_ID
from app.models.settings import AppSettingRow, user_key
from app.models.watch import WatchEventRow, WatchItemRow
from app.stocks import service as sv

logger = logging.getLogger(__name__)

SETTINGS_KEY = "watch.settings"
MAX_ITEMS = 30
WEIGHT_MIN, WEIGHT_MAX = 0.0, 3.0
FIRST_LOOKBACK_DAYS = 7
ANN_DAYS = 30
BIG_MOVE = 0.05
SEEN_KEEP = 400
MAX_NEW = 25                    # 每次最多交给 AI 的新增资料条数
TIMEOUT_S = 300
MAX_TOOL_CALLS = 6
CONCURRENCY = 2
LEVELS = {1: "一般关注", 2: "重要", 3: "重大"}
DIRECTIONS = {"good": "利好", "bad": "利空", "neutral": "中性"}

SYSTEM = """你是自选股监控员。用户关注下面这只股票，请阅读「新增资料」（最近的公告、新闻和走势），
判断其中有没有用户需要知道的事件。

值得提醒的事件（有明确依据时）：业绩预告 / 快报 / 财报出现大幅变化，立案调查或行政处罚，监管问询，重大诉讼仲裁，
控股股东 / 实控人 / 高管大额增减持，股权质押或冻结风险，重大资产重组、并购、出售，大额回购，分红方案，重大合同或订单，
停复牌，退市风险或被实施 ST，主营业务或核心产品重大变化，重大安全事故，以及当天股价异常波动的原因。
例行公告（董事会决议、会议通知、法律意见书等格式性文件）不需要提醒，除非其中包含上面的事项。

要求：只依据资料说话，不要编造；不预测股价涨跌，不给出买卖建议。
对每个事件给出：level（1 一般关注 / 2 重要 / 3 重大）、direction（good 利好 / bad 利空 / neutral 中性）、
title（一句话标题，不超过 30 字）、summary（发生了什么，引用资料中的具体数字）、
impact（对公司意味着什么、影响有多大，例如占营收 / 利润 / 市值的比例；资料不足时如实说明）、
sources（引用的资料编号列表）。
同一件事只写一条；没有值得提醒的事件就返回空列表。
只输出 JSON：
{"events": [{"level": 2, "direction": "bad", "title": "...", "summary": "...", "impact": "...", "sources": [1]}]}"""

SUBMIT_PARAMS = {
    "type": "object",
    "properties": {"events": {"type": "array", "items": {
        "type": "object",
        "properties": {"level": {"type": "integer", "minimum": 1, "maximum": 3},
                       "direction": {"type": "string", "enum": ["good", "bad", "neutral"]},
                       "title": {"type": "string"}, "summary": {"type": "string"}, "impact": {"type": "string"},
                       "sources": {"type": "array", "items": {"type": "integer"}}},
        "required": ["level", "direction", "title", "summary"]}}},
    "required": ["events"],
}


class WatchError(Exception):
    pass


# ---------------------------------------------------------------- 设置与自选股


def settings(s: Session, uid: int = DEFAULT_USER_ID) -> dict:
    """{enabled, provider_id, model, push_level}；provider_id 默认沿用该用户 AI 复核的模型。"""
    from app.models import LLMProviderRow

    row = s.get(AppSettingRow, user_key(SETTINGS_KEY, uid))
    cfg = dict(row.value or {}) if row else {}
    if not cfg.get("provider_id"):
        from app.models.quant import QuantPortfolioRow
        from app.quant import review

        for pf in s.scalars(select(QuantPortfolioRow).where(QuantPortfolioRow.user_id == uid)):
            pid = review.settings(pf).get("provider_id")
            if pid:
                cfg["provider_id"] = pid
                break
    prov = s.get(LLMProviderRow, cfg["provider_id"]) if cfg.get("provider_id") else None
    if prov is None or prov.user_id != uid:         # 只能用自己配置的模型
        cfg["provider_id"] = None
    return {"enabled": cfg.get("enabled", True), "provider_id": cfg.get("provider_id"), "model": cfg.get("model"),
            "push_level": int(cfg.get("push_level", 2))}


def set_settings(s: Session, uid: int = DEFAULT_USER_ID, **kw) -> dict:
    from app.models import LLMProviderRow

    key = user_key(SETTINGS_KEY, uid)
    row = s.get(AppSettingRow, key) or AppSettingRow(key=key)
    cur = dict(row.value or {})
    for k in ("enabled", "provider_id", "model", "push_level"):
        if k in kw and kw[k] is not None:
            cur[k] = kw[k]
    if "push_level" in cur and int(cur["push_level"]) not in (1, 2, 3):
        raise WatchError("推送级别只能是 1～3")
    if kw.get("provider_id") is not None:
        prov = s.get(LLMProviderRow, kw["provider_id"])
        if prov is None or prov.user_id != uid:
            raise WatchError("模型供应商不存在（只能使用自己配置的模型）")
    row.value = cur
    s.add(row)
    s.flush()
    return settings(s, uid)


def items(s: Session, uid: int = DEFAULT_USER_ID) -> list[WatchItemRow]:
    return list(s.scalars(select(WatchItemRow).where(WatchItemRow.user_id == uid).order_by(WatchItemRow.id)))


def weights(s: Session, uid: int = DEFAULT_USER_ID) -> dict[str, float]:
    """该用户的自选权重：{代码: 权重}（非自选股默认 1，不在此列）。"""
    return {r.symbol: float(r.weight) for r in items(s, uid)}


def _check_weight(w: float) -> float:
    w = float(w)
    if not WEIGHT_MIN <= w <= WEIGHT_MAX:
        raise WatchError(f"权重须在 {WEIGHT_MIN:g}～{WEIGHT_MAX:g} 之间")
    return round(w, 2)


def add(s: Session, symbol: str, weight: float = 1.0, note: str = "", uid: int = DEFAULT_USER_ID) -> WatchItemRow:
    symbol = sv.normalize_symbol(symbol)
    if symbol in sv.INDICES:
        raise WatchError("指数不能加入自选股")
    if s.scalar(select(WatchItemRow).where(WatchItemRow.symbol == symbol, WatchItemRow.user_id == uid)) is not None:
        raise WatchError(f"{symbol} 已在自选股中")
    if len(items(s, uid)) >= MAX_ITEMS:
        raise WatchError(f"自选股最多 {MAX_ITEMS} 只（每只每天都要 AI 检查）")
    inst = sv.resolve(s, symbol)
    q = sv.quotes([symbol]).get(symbol) or {}
    name = q.get("name") if inst.name.isascii() and q.get("name") else inst.name
    row = WatchItemRow(symbol=symbol, name=name, market=inst.market, weight=_check_weight(weight), note=note[:500],
                       user_id=uid)
    s.add(row)
    s.flush()
    return row


def update(s: Session, row: WatchItemRow, *, weight: float | None = None, note: str | None = None,
           monitor: bool | None = None) -> WatchItemRow:
    if weight is not None:
        row.weight = _check_weight(weight)
    if note is not None:
        row.note = note[:500]
    if monitor is not None:
        row.monitor = monitor
    s.flush()
    return row


def to_dict(row: WatchItemRow) -> dict:
    return {"id": row.id, "symbol": row.symbol, "name": row.name, "market": row.market, "weight": row.weight,
            "note": row.note, "monitor": row.monitor,
            "last_check_at": row.last_check_at.isoformat() if row.last_check_at else None,
            "last_result": row.last_result or {}}


def event_dict(e: WatchEventRow, name: str = "") -> dict:
    return {"id": e.id, "item_id": e.item_id, "symbol": e.symbol, "name": name, "date": e.date.isoformat(),
            "level": e.level, "direction": e.direction, "title": e.title, "summary": e.summary, "impact": e.impact,
            "sources": e.sources or [], "model": e.model, "notified": e.notified, "read": e.read,
            "created_at": e.created_at.isoformat()}


# ---------------------------------------------------------------- 收集资料


def collect(row: WatchItemRow, today: date) -> dict:
    """新增的公告 / 新闻（没看过的）与走势。返回 {materials: [{kind, date, title, url, key}], move: {...}}。"""
    from app.market.news import EastmoneyNewsProvider
    from app.textsignal.announcements import fetch as fetch_ann

    seen = set((row.state or {}).get("seen", []))
    first = not seen and not row.last_check_at
    since = today - timedelta(days=FIRST_LOOKBACK_DAYS if first else ANN_DAYS)
    mats: list[dict] = []
    errors: list[str] = []
    if row.market == "CN":
        try:
            for a in fetch_ann(row.symbol, since, today):
                mats.append({"kind": "公告", "date": a.date, "title": a.title, "url": a.url, "key": a.url})
        except Exception as e:  # noqa: BLE001 - 数据源失败只影响信息量
            errors.append(f"公告：{e}")
    try:
        for n in EastmoneyNewsProvider().fetch(row.symbol):
            d = n.published_at.date()
            if d >= since:
                mats.append({"kind": "新闻", "date": d.isoformat(), "title": n.title, "url": n.url,
                             "key": n.url or n.title, "content": (n.content or "")[:300]})
    except Exception as e:  # noqa: BLE001
        errors.append(f"新闻：{e}")
    new = [m for m in mats if m["key"] not in seen]
    new.sort(key=lambda m: m["date"], reverse=True)
    return {"materials": new[:MAX_NEW], "all_keys": [m["key"] for m in mats], "errors": errors,
            "move": _move(row.symbol)}


def _move(symbol: str) -> dict:
    """当天涨跌（实时行情）与近 5 个交易日涨跌（库里的日线，没有时只看当天）。"""
    out: dict = {}
    q = sv.quotes([symbol]).get(symbol)
    if q:
        out |= {"price": q["price"], "day": q.get("change_pct"), "time": q.get("time")}
    try:
        with get_sessionmaker()() as s:
            bars = sv.bars(s, symbol, "d", "qfq")[-6:]
        if len(bars) >= 2:
            out["five_day"] = bars[-1][2] / bars[0][2] - 1
    except Exception:  # noqa: BLE001
        pass
    return out


def material_text(row: WatchItemRow, info: dict) -> str:
    mv = info["move"]
    lines = [f"### {row.symbol} {row.name}"]
    if row.note:
        lines.append(f"用户备注：{row.note}")
    parts = []
    if mv.get("day") is not None:
        parts.append(f"今日涨跌 {mv['day'] * 100:+.2f}%（{mv.get('time', '')}）")
    if mv.get("five_day") is not None:
        parts.append(f"近 5 个交易日 {mv['five_day'] * 100:+.2f}%")
    lines.append("走势：" + ("；".join(parts) if parts else "无数据"))
    lines.append("新增资料：" if info["materials"] else "新增资料：无")
    for k, m in enumerate(info["materials"], 1):
        extra = f" —— {m['content']}" if m.get("content") else ""
        lines.append(f"[{k}] {m['kind']} {m['date']} {m['title']}{extra}")
    return "\n".join(lines)


def needs_ai(info: dict) -> bool:
    day = info["move"].get("day")
    return bool(info["materials"]) or (day is not None and abs(day) >= BIG_MOVE)


# ---------------------------------------------------------------- AI 检查


def _parse(raw: str) -> list[dict]:
    from app.agent.parse import DecisionParseError

    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        raise DecisionParseError("没有找到 JSON 对象")
    try:
        obj = json.loads(m.group(0))
    except ValueError as e:
        raise DecisionParseError(f"JSON 格式错误：{e}") from None
    evs = obj.get("events") if isinstance(obj, dict) else None
    if not isinstance(evs, list):
        raise DecisionParseError("缺少 events 列表")
    out = []
    for e in evs:
        if not isinstance(e, dict) or not e.get("title"):
            continue
        try:
            level = min(3, max(1, int(e.get("level") or 1)))
        except (TypeError, ValueError):
            level = 1
        d = str(e.get("direction") or "neutral").lower()
        out.append({"level": level, "direction": d if d in DIRECTIONS else "neutral",
                    "title": str(e["title"])[:200], "summary": str(e.get("summary") or "")[:2000],
                    "impact": str(e.get("impact") or "")[:2000],
                    "sources": [int(x) for x in (e.get("sources") or []) if str(x).isdigit()][:10]})
    return out


def _llm(s: Session, uid: int = DEFAULT_USER_ID):
    """(client, model, params, 名称)；该用户没有可用的模型时抛出 WatchError。"""
    from app.arena.strategy import llm_config_for
    from app.llm import create_client
    from app.llm.base import is_local_endpoint
    from app.models import LLMProviderRow

    cfg = settings(s, uid)
    pid = cfg["provider_id"]
    provider = s.get(LLMProviderRow, pid) if pid else None
    if provider is None or provider.user_id != uid:
        raise WatchError("还没有选择监控用的模型（配置中心 → 模型供应商）")
    conf, model, _, _ = llm_config_for(s, pid, cfg["model"])
    params = {"temperature": 0}
    if is_local_endpoint(provider.base_url):
        params["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
    return create_client(conf), model, params, conf


async def analyze(text: str, client, model: str, params: dict, label: str,
                  use_skills: bool = True) -> tuple[list[dict], dict]:
    """调用模型找出事件；有已安装的技能且允许使用时用工具模式。返回（事件列表，{tokens, skill_calls, error}）。"""
    from app.agent.runner import run_llm_json, run_llm_tools
    from app.llm import ToolSpec
    from app.skills import tools as skill_tools

    meta: dict = {"tokens_in": 0, "tokens_out": 0, "skill_calls": []}
    catalog = []
    if use_skills:
        try:
            with get_sessionmaker()() as s:
                catalog = skill_tools.catalog(s)
        except Exception:  # noqa: BLE001
            catalog = []
    out = None
    if catalog:
        submit = ToolSpec("submit_decision", "提交检查结果（没有值得提醒的事件时 events 为空列表）。", SUBMIT_PARAMS)
        system = (SYSTEM + "\n\n" + skill_tools.catalog_text(catalog) + "\n\n查完资料后调用 submit_decision 提交"
                  f"（参数格式与上面的 JSON 相同）。最多调用工具 {MAX_TOOL_CALLS} 次，资料已经足够时不必查询。")

        async def execute(name: str, args: dict) -> str:
            return await skill_tools.execute(name, args, "ai:watch", label)

        out = await run_llm_tools(client, model=model, system=system, user=text, params=params, timeout_s=TIMEOUT_S,
                                  tools=[*skill_tools.specs(catalog), submit], execute=execute, parse=_parse,
                                  max_calls=MAX_TOOL_CALLS)
        meta["skill_calls"] = [{"tool": t["tool"], "args": t["args"]} for t in (out.trace or [])
                               if t["tool"] != "submit_decision"]
        if out.status != "ok":
            meta["tokens_in"] += out.tokens_in
            meta["tokens_out"] += out.tokens_out
            out = None
    if out is None:
        out = await run_llm_json(client, model=model, system=SYSTEM, user=text, params=params, json_mode=True,
                                 timeout_s=TIMEOUT_S, parse=_parse)
    meta["tokens_in"] += out.tokens_in
    meta["tokens_out"] += out.tokens_out
    if out.status != "ok":
        meta["error"] = out.error or out.status
        return [], meta
    return out.parsed, meta


async def check(item_ids: list[int] | None = None, reason: str = "定时", uid: int = DEFAULT_USER_ID) -> dict:
    """检查某个用户的自选股（默认全部开启监控的）。返回 {checked, skipped, events, errors}。"""
    from app.auth import core

    today = (utcnow() + timedelta(hours=8)).date()
    with get_sessionmaker()() as s:
        rows = [r for r in items(s, uid) if r.monitor and (item_ids is None or r.id in item_ids)]
        if not rows:
            return {"checked": 0, "skipped": 0, "events": 0, "errors": []}
        client, model, params, _conf = _llm(s, uid)
        push_level = settings(s, uid)["push_level"]
        ids = [r.id for r in rows]
    use_skills = core.is_admin(uid)
    sem = asyncio.Semaphore(CONCURRENCY)
    summary = {"checked": 0, "skipped": 0, "events": 0, "errors": []}

    async def one(item_id: int) -> None:
        async with sem:
            with get_sessionmaker()() as s:
                row = s.get(WatchItemRow, item_id)
                info = await asyncio.to_thread(collect, row, today)
                text = material_text(row, info)
                symbol, name = row.symbol, row.name
            started = datetime.now()
            result: dict = {"at": utcnow().isoformat(), "reason": reason, "new": len(info["materials"]),
                            "move": info["move"].get("day"), "errors": info["errors"]}
            events: list[dict] = []
            if needs_ai(info):
                events, meta = await analyze(text, client, model, params, f"自选 {symbol}", use_skills)
                result |= {"ai": True, "events": len(events), "tokens_in": meta["tokens_in"],
                           "tokens_out": meta["tokens_out"], "skill_calls": len(meta["skill_calls"]),
                           "seconds": round((datetime.now() - started).total_seconds(), 1)}
                if meta.get("error"):
                    result["error"] = meta["error"]
                    summary["errors"].append(f"{name}：{meta['error']}")
                summary["checked"] += 1
            else:
                result |= {"ai": False, "events": 0}
                summary["skipped"] += 1
            new_ids = []
            with get_sessionmaker()() as s:
                row = s.get(WatchItemRow, item_id)
                if row is None:
                    return
                for ev in events:
                    srcs = [info["materials"][k - 1] for k in ev["sources"] if 0 < k <= len(info["materials"])]
                    e = WatchEventRow(item_id=row.id, symbol=row.symbol, date=today, level=ev["level"],
                                      direction=ev["direction"], title=ev["title"], summary=ev["summary"],
                                      impact=ev["impact"], model=model,
                                      sources=[{"kind": m["kind"], "date": m["date"], "title": m["title"],
                                                "url": m["url"]} for m in srcs])
                    s.add(e)
                    s.flush()
                    if ev["level"] >= push_level:
                        new_ids.append(e.id)
                # AI 调用失败时不标记为已看过，下次重试
                if not result.get("error"):
                    seen = list(dict.fromkeys([*info["all_keys"], *(row.state or {}).get("seen", [])]))[:SEEN_KEEP]
                    row.state = {**(row.state or {}), "seen": seen}
                row.last_check_at = utcnow()
                row.last_result = result
                s.commit()
            summary["events"] += len(events)
            for eid in new_ids:
                await asyncio.to_thread(_notify, eid)

    await asyncio.gather(*(one(i) for i in ids))
    return summary


def notify_message(e: WatchEventRow, name: str, user_id: int = DEFAULT_USER_ID):
    from app.core.config import get_settings
    from app.notify.service import Message

    tag = f"{LEVELS.get(e.level, '')}·{DIRECTIONS.get(e.direction, '')}"
    lines = [f"**{name}（{e.symbol}）** {tag}", "", f"**{e.title}**", "", e.summary]
    if e.impact:
        lines += ["", f"影响：{e.impact}"]
    for src in (e.sources or [])[:3]:
        lines.append(f"- {src.get('kind', '')} {src.get('date', '')} {src.get('title', '')}")
    lines += ["", "以上为 AI 根据公告和新闻整理，不构成投资建议，请以原文为准。"]
    base = get_settings().public_url
    if base:
        lines.append(f"{base.rstrip('/')}/watch")
    return Message("watch", f"watch-event:{e.id}", f"【自选股】{name}：{e.title}", "\n".join(lines),
                   user_id)


def _notify(event_id: int) -> None:
    from app.notify import service as notify

    try:
        with get_sessionmaker()() as s:
            e = s.get(WatchEventRow, event_id)
            item = s.get(WatchItemRow, e.item_id)
            msg = notify_message(e, item.name if item else e.symbol,
                                 item.user_id if item else DEFAULT_USER_ID)    # 只推送给自选股的所有者
        results = notify.deliver(msg)
        if any(r.get("status") == "ok" for r in results):
            with get_sessionmaker()() as s:
                e = s.get(WatchEventRow, event_id)
                e.notified = True
                s.commit()
    except Exception:  # noqa: BLE001 - 推送失败不影响检查
        logger.exception("自选股提醒推送失败")


# ---------------------------------------------------------------- 后台任务


_tasks: dict[int, dict] = {}        # 用户 ID → 后台检查状态


def run_in_background(item_ids: list[int] | None = None, reason: str = "手动", uid: int = DEFAULT_USER_ID) -> dict:
    """在后台检查（页面上的「立即检查」）；每个用户同一时间只运行一个。"""
    task = _tasks.setdefault(uid, {})
    if task.get("running"):
        raise WatchError("正在检查中，请稍候")
    task.clear()
    task.update({"running": True, "started_at": utcnow().isoformat(), "reason": reason})

    async def work():
        try:
            res = await check(item_ids, reason, uid)
            task.update({"running": False, "result": res})
        except Exception as e:  # noqa: BLE001
            logger.exception("自选股检查失败")
            task.update({"running": False, "error": str(e)})

    loop = asyncio.get_running_loop()
    t = loop.create_task(work())
    task["_ref"] = t
    return status(uid)


def status(uid: int = DEFAULT_USER_ID) -> dict:
    return {k: v for k, v in _tasks.get(uid, {}).items() if not k.startswith("_")}


async def scheduled_check() -> None:
    """定时任务：晚上依次检查每个用户的自选股（各用各自的模型）。"""
    with get_sessionmaker()() as s:
        owners = sorted(set(s.scalars(select(WatchItemRow.user_id).where(WatchItemRow.monitor.is_(True)))))
        owners = [u for u in owners if settings(s, u)["enabled"]]
    for uid in owners:
        try:
            res = await check(None, "定时", uid)
            logger.info("自选股检查完成（用户 %s）：%s", uid, res)
        except WatchError as e:
            logger.warning("自选股检查未运行（用户 %s）：%s", uid, e)
