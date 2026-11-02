"""每周点评与偏差分析：程序整理数据，模型写成大白话，保存为 AiNoteRow 并推送。

- 每周点评：每周六 09:00，为每个跟踪中的策略 / 组合写一篇（同一周期只写一次）；推送事件 insight（「AI 点评」）；
- 偏差分析：交易日 21:40，健康度为「提醒」的跟踪账户，分析跟踪表现偏弱的可能原因（每个账户每周最多一次）；
  推送事件 health（与健康度提醒一起）。

用户可以在 AI 助手页关闭其中一项，或手动立即生成。模型失败时也保存一条（status=error），页面上能看到原因。
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, timedelta

from sqlalchemy import select

from app.core.timeutils import utcnow
from app.db.session import get_sessionmaker
from app.insight import data, llm
from app.models.insight import AiNoteRow
from app.models.quant import QuantPortfolioRow, QuantStrategyRow

logger = logging.getLogger(__name__)

WEEK_DAYS = 5
DEVIATION_DAYS = 60
DEVIATION_EVERY_DAYS = 7

_RULES = """- 用简体中文、大白话，面向不熟悉量化的普通投资者；Markdown 格式。
- 只根据给出的数据说话，引用具体数字；不要编造原因，
  数据里看不出原因时直接说「从数据里看不出原因」。
- 这些是规则化的量化策略，买卖由规则自动决定：解释时说明是规则的结果（排名变化、调仓日、择时条件等），
  不要说成人的主观判断。
- 不给买卖建议，不预测后市，不评价具体股票值不值得买。"""

SYSTEM_WEEKLY = f"""你是量化投资研究助理，为用户写实时跟踪中的策略或组合的每周点评。

要求：
{_RULES}
- 300 字以内，用三个小标题：**本周表现**（与基准比）、**主要贡献与交易**、
  **需要留意**（健康度、回撤等；没有就说一切正常）。"""

SYSTEM_DEVIATION = f"""你是量化投资研究助理。程序的健康度检查发现，
某个实时跟踪的策略或组合最近的表现明显差于它的回测历史。
请根据数据分析可能的原因，帮助用户判断这是正常波动还是策略可能失效。

要求：
{_RULES}
- 400 字以内，用三个小标题：
  **偏弱到什么程度**；
  **可能的原因**（逐条列出，每条说明依据：同期基准是否也在跌、亏损是否集中在少数持仓、
  是否集中在某个成员策略、对账是否有执行偏差等）；
  **怎么看**（只说「继续观察 / 建议复查策略」这类结论，不给买卖建议）。"""


def _ref_row(s, ref: dict):
    return s.get(QuantPortfolioRow if ref["kind"] == "portfolio" else QuantStrategyRow, ref["id"])


def material(s, ref: dict, days: int) -> dict | None:
    """交给模型的数据；跟踪不足两个交易日时返回 None。"""
    acc = ref["account_id"]
    perf = data.performance(s, acc, days)
    if not perf.get("days"):
        return None
    start, end = date.fromisoformat(perf["start"]), date.fromisoformat(perf["end"])
    hold = data.holdings(s, acc, limit=10)
    health = (_ref_row(s, ref).readiness or {}).get("health") or {}
    return {
        "name": ref["name"], "type": "多风格组合" if ref["kind"] == "portfolio" else "单个策略",
        "members": data.members(s, ref), "period": perf,
        "since_start": data.performance(s, acc), "contributions": data.contributions(s, acc, start, end),
        "trades": data.trades(s, acc, start, end, limit=10),
        "holdings": {k: hold[k] for k in ("as_of", "nav", "cash_weight", "count", "positions")},
        "health": {k: health.get(k) for k in ("status", "message", "checks")} if health else None,
    }


def _user_text(m: dict, what: str) -> str:
    return (f"{what}。收益、回撤、权重、贡献都是小数（0.012 = 1.2%）；contribution 是该标的对整个账户净值的贡献；"
            "period 是本次分析的区间，since_start 是跟踪以来。\n\n" + json.dumps(m, ensure_ascii=False, indent=1))


async def write_note(uid: int, ref: dict, kind: str) -> int | None:
    """为一个跟踪中的策略 / 组合写一篇点评（weekly）或偏差分析（deviation）。返回 AiNoteRow.id；没有数据时 None。"""
    days = WEEK_DAYS if kind == "weekly" else DEVIATION_DAYS
    with get_sessionmaker()() as s:
        m = material(s, ref, days)
        if m is None:
            return None
        client, model, params = llm.client_for(s, uid)
    if kind == "weekly":
        out = await llm.write(client, model, params, SYSTEM_WEEKLY,
                              _user_text(m, f"请为「{ref['name']}」写本周点评"))
    else:
        out = await llm.write(client, model, params, SYSTEM_DEVIATION,
                              _user_text(m, f"「{ref['name']}」的健康度为「提醒」，请分析可能的原因"))
    with get_sessionmaker()() as s:
        row = AiNoteRow(user_id=uid, kind=kind, ref_kind=ref["kind"], ref_id=ref["id"], ref_name=ref["name"][:64],
                        account_id=ref["account_id"], period_start=date.fromisoformat(m["period"]["start"]),
                        period_end=date.fromisoformat(m["period"]["end"]), content=out["content"], material=m,
                        status=out["status"], error=out["error"], model=out["model"],
                        tokens_in=out["tokens_in"], tokens_out=out["tokens_out"])
        s.add(row)
        s.commit()
        note_id = row.id
    if out["status"] == "ok":
        _push(uid, ref, kind, m, out["content"])
    else:
        logger.warning("AI %s 失败：%s：%s", kind, ref["name"], out["error"])
    return note_id


def _push(uid: int, ref: dict, kind: str, m: dict, content: str) -> None:
    from app.notify import service as notify

    p = m["period"]
    link = notify.page_link("/insight")
    body = content + (f"\n\n详情：{link}" if link else "")
    if kind == "weekly":
        ret = p.get("return")
        title = f"【每周点评】{ref['name']}" + (f"：本周 {ret:+.1%}" if ret is not None else "")
        msg = notify.Message("insight", f"weekly:{ref['kind']}:{ref['id']}:{p['end']}", title, body, user_id=uid)
    else:
        year, week, _ = date.fromisoformat(p["end"]).isocalendar()
        msg = notify.Message("health", f"deviation:{ref['kind']}:{ref['id']}:{year}-W{week:02d}",
                             f"【AI 分析】{ref['name']}：跟踪表现偏弱的可能原因", body, user_id=uid)
    try:
        notify.deliver(msg)
    except Exception:
        logger.exception("AI 点评推送失败")


# ---------------------------------------------------------------- 定时任务


def _users() -> list[int]:
    with get_sessionmaker()() as s:
        ids = set(s.scalars(select(QuantStrategyRow.user_id).where(QuantStrategyRow.follow_account_id.is_not(None))))
        ids |= set(s.scalars(select(QuantPortfolioRow.user_id).where(QuantPortfolioRow.follow_account_id.is_not(None))))
    return sorted(ids)


def _done(uid: int, ref: dict, kind: str, since) -> bool:
    with get_sessionmaker()() as s:
        return s.scalars(select(AiNoteRow.id).where(
            AiNoteRow.user_id == uid, AiNoteRow.kind == kind, AiNoteRow.ref_kind == ref["kind"],
            AiNoteRow.ref_id == ref["id"], AiNoteRow.status == "ok", since)).first() is not None


async def run_weekly(uid: int, ref_ids: set[tuple[str, int]] | None = None, force: bool = False) -> dict:
    """为一个用户的跟踪中的策略 / 组合写每周点评。同一个区间（截止日相同）已写过的跳过，除非 force。"""
    with get_sessionmaker()() as s:
        refs = data.followed(s, uid)
    done, skipped, errors = [], [], []
    for ref in refs:
        if ref_ids is not None and (ref["kind"], ref["id"]) not in ref_ids:
            continue
        with get_sessionmaker()() as s:
            end = data.performance(s, ref["account_id"], WEEK_DAYS).get("end")
        if not force and end and _done(uid, ref, "weekly", AiNoteRow.period_end == date.fromisoformat(end)):
            skipped.append(ref["name"])
            continue
        try:
            nid = await write_note(uid, ref, "weekly")
            (done if nid else skipped).append(ref["name"])
        except data.InsightError as e:
            errors.append(f"{ref['name']}：{e}")
            break                           # 没有可用的模型：后面的也不会成功
        except Exception as e:  # noqa: BLE001
            logger.exception("每周点评失败：%s", ref["name"])
            errors.append(f"{ref['name']}：{type(e).__name__}")
    return {"done": done, "skipped": skipped, "errors": errors}


async def run_deviation(uid: int) -> dict:
    """健康度为「提醒」的跟踪账户写偏差分析（每个账户 DEVIATION_EVERY_DAYS 天内最多一次）。"""
    with get_sessionmaker()() as s:
        refs = [r for r in data.followed(s, uid) if r["health"] == "提醒"]
    since = utcnow() - timedelta(days=DEVIATION_EVERY_DAYS)
    done, errors = [], []
    for ref in refs:
        if _done(uid, ref, "deviation", AiNoteRow.created_at >= since):
            continue
        try:
            if await write_note(uid, ref, "deviation"):
                done.append(ref["name"])
        except data.InsightError as e:
            errors.append(f"{ref['name']}：{e}")
            break
        except Exception as e:  # noqa: BLE001
            logger.exception("偏差分析失败：%s", ref["name"])
            errors.append(f"{ref['name']}：{type(e).__name__}")
    return {"done": done, "errors": errors}


async def scheduled(kind: str) -> None:
    """定时任务入口：所有有跟踪账户、且没有关闭该项的用户。"""
    for uid in _users():
        with get_sessionmaker()() as s:
            cfg = llm.settings(s, uid)
        if not cfg["weekly" if kind == "weekly" else "deviation"] or not cfg["provider_id"]:
            continue
        out = await (run_weekly(uid) if kind == "weekly" else run_deviation(uid))
        if out.get("done") or out.get("errors"):
            logger.info("AI %s（用户 %s）：%s", kind, uid, out)


_running: set[int] = set()


def run_in_background(uid: int, kind: str, ref: tuple[str, int] | None = None) -> dict:
    """页面上的「立即生成」：后台运行，同一个用户同时只运行一个。"""
    if uid in _running:
        return {"started": False, "message": "上一次生成还没有完成"}
    _running.add(uid)

    async def job():
        try:
            if kind == "weekly":
                await run_weekly(uid, {ref} if ref else None, force=True)
            elif ref:                       # 指定账户：不论健康度如何都分析
                with get_sessionmaker()() as s:
                    target = [r for r in data.followed(s, uid) if (r["kind"], r["id"]) == ref]
                for r in target:
                    await write_note(uid, r, "deviation")
            else:
                await run_deviation(uid)
        except Exception:
            logger.exception("手动生成 AI %s 失败", kind)
        finally:
            _running.discard(uid)

    asyncio.get_running_loop().create_task(job())
    return {"started": True, "message": "已开始生成，完成后在本页查看"}
