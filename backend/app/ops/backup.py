"""数据库每日备份：用 SQLite 在线备份接口得到一致的副本，检查完整性后压缩保存，保留最近 backup_keep 份。

配置 backup_dir 时再复制一份到那里（建议是另一块硬盘、NAS 挂载目录或同步盘），服务器磁盘坏了也不至于全丢。
跟踪账户、AI 复核评估、westock 快照这些数据是不可再生的，部署时那一次备份不够。
"""

from __future__ import annotations

import gzip
import logging
import shutil
import sqlite3
import time
from pathlib import Path

from app.core.config import get_settings
from app.core.timeutils import to_market_time, utcnow

logger = logging.getLogger(__name__)

PREFIX = "stockarena-"
SUFFIX = ".db.gz"


class BackupError(RuntimeError):
    pass


def db_path() -> Path | None:
    url = get_settings().resolved_database_url
    return Path(url.removeprefix("sqlite:///")) if url.startswith("sqlite:///") else None


def local_dir() -> Path:
    return get_settings().data_dir / "backup" / "daily"


def _prune(folder: Path, keep: int) -> list[str]:
    files = sorted(folder.glob(f"{PREFIX}*{SUFFIX}"))
    removed = []
    for f in files[:-keep] if keep > 0 else []:
        f.unlink(missing_ok=True)
        removed.append(f.name)
    return removed


def run_backup() -> dict:
    """备份一次（同步，在线程里调用）。返回 {file, size, seconds, extra}；失败时抛 BackupError。"""
    settings = get_settings()
    src = db_path()
    if src is None or not src.is_file():
        raise BackupError("只支持 SQLite 数据库文件的备份")
    folder = local_dir()
    folder.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(folder).free
    if free < src.stat().st_size * 2:
        need = src.stat().st_size * 2 / 2**30
        raise BackupError(f"磁盘剩余 {free / 2**30:.1f} GB，不够做一次备份（需要约 {need:.1f} GB）")
    t0 = time.monotonic()
    stamp = to_market_time(utcnow(), "CN").strftime("%Y%m%d-%H%M")
    tmp = folder / f".{PREFIX}{stamp}.db.tmp"
    final = folder / f"{PREFIX}{stamp}{SUFFIX}"
    try:
        s, d = sqlite3.connect(str(src)), sqlite3.connect(str(tmp))
        try:
            s.backup(d, pages=4096)
            ok = d.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            d.close()
            s.close()
        if ok != "ok":
            raise BackupError(f"备份副本完整性检查失败：{ok}")
        with open(tmp, "rb") as fi, gzip.open(final, "wb", compresslevel=6) as fo:
            shutil.copyfileobj(fi, fo, 4 << 20)
    finally:
        tmp.unlink(missing_ok=True)
    out = {"file": str(final), "size": final.stat().st_size, "seconds": round(time.monotonic() - t0),
           "removed": _prune(folder, settings.backup_keep), "extra": None}
    if settings.backup_dir:
        try:
            dest = Path(settings.backup_dir)
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(final, dest / final.name)
            _prune(dest, settings.backup_keep)
            out["extra"] = str(dest / final.name)
        except OSError as e:
            out["extra_error"] = f"复制到 {settings.backup_dir} 失败：{e}"
    return out


async def scheduled() -> None:
    import asyncio

    from app.ops import notify_admin, now_iso, update_state

    try:
        out = await asyncio.to_thread(run_backup)
    except Exception as e:  # noqa: BLE001 - 备份失败要提醒，不能让任务静默失败
        logger.exception("数据库备份失败")
        update_state("backup", last_error=str(e), last_error_at=now_iso())
        notify_admin(f"backup:{now_iso()[:10]}", "【备份失败】数据库每日备份没有完成", [f"错误：{e}"])
        return
    update_state("backup", last=out | {"at": now_iso()}, last_error=None)
    logger.info("数据库已备份：%s（%.0f MB，%d 秒）", out["file"], out["size"] / 2**20, out["seconds"])
    if out.get("extra_error"):
        notify_admin(f"backup-extra:{now_iso()[:10]}", "【备份】异地副本没有复制成功", [out["extra_error"]])
