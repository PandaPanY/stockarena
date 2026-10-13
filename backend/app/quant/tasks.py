"""策略工坊后台任务：数据准备、创建初始策略库、AI 研究、每晚自动优化。"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import date

from sqlalchemy import select

from app.core.timeutils import utcnow
from app.db.session import get_sessionmaker
from app.models.quant import QuantStrategyRow, ResearchRunRow
from app.quant import data, research, service

logger = logging.getLogger(__name__)

_tasks: set[asyncio.Task] = set()
status: dict[str, dict] = {}        # 任务名 → {running, message, finished_at}


def _keep(task: asyncio.Task) -> None:
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def busy(name: str) -> bool:
    return bool(status.get(name, {}).get("running"))


def _spawn(name: str, coro_fn) -> bool:
    if busy(name):
        return False
    status[name] = {"running": True, "message": "开始…", "started_at": utcnow().isoformat()}

    async def wrapper():
        try:
            msg = await coro_fn()
        except Exception as e:
            logger.exception("策略工坊任务 %s 失败", name)
            msg = f"失败：{e}"
        status[name] = {**status.get(name, {}), "running": False, "message": msg,
                        "finished_at": utcnow().isoformat()}

    _keep(asyncio.get_running_loop().create_task(wrapper()))
    return True


def _progress(name: str):
    def say(msg: str) -> None:
        status[name] = {**status.get(name, {}), "running": True, "message": msg}
    return say


def spawn_prepare(universe_name: str, train_start) -> bool:
    name = f"prepare:{universe_name}"

    async def run():
        r = await asyncio.to_thread(data.prepare, universe_name, data.data_start(train_start), _progress(name))
        return r["message"]

    return _spawn(name, run)


def spawn_quick_start(train_start) -> bool:
    """快速开始：先准备快速体验股票池（约 60 个标的，几分钟），完成后自动在后台准备完整的全时点股票池。"""
    name = f"prepare:{service.QUICK_UNIVERSE}"

    async def run():
        r = await asyncio.to_thread(data.prepare, service.QUICK_UNIVERSE, data.data_start(train_start),
                                    _progress(name))
        started = spawn_prepare(service.DEFAULT_UNIVERSE, train_start)
        return r["message"] + ("；完整数据已在后台开始准备" if started else "")

    return _spawn(name, run)


def spawn_seed(provider_id: int | None, model: str | None, styles: list[str] | None) -> bool:
    async def run():
        def work():
            with get_sessionmaker()() as s:
                created = service.seed_library(s, provider_id, model, styles)
                return f"已创建 {len(created)} 条策略：" + "、".join(c.name for c in created) if created \
                    else "没有需要创建的策略（各风格已存在）"
        _progress("seed")("回测初始策略中（首次需要读取多年行情，约 1～2 分钟）…")
        return await asyncio.to_thread(work)

    return _spawn("seed", run)


def spawn_research(run_id: int) -> None:
    async def run():
        msg = await research.run_research(run_id)
        logger.info("研究任务 %s：%s", run_id, msg)

    _keep(asyncio.get_running_loop().create_task(run()))


async def nightly() -> None:
    """每个交易日晚上：更新各股票池行情、扫描数据质量，刷新跟踪中策略与组合的「可实盘」检查，然后依次优化开启了自动优化的策略。"""
    with get_sessionmaker()() as s:
        strategies = list(s.scalars(select(QuantStrategyRow).where(QuantStrategyRow.status == "active")))
        universes = {(st.universe_name, service.settings_of(st).train_start) for st in strategies}
    for universe, start in universes:
        try:
            await asyncio.to_thread(data.prepare, universe, data.data_start(start))
        except Exception:
            logger.exception("每晚数据更新失败：%s", universe)
    try:
        await asyncio.to_thread(_scan_quality)
    except Exception:
        logger.exception("数据质量扫描失败")
    await asyncio.to_thread(refresh_readiness)
    await asyncio.to_thread(warm_caches)
    run_ids = []
    with get_sessionmaker()() as s:
        for st in s.scalars(select(QuantStrategyRow).where(QuantStrategyRow.status == "active",
                                                          QuantStrategyRow.auto_optimize.is_(True))):
            try:
                run_ids.append(research.start_run(s, st, trigger="schedule").id)
            except service.LabError as e:
                logger.info("策略 %s 跳过自动优化：%s", st.name, e)
        s.commit()
    for rid in run_ids:
        await research.run_research(rid)


_warm_lock = threading.Lock()


def warm_caches() -> None:
    """预先算好页面上常用的回测结果：因子研究页默认股票池的开发期 / 留出期成绩单、各组合的报告。

    行情每天更新后这些缓存随之失效，不预热的话当天第一次打开要等十几秒。已经算过的直接命中缓存，很快。
    启动时、每次 A股日终流程之后、每晚数据更新之后调用；同一时间只跑一个。
    """
    from app.models.quant import QuantPortfolioRow
    from app.quant import costs, factor_research, portfolio

    with _warm_lock:
        cfg = service.LabSettings()
        t0 = time.monotonic()
        done = []
        try:
            with get_sessionmaker()() as s:
                panel = data.get_panel(s, service.DEFAULT_UNIVERSE, cfg.train_start, date.today())
                for a, b in ((cfg.train_start, cfg.valid_end), (cfg.holdout_start, panel.dates[-1])):
                    factor_research.scorecard(panel, a, b, factor_research.default_factors([]))
                done.append("因子研究")
                for pf in s.scalars(select(QuantPortfolioRow).order_by(QuantPortfolioRow.id)):
                    try:
                        with costs.use(pf.user_id):            # 按组合所有者的交易成本
                            portfolio.evaluate_portfolio(s, pf)
                        done.append(pf.name)
                    except Exception:  # noqa: BLE001  个别组合失败（如成员不足）不影响其他
                        logger.warning("组合预热失败：%s", pf.name, exc_info=True)
        except Exception:
            logger.exception("预热失败")
        if done:
            logger.info("已预热：%s（%.0f 秒）", "、".join(done), time.monotonic() - t0)


REEVALUATE = "reevaluate"


def reevaluate_task(uid: int | None) -> str:
    """重新回测任务名：某个用户（修改交易成本后）为 reevaluate@uid；多个或全部用户（数据修复、启动检查）为 reevaluate。
    """
    return REEVALUATE if uid is None else f"{REEVALUATE}@{uid}"


def reevaluate_all(say=None, uids: set[int] | None = None) -> str:
    """重新回测在用策略的全部版本（uids 给出时只回测这些用户的策略），再刷新「可实盘」检查。

    每个策略按它的所有者的交易成本设置回测；逐个策略先算后写（短事务）；
    运行期间成本又被修改时，结束后按新设置再跑一遍。
    """
    from app.quant import costs

    done = 0
    while True:
        with get_sessionmaker()() as s:
            q = select(QuantStrategyRow).where(QuantStrategyRow.status == "active").order_by(QuantStrategyRow.id)
            if uids is not None:
                q = q.where(QuantStrategyRow.user_id.in_(uids))
            rows = [(st.id, st.name, st.user_id) for st in s.scalars(q)]
        sigs = {u: costs.for_user(u).signature() for u in {r[2] for r in rows} | (uids or set())}
        for k, (sid, name, owner) in enumerate(rows, 1):
            if say:
                say(f"重新回测 {k}/{len(rows)}：{name}")
            try:
                with costs.use(owner), get_sessionmaker()() as s:
                    done += service.reevaluate(s, s.get(QuantStrategyRow, sid))
                    s.commit()
            except Exception:
                logger.exception("重新回测失败：%s", name)
        if say:
            say("刷新「可实盘」检查…")
        refresh_readiness(uids)
        with get_sessionmaker()() as s:
            for u, sig in sigs.items():
                costs.mark_evaluated(s, sig, u)
            s.commit()
        if all(costs.for_user(u).signature() == sig for u, sig in sigs.items()):
            break
    msg = (f"已按「{costs.for_user(next(iter(sigs))).describe()}」重新回测 {done} 个版本" if len(sigs) == 1
           else f"已按各用户的交易成本设置重新回测 {done} 个版本")
    logger.info(msg)
    return msg


def spawn_reevaluate(rescan: bool = False, uid: int | None = None, uids: set[int] | None = None) -> bool:
    """后台重新回测：uid 给出时只回测该用户的策略（修改交易成本后）；
    rescan=True（数据修复后）时完成后重新扫描数据质量。
    """
    name = reevaluate_task(uid)
    targets = {uid} if uid is not None else uids

    async def run():
        msg = await asyncio.to_thread(reevaluate_all, _progress(name), targets)
        if rescan:
            await asyncio.to_thread(_scan_quality, False)
        return msg

    return _spawn(name, run)


QUALITY = "quality"


def _scan_quality(notify: bool = True) -> str:
    from app.quant import quality

    res = quality.run_and_save(notify)
    parts = []
    for name, u in res["universes"].items():
        c = u.get("counts") or {}
        parts.append(f"{name}：错误 {c.get('error', 0)}、提示 {c.get('warn', 0)}、参考 {c.get('info', 0)}"
                     if "counts" in u else f"{name}：{u.get('error')}")
    return "扫描完成。" + "；".join(parts)


def spawn_quality() -> bool:
    async def run():
        _progress(QUALITY)("扫描中…")
        return await asyncio.to_thread(_scan_quality, False)

    return _spawn(QUALITY, run)


def check_costs_on_startup() -> None:
    """启动时读取交易成本设置；各用户与上次重新回测时的设置（或成本计算口径）不同则在后台重新回测这些用户的策略。"""
    from app.models import DEFAULT_USER_ID
    from app.quant import costs

    stale: set[int] = set()
    with get_sessionmaker()() as s:
        owners = set(s.scalars(select(QuantStrategyRow.user_id).where(QuantStrategyRow.status == "active")))
        for uid in owners | {DEFAULT_USER_ID}:
            current = costs.load(s, uid)
            evaluated = costs.evaluated_signature(s, uid)
            if uid not in owners:
                if evaluated is None:
                    costs.mark_evaluated(s, current.signature(), uid)     # 没有需要重算的版本
            elif evaluated != current.signature():
                stale.add(uid)
        s.commit()
    if stale:
        logger.info("交易成本设置或计算口径有变化，后台重新回测这些用户的策略：%s", sorted(stale))
        spawn_reevaluate(uids=stale)


def refresh_readiness(uids: set[int] | None = None) -> None:
    """刷新正在实时跟踪的策略与组合的「可实盘」检查（先算后写，逐个短事务保存；按所有者的交易成本计算）。"""
    from app.models.quant import QuantPortfolioRow
    from app.quant import costs, readiness

    with get_sessionmaker()() as s:
        refs = [("strategy", st.id, st.user_id) for st in s.scalars(select(QuantStrategyRow).where(
            QuantStrategyRow.status == "active", QuantStrategyRow.follow_account_id.is_not(None)))]
        refs += [("portfolio", pf.id, pf.user_id) for pf in s.scalars(select(QuantPortfolioRow).where(
            QuantPortfolioRow.status == "active", QuantPortfolioRow.follow_account_id.is_not(None)))]
    for kind, ref_id, owner in refs:
        if uids is not None and owner not in uids:
            continue
        try:
            with costs.use(owner), get_sessionmaker()() as s:
                out = readiness.check(s, kind, ref_id)
            with get_sessionmaker()() as s:
                row = s.get(QuantStrategyRow if kind == "strategy" else QuantPortfolioRow, ref_id)
                prev, name = row.readiness, row.name
                row.readiness = out
                s.commit()
        except Exception:
            logger.exception("「可实盘」检查失败：%s %s", kind, ref_id)
            continue
        try:
            from app.notify import service as notify

            for msg in notify.readiness_messages(kind, name, ref_id, prev, out):
                msg.user_id = owner                 # 发给策略 / 组合的所有者
                notify.deliver(msg)
        except Exception:
            logger.exception("健康度推送失败：%s %s", kind, ref_id)


def recover() -> None:
    """启动时把上次未完成的研究标记为中断。"""
    with get_sessionmaker()() as s:
        for run in s.scalars(select(ResearchRunRow).where(ResearchRunRow.status.in_(("pending", "running")))):
            run.status, run.error, run.progress = "failed", "后端重启，任务中断", "中断"
        s.commit()
