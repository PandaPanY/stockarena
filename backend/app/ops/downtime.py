"""服务停机检测：运行时每分钟写一次心跳文件；启动时读上一次心跳，算出停机时段和其间错过的定时任务。

停机期间错过了任务时推送「服务中断」提醒，说明哪些会由启动补跑补上、哪些补不回来（westock 快照只有当天值），
并在启动 40 分钟后（补跑完成后）对最近一个交易日做一次完整性检查。部署这类一两分钟的重启不会错过任务，不提醒。

外部心跳（配置 heartbeat_url 时）：数据库可用、定时任务在运行时，每 5 分钟访问一次这个地址。整台机器卡死、断网、
服务停了，报到就会中断，由外部服务（healthchecks.io、Uptime Kuma 等）通知；这些情况下平台自己的推送发不出去。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import httpx

from app.core.config import get_settings
from app.core.timeutils import utcnow

logger = logging.getLogger(__name__)

HEARTBEAT_EVERY_S = 60
PING_EVERY_S = 300                 # 外部心跳间隔（外部服务的周期设 5 分钟、宽限 10 分钟左右）
PING_TIMEOUT_S = 10
_last_ping: dict = {}              # 最近一次外部心跳：at、ok、error、ok_at（只在内存里，重启后清空）
CHECK_DELAY_MIN = 40               # 启动后多久做完整性检查（启动补跑一般十几分钟内完成）
# 错过后补不回来、或补跑不覆盖的任务（其余的日终流程由启动补跑处理）
NOT_RECOVERABLE = {"westock_snapshot": "westock 快照（只有当天值，补不回来）",
                   "quant_nightly": "每晚 AI 优化（下一个交易日晚上照常运行）",
                   "watch_daily": "自选股监控（下一次照常运行）",
                   "notify": "调仓提醒（启动补跑完成后可在页面查看当天的调仓清单）",
                   "ops_backup": "数据库备份（下一次照常运行）",
                   "ops_integrity": "每日完整性检查（启动后会单独检查一次）"}


def _file():
    return get_settings().data_dir / "heartbeat"


def read_last() -> datetime | None:
    try:
        return datetime.fromisoformat(_file().read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def beat() -> None:
    try:
        _file().write_text(utcnow().isoformat(timespec="seconds"), encoding="utf-8")
    except OSError:
        logger.warning("写心跳文件失败", exc_info=True)


def ping_host() -> str | None:
    """外部心跳地址的主机名（页面只显示主机名：地址里的 id 相当于密钥）。"""
    url = get_settings().heartbeat_url
    return (urlsplit(url).hostname or "?") if url else None


def ping() -> dict | None:
    """外部心跳报到一次：先确认数据库可用、定时任务在运行，有问题就不报到（让外部服务按超时报警）。"""
    if not get_settings().heartbeat_url:
        return None
    from sqlalchemy import text

    from app.db.session import get_sessionmaker
    from app.scheduler import get_scheduler

    error = None
    try:
        with get_sessionmaker()() as s:
            s.execute(text("select 1"))
    except Exception as e:      # noqa: BLE001 — 任何数据库错误都只是「不报到」
        error = f"数据库不可用，未报到：{e}"
    if error is None and get_settings().auto_run and not get_scheduler().running:
        error = "定时任务没有运行，未报到"
    if error is None:
        try:
            httpx.get(get_settings().heartbeat_url, timeout=PING_TIMEOUT_S).raise_for_status()
        except httpx.HTTPError as e:
            error = f"访问 {ping_host()} 失败：{type(e).__name__} {str(e)[:120]}"
    _last_ping.update(at=utcnow().isoformat(timespec="seconds"), ok=error is None, error=error)
    if error is None:
        _last_ping["ok_at"] = _last_ping["at"]
    else:
        logger.warning("外部心跳：%s", error)
    return dict(_last_ping)


def ping_status() -> dict:
    return {"host": ping_host(), "every_min": PING_EVERY_S // 60, **_last_ping}


async def heartbeat_loop(stop: asyncio.Event) -> None:
    n = 0
    while not stop.is_set():
        beat()
        if n % (PING_EVERY_S // HEARTBEAT_EVERY_S) == 0 and get_settings().heartbeat_url:
            await asyncio.to_thread(ping)
        n += 1
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_EVERY_S)


def missed_jobs(jobs, start: datetime, end: datetime) -> list[tuple[str, str, datetime]]:
    """[(任务 id, 名称, 应运行时间)]：start ～ end 之间应触发的定时任务（只看周期性任务）。"""
    from apscheduler.triggers.cron import CronTrigger

    out = []
    for job in jobs:
        if not isinstance(job.trigger, CronTrigger):
            continue
        t = job.trigger.get_next_fire_time(None, start)
        while t is not None and t <= end and len(out) < 500:
            out.append((job.id, job.name, t))
            t = job.trigger.get_next_fire_time(t, t + timedelta(seconds=1))
    return sorted(out, key=lambda x: x[2])


def on_startup(jobs) -> dict | None:
    """启动时调用（定时任务注册之后）：有停机且错过了任务时记录、推送，并返回停机信息。"""
    from app.ops import notify_admin, update_state

    last, now = read_last(), utcnow()
    beat()
    if last is None:
        return None
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    missed = missed_jobs(jobs, last, now)
    info = {"from": last.isoformat(timespec="seconds"), "to": now.isoformat(timespec="seconds"),
            "minutes": round((now - last).total_seconds() / 60),
            "missed": [{"id": i, "name": n, "at": t.isoformat(timespec="seconds")} for i, n, t in missed]}
    update_state("downtime", last=info)
    if not missed:
        return info
    from app.core.timeutils import to_market_time

    def bj(t: datetime) -> str:
        return to_market_time(t, "CN").strftime("%m-%d %H:%M")

    lost = sorted({v for i, _n, _t in missed for k, v in NOT_RECOVERABLE.items() if i.startswith(k)})
    lines = [f"服务从 {bj(last)} 到 {bj(now)}（北京时间）没有运行，约 {info['minutes'] // 60} 小时 "
             f"{info['minutes'] % 60} 分钟，期间应运行 {len(missed)} 次定时任务：", ""]
    lines += [f"- {bj(t)} {n}" for _i, n, t in missed[:15]]
    if len(missed) > 15:
        lines.append(f"- ……共 {len(missed)} 次")
    lines += ["", "各市场的日终流程（结算、决策）已安排启动补跑。"]
    if lost:
        lines += ["没有补跑的：" + "；".join(lost) + "。"]
    lines.append(f"启动 {CHECK_DELAY_MIN} 分钟后会检查最近一个交易日的数据是否完整，有问题会再推送。")
    notify_admin(f"downtime:{info['from']}", f"【服务中断】停机约 {info['minutes'] // 60} 小时 "
                 f"{info['minutes'] % 60} 分钟，错过 {len(missed)} 次定时任务", lines)
    logger.warning("服务停机 %s ～ %s，错过 %d 次定时任务", info["from"], info["to"], len(missed))
    return info
