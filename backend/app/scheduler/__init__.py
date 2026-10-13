"""调度器：按各市场的收盘时间自动执行运行中赛道的日终流程。"""

from __future__ import annotations

from datetime import timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from app.core.timeutils import utcnow
from app.market.markets import MARKETS

_scheduler: AsyncIOScheduler | None = None


def get_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(timezone="UTC")
    return _scheduler


async def run_market(market: str) -> None:
    from app.arena.cycle import run_market_tracks

    await run_market_tracks(market)
    if market == "CN":
        import asyncio

        from app.quant.tasks import warm_caches

        await asyncio.to_thread(warm_caches)        # 当日行情更新后重算页面常用的回测结果（未变化时直接命中缓存）


async def run_all_markets() -> None:
    for code in MARKETS:
        await run_market(code)


async def run_notify(final: bool = False) -> None:
    import asyncio

    from app.notify.service import daily_check

    await asyncio.to_thread(daily_check, final)


async def run_watch() -> None:
    from app.watch.service import scheduled_check

    await scheduled_check()


async def run_quant_nightly() -> None:
    from app.quant.tasks import nightly

    await nightly()


def register_jobs(startup_catch_up_delay_s: int = 20) -> None:
    """收盘后多次尝试：数据源的当日日线可能延迟发布；流程幂等，已完成的步骤会跳过。"""
    scheduler = get_scheduler()
    for spec in MARKETS.values():
        for hour, minute in spec.run_times:
            scheduler.add_job(
                run_market, CronTrigger(day_of_week="mon-fri", hour=hour, minute=minute, timezone=spec.tz),
                args=[spec.code], id=f"{spec.code.lower()}_daily_{hour:02d}{minute:02d}",
                name=f"{spec.name}日终流程 {hour:02d}:{minute:02d}（{spec.tz}）",
                replace_existing=True, misfire_grace_time=600, coalesce=True, max_instances=1,
            )
    # 策略工坊：每个交易日晚上更新行情并自动优化（A股收盘、日线发布之后）
    nightly = CronTrigger(day_of_week="mon-fri", hour=19, minute=30, timezone="Asia/Shanghai")
    scheduler.add_job(run_quant_nightly, nightly,
                      id="quant_nightly", name="策略工坊每晚优化 19:30（Asia/Shanghai）", replace_existing=True,
                      misfire_grace_time=3600, coalesce=True, max_instances=1)
    # 调仓提醒：A股日终流程（15:45 起多次尝试）之后检查并推送；同一天的提醒只发一次，21:00 那次检查未完成的决策
    for hour, minute, final in ((16, 20, False), (17, 0, False), (18, 40, False), (21, 0, True)):
        scheduler.add_job(run_notify, CronTrigger(day_of_week="mon-fri", hour=hour, minute=minute,
                                                  timezone="Asia/Shanghai"),
                          args=[final], id=f"notify_{hour:02d}{minute:02d}",
                          name=f"调仓提醒检查 {hour:02d}:{minute:02d}（Asia/Shanghai）", replace_existing=True,
                          misfire_grace_time=1800, coalesce=True, max_instances=1)
    # 自选股监控：每天晚上（含周末，公告和新闻不只在交易日发布），排在 A股日终流程与 AI 复核之后
    scheduler.add_job(run_watch, CronTrigger(hour=21, minute=15, timezone="Asia/Shanghai"),
                      id="watch_daily", name="自选股监控 21:15（Asia/Shanghai）", replace_existing=True,
                      misfire_grace_time=3600, coalesce=True, max_instances=1)
    # 启动后补跑一次：处理停机期间错过的交易日
    scheduler.add_job(run_all_markets, DateTrigger(run_date=utcnow() + timedelta(seconds=startup_catch_up_delay_s)),
                      id="startup_catch_up", name="启动补跑（全部市场）", replace_existing=True)


def start_scheduler() -> None:
    scheduler = get_scheduler()
    if not scheduler.running:
        scheduler.start()


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
    _scheduler = None
