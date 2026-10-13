"""后台任务：在请求之外执行赛道日终流程，并记录最近一次执行结果供界面查询。"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from app.arena import cycle, strategy
from app.core.timeutils import utcnow

logger = logging.getLogger(__name__)

_tasks: set[asyncio.Task] = set()
last_result: dict[int, dict] = {}


def is_busy(track_id: int) -> bool:
    """赛道正在执行日终流程或历史回放。"""
    lock = cycle._locks.get(track_id)
    return (bool(lock and lock.locked()) or track_id in cycle._replaying
            or bool(last_result.get(track_id, {}).get("running")))


async def _run(track_id: int, trigger: str, force_round: bool) -> None:
    started: datetime = utcnow()
    last_result[track_id] = {"running": True, "started_at": started.isoformat(), "message": "执行中…"}
    msg = await cycle.run_track_cycle(track_id, trigger=trigger, force_round=force_round)
    last_result[track_id] = {"running": False, "started_at": started.isoformat(),
                             "finished_at": utcnow().isoformat(), "message": msg}
    logger.info("赛道 %s（%s）：%s", track_id, trigger, msg)


async def _run_replay(track_id: int) -> None:
    started = utcnow().isoformat()

    def progress(message: str) -> None:
        last_result[track_id] = {"running": True, "started_at": started, "message": message}

    progress("历史回放准备中（预取行情）…")
    msg = await cycle.run_replay(track_id, progress)
    last_result[track_id] = {"running": False, "started_at": started, "finished_at": utcnow().isoformat(),
                             "message": msg}
    logger.info("赛道 %s（回放）：%s", track_id, msg)


def spawn_replay(track_id: int) -> bool:
    if is_busy(track_id):
        return False
    _keep(asyncio.get_running_loop().create_task(_run_replay(track_id)))
    return True


def _keep(task: asyncio.Task) -> None:
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


reflect_status: dict[int, dict] = {}    # 账户 → 手动复盘状态


async def _run_reflect(track_id: int, account_id: int) -> None:
    reflect_status[account_id] = {"running": True, "message": "复盘中…"}
    try:
        async with cycle._lock(track_id):
            msg = await strategy.reflect_now(account_id)
    except Exception as e:
        logger.exception("账户 %s 手动复盘失败", account_id)
        msg = f"复盘失败：{e}"
    reflect_status[account_id] = {"running": False, "message": msg, "finished_at": utcnow().isoformat()}
    logger.info("账户 %s（手动复盘）：%s", account_id, msg)


def spawn_reflect(track_id: int, account_id: int) -> bool:
    """手动复盘；该账户正在复盘时返回 False。赛道正在执行日终流程时会排队等待。"""
    if reflect_status.get(account_id, {}).get("running"):
        return False
    reflect_status[account_id] = {"running": True, "message": "等待赛道空闲…"}
    _keep(asyncio.get_running_loop().create_task(_run_reflect(track_id, account_id)))
    return True


consistency_status: dict[int, dict] = {}    # 赛道 → 一致性评估状态


async def _run_consistency(track_id: int, provider_id: int, model: str | None, sample: int) -> None:
    from app.arena import consistency

    consistency_status[track_id] = {"running": True, "message": "评估中…"}
    try:
        msg = await consistency.run_checks(track_id, provider_id, model, sample)
    except Exception as e:
        logger.exception("赛道 %s 一致性评估失败", track_id)
        msg = f"评估失败：{e}"
    consistency_status[track_id] = {"running": False, "message": msg, "finished_at": utcnow().isoformat()}


def spawn_consistency(track_id: int, provider_id: int, model: str | None, sample: int) -> bool:
    if consistency_status.get(track_id, {}).get("running"):
        return False
    consistency_status[track_id] = {"running": True, "message": "准备中…"}
    _keep(asyncio.get_running_loop().create_task(_run_consistency(track_id, provider_id, model, sample)))
    return True


def spawn_experiment(exp_id: int) -> None:
    from app.arena import experiment

    async def _go() -> None:
        msg = await experiment.run(exp_id)
        logger.info("进化实验 %s：%s", exp_id, msg)

    _keep(asyncio.get_running_loop().create_task(_go()))


def spawn_cycle(track_id: int, trigger: str = "manual", force_round: bool = False) -> bool:
    """启动后台执行；赛道已在执行中时返回 False。"""
    if is_busy(track_id):
        return False
    _keep(asyncio.get_running_loop().create_task(_run(track_id, trigger, force_round)))
    return True
