"""每日完整性检查：每个 A 股交易日晚上（westock 补查之后），以及服务中断后重启 40 分钟，检查当天的数据有没有漏：

- 实时跟踪：运行中的实时赛道里，每个账户都有当天的净值（结算完成）；
- AI 复核：开启复核的组合当天的复核没有全部失败；
- westock 快照：开启时当天至少存下九成；
- 每晚 AI 优化：当天没有失败的优化；
- 模型供应商：没有连续失败中的模型；
- 数据库备份：最近 30 小时内备份过；磁盘剩余空间不少于 5% 且不少于 10 GB。

有问题时推送「数据完整性」提醒（同一天同样的问题只推一次），结果显示在系统状态页。
"""

from __future__ import annotations

import hashlib
import logging
import shutil
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.timeutils import to_market_time, utcnow

logger = logging.getLogger(__name__)

DISK_MIN_FREE = 0.05              # 磁盘剩余少于 5% 或少于 10 GB 时提醒（大磁盘按比例太早，小磁盘按容量太晚）
DISK_MIN_GB = 10
BACKUP_MAX_AGE_H = 30
SNAPSHOT_MIN_SHARE = 0.9


def latest_trading_day(s, today: date | None = None) -> date | None:
    from app.models.market import TradingDayRow

    today = today or to_market_time(utcnow(), "CN").date()
    return s.scalar(select(func.max(TradingDayRow.date)).where(TradingDayRow.market == "CN",
                                                                 TradingDayRow.date <= today))


def _tracking(s, day: date) -> list[str]:
    from app.models import AccountRow, NavSnapshotRow, TrackRow

    out = []
    for t in s.scalars(select(TrackRow).where(TrackRow.market == "CN", TrackRow.mode == "live",
                                              TrackRow.status == "running")):
        accs = [a.id for a in s.scalars(select(AccountRow).where(AccountRow.track_id == t.id,
                                                                 AccountRow.status == "active"))]
        if not accs:
            continue
        have = set(s.scalars(select(NavSnapshotRow.account_id).where(NavSnapshotRow.account_id.in_(accs),
                                                                     NavSnapshotRow.date == day)))
        miss = [a for a in accs if a not in have]
        if miss:
            out.append(f"实时跟踪「{t.name}」：账户 {'、'.join(map(str, miss))} 没有 {day} 的净值（结算没有完成）")
    return out


def _reviews(s, day: date) -> list[str]:
    from app.models import DecisionRow, RoundRow
    from app.models.quant import QuantPortfolioRow
    from app.quant.review import is_failed

    out = []
    for pf in s.scalars(select(QuantPortfolioRow).where(QuantPortfolioRow.status == "active")):
        cfg = (pf.settings or {}).get("review") or {}
        if not cfg.get("enabled") or not cfg.get("account_id"):
            continue
        for dec in s.scalars(select(DecisionRow).join(RoundRow, DecisionRow.round_id == RoundRow.id)
                             .where(DecisionRow.account_id == cfg["account_id"], RoundRow.data_date == day)):
            items = ((dec.parsed or {}).get("review") or {}).get("items") or []
            failed = sum(is_failed(it) for it in items)
            if items and failed == len(items):
                out.append(f"AI 复核「{pf.name}」：{len(items)} 只新买入全部复核失败，按规则执行")
            elif failed:
                out.append(f"AI 复核「{pf.name}」：{len(items)} 只中 {failed} 只复核失败，按规则执行")
    return out


def _snapshots(s, day: date) -> list[str]:
    from app.models.research import WestockSnapshotRow
    from app.quant import snapshots

    if snapshots.skill_row() is None:
        return []
    n = s.scalar(select(func.count()).select_from(WestockSnapshotRow).where(WestockSnapshotRow.as_of == day,
                                                                             WestockSnapshotRow.status != "error"))
    try:
        expected = len(snapshots.symbols_of()) * len(snapshots.KINDS)
    except Exception:  # noqa: BLE001 - 股票池读不到时按上次的规模估计
        expected = 600
    if n < expected * SNAPSHOT_MIN_SHARE:
        return [f"westock 快照：{day} 只存下 {n} / {expected} 条（只有当天值，过了今天补不回来）"]
    return []


def _research(s, day: date) -> list[str]:
    from app.models.quant import QuantStrategyRow, ResearchRunRow

    start = datetime.combine(day, datetime.min.time(), tzinfo=UTC) - timedelta(hours=8)   # 北京时间当天 0 点
    out = []
    for run, name in s.execute(select(ResearchRunRow, QuantStrategyRow.name)
                               .join(QuantStrategyRow, QuantStrategyRow.id == ResearchRunRow.strategy_id)
                               .where(ResearchRunRow.status == "failed", ResearchRunRow.created_at >= start,
                                      ResearchRunRow.created_at < start + timedelta(days=1))):
        out.append(f"AI 优化「{name}」失败：{(run.error or '')[:120]}")
    return out


def _models(s) -> list[str]:
    from app.llm import health
    from app.models import LLMProviderRow

    out = []
    for p in s.scalars(select(LLMProviderRow)):
        h = health.get(s, p.id)
        if (h.get("fails") or 0) >= health.FAIL_ALERT:
            out.append(f"模型「{p.name}」连续 {h['fails']} 次调用失败：{(h.get('last_error') or '')[:100]}")
    return out


def _backup_and_disk(s) -> list[str]:
    from app.ops import get_state

    out = []
    b = get_state(s, "backup")
    last = (b.get("last") or {}).get("at")
    if not last or utcnow() - datetime.fromisoformat(last) > timedelta(hours=BACKUP_MAX_AGE_H):
        out.append(f"数据库备份：最近一次成功备份是 {last or '从未'}" + (f"（错误：{b['last_error']}）"
                                                                       if b.get("last_error") else ""))
    out += disk_problems()
    return out


def disk_status() -> dict:
    d = get_settings().data_dir
    u = shutil.disk_usage(d)
    return {"path": str(d), "total": u.total, "free": u.free, "free_share": round(u.free / u.total, 4)}


def disk_problems() -> list[str]:
    st = disk_status()
    if st["free_share"] < DISK_MIN_FREE or st["free"] < DISK_MIN_GB * 2**30:
        return [f"磁盘空间：数据目录所在磁盘只剩 {st['free'] / 2**30:.0f} GB（{st['free_share']:.0%}）"]
    return []


def check(day: date | None = None, notify: bool = True) -> dict:
    """检查 day（默认最近一个 A 股交易日）的数据是否完整。"""
    from app.db.session import get_sessionmaker
    from app.ops import notify_admin, now_iso, update_state

    with get_sessionmaker()() as s:
        day = day or latest_trading_day(s)
        problems: list[str] = []
        sections = [("tracking", _tracking), ("reviews", _reviews), ("snapshots", _snapshots),
                    ("research", _research)] if day else []
        for name, fn in sections:
            try:
                problems += fn(s, day)
            except Exception as e:  # noqa: BLE001 - 单项检查出错不影响其他项
                logger.exception("完整性检查 %s 出错", name)
                problems.append(f"{name} 检查本身出错：{e}")
        for fn in (_models, _backup_and_disk):
            try:
                problems += fn(s)
            except Exception as e:  # noqa: BLE001
                logger.exception("完整性检查出错")
                problems.append(f"检查本身出错：{e}")
    result = {"day": day.isoformat() if day else None, "checked_at": now_iso(), "ok": not problems,
              "problems": problems}
    update_state("integrity", last=result)
    if problems and notify:
        key = f"integrity:{result['day']}:{hashlib.md5('|'.join(problems).encode()).hexdigest()[:10]}"
        notify_admin(key, f"【数据完整性】{result['day']} 有 {len(problems)} 项需要注意",
                     [f"- {p}" for p in problems])
    logger.info("完整性检查 %s：%s", result["day"], "正常" if not problems else "；".join(problems))
    return result


async def scheduled(day: date | None = None) -> None:
    """定时任务：只在 A 股交易日运行（服务中断后的补查不限）。"""
    import asyncio

    from app.db.session import get_sessionmaker

    if day is None:
        with get_sessionmaker()() as s:
            today = to_market_time(utcnow(), "CN").date()
            if latest_trading_day(s, today) != today:
                return
    await asyncio.to_thread(check, day)
