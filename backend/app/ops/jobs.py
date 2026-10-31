"""定时任务运行记录：每个任务最近一次的开始、结束、耗时与结果；任务抛出异常或被错过时推送提醒。

注意：多数任务在内部捕获了自己的错误（例如数据源失败会重试、单个赛道失败不影响其他赛道），
这里记录的「成功」只表示任务跑完了；具体数据是否完整由每日完整性检查（app/ops/integrity.py）判断。
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime

from app.core.timeutils import utcnow

logger = logging.getLogger(__name__)

_started: dict[str, datetime] = {}
_lock = threading.Lock()


def _record(job_id: str, name: str, status: str, error: str | None = None) -> None:
    from app.ops import get_state, notify_admin, set_state

    end = utcnow()
    with _lock:
        start = _started.pop(job_id, None)
    entry = {"name": name, "status": status, "finished_at": end.isoformat(timespec="seconds"),
             "started_at": start.isoformat(timespec="seconds") if start else None,
             "seconds": round((end - start).total_seconds()) if start else None, "error": (error or "")[:500] or None}
    from app.db.session import get_sessionmaker

    with get_sessionmaker()() as s:
        cur = get_state(s, "jobs")
        cur[job_id] = entry
        set_state(s, "jobs", cur)
        s.commit()
    if status != "ok":
        what = f"运行出错：{(error or '')[:300]}" if status == "error" else "没有按时运行（服务繁忙或停机）"
        notify_admin(f"job:{job_id}:{entry['finished_at'][:13]}",
                     f"【定时任务{'出错' if status == 'error' else '被错过'}】{name}",
                     [f"**{name}** {what}", "", "详细错误见服务日志（data/logs/）。"])


def listener(event) -> None:
    from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MISSED, EVENT_JOB_SUBMITTED

    from app.scheduler import get_scheduler

    job = get_scheduler().get_job(event.job_id)
    name = job.name if job else event.job_id
    try:
        if event.code == EVENT_JOB_SUBMITTED:
            with _lock:
                _started[event.job_id] = utcnow()
            return
        if event.code == EVENT_JOB_ERROR:
            _record(event.job_id, name, "error", f"{type(event.exception).__name__}: {event.exception}")
        elif event.code == EVENT_JOB_MISSED:
            _record(event.job_id, name, "missed")
        else:
            _record(event.job_id, name, "ok")
    except Exception:
        logger.exception("记录定时任务运行结果失败：%s", event.job_id)


def install() -> None:
    from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, EVENT_JOB_MISSED, EVENT_JOB_SUBMITTED

    from app.scheduler import get_scheduler

    get_scheduler().add_listener(listener, EVENT_JOB_SUBMITTED | EVENT_JOB_EXECUTED | EVENT_JOB_ERROR
                                 | EVENT_JOB_MISSED)


def recent(s) -> dict:
    from app.ops import get_state

    return get_state(s, "jobs")

