"""运行保障：服务停机检测、定时任务运行记录、每日完整性检查、数据库每日备份、磁盘空间。

实时跟踪与 AI 复核评估的数据是不可再生的（错过的交易日、只有当天值的快照补不回来），所以服务器重启、
数据源出错、模型失效、任务失败都要尽快发现：有问题时推送「异常提醒」，系统状态页集中显示。
"""

from __future__ import annotations

from app.core.timeutils import utcnow


def get_state(s, key: str) -> dict:
    from app.models import AppSettingRow

    row = s.get(AppSettingRow, f"ops.{key}")
    return dict(row.value or {}) if row else {}


def set_state(s, key: str, value: dict) -> None:
    from app.models import AppSettingRow

    row = s.get(AppSettingRow, f"ops.{key}") or AppSettingRow(key=f"ops.{key}")
    row.value = value
    s.add(row)


def update_state(key: str, **fields) -> dict:
    """在独立的短事务里合并更新一项运行状态。"""
    from app.db.session import get_sessionmaker

    with get_sessionmaker()() as s:
        cur = get_state(s, key) | fields
        set_state(s, key, cur)
        s.commit()
        return cur


def notify_admin(key: str, title: str, lines: list[str]) -> None:
    """推送给管理员（异常提醒）；推送失败只记日志。"""
    import logging

    from app.notify.service import Message, deliver, page_link

    link = page_link("/system")
    try:
        deliver(Message("errors", key, title, "\n".join(lines + ([f"详情：{link}"] if link else []))))
    except Exception:
        logging.getLogger(__name__).exception("运行保障提醒发送失败：%s", title)


def now_iso() -> str:
    return utcnow().isoformat(timespec="seconds")
