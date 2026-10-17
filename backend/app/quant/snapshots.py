"""westock 数据每日快照：一致预期、个股评分这类只有当前值、查不到历史的数据，每个交易日晚上存一份，
为一两年后做时点正确的研究积累数据（研究时只能使用 fetched_at 之后的数据）。

- 只在开启技能功能、且已安装并启用 westockdata 技能时运行；用技能所有者（管理员）的授权；
- 在批量专用的常驻容器里逐只查询（复用容器，每次约 1 秒）；批量查询多只实测会握手超时，不使用；
- 与 AI 复核共用 westock 额度：失败太多（连续 10 次，或累计 30 次且超过两成）就停止当天的查询，留给复核；
- 幂等：同一天已保存的不再查询，晚些时候的补跑只查缺的。

命令行（在 backend/ 目录）：
    uv run python -m app.quant.snapshots run        # 立即运行一次（今天）
    uv run python -m app.quant.snapshots summary    # 最近几天的保存情况
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy import func, select

from app.core.timeutils import to_market_time, utcnow
from app.db.session import get_sessionmaker
from app.models.research import WestockSnapshotRow
from app.models.skills import SkillRow

logger = logging.getLogger(__name__)

SKILL_NAME = "westockdata"
UNIVERSE = "cn_csi300"                  # 先存沪深300（当前成分），观察一周没有被限流再扩到中证800
KINDS = ("consensus", "score")
TIMEOUT_S = 60
MAX_CONSECUTIVE_FAILS = 10
MAX_FAILS, MAX_FAIL_RATE = 30, 0.2
COMMIT_EVERY = 20

_EMPTY_MARKERS = ("暂无数据", "无数据", "未找到")


# ---------------------------------------------------------------- 解析


def _num(text: str) -> float | None:
    text = text.strip().replace(",", "")
    try:
        return float(text)
    except ValueError:
        return None


def _table(text: str) -> list[dict[str, str]]:
    """Markdown 表格 → [{列名: 文本}]（第一张表）。"""
    rows = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("|")]
    if len(rows) < 2:
        return []
    header = [c.strip() for c in rows[0].strip("|").split("|")]
    out = []
    for ln in rows[1:]:
        cells = [c.strip() for c in ln.strip("|").split("|")]
        if all(set(c) <= set("-: ") for c in cells):
            continue
        out.append(dict(zip(header, cells, strict=False)))
    return out


def parse_consensus(text: str) -> dict:
    """一致预期：{"target_price": 目标价, "years": [{year, eps, revenue, netProfit, pe, pb, ps, ...}]}。"""
    m = re.search(r"目标价[:：]\s*([-\d.,]+)", text)
    years = []
    for row in _table(text):
        item = {k: (_num(v) if k != "year" else (int(v) if v.isdigit() else v)) for k, v in row.items()}
        years.append(item)
    return {"target_price": _num(m.group(1)) if m else None, "years": years}


_SCORE_RE = re.compile(r"([-\d.]+)\s*\((.*)\)")
_CHANGE_RE = re.compile(r"(周|月|季)\D*?([+-]?\d+(?:\.\d+)?)")
_PERIOD = {"周": "week", "月": "month", "季": "quarter"}


def parse_score(text: str) -> dict:
    """个股评分：{capScore: {value, week, month, quarter}, compScore: ..., ...}（周 / 月 / 季为变动值）。"""
    rows = _table(text)
    if not rows:
        return {}
    out: dict = {}
    for key, cell in rows[0].items():
        if not key.endswith("Score"):
            continue
        m = _SCORE_RE.match(cell)
        if not m:
            out[key] = {"value": _num(cell)}
            continue
        item = {"value": _num(m.group(1))}
        for label, val in _CHANGE_RE.findall(m.group(2)):
            item[_PERIOD[label]] = _num(val)
        out[key] = item
    return out


PARSERS = {"consensus": parse_consensus, "score": parse_score}


def classify(kind: str, output: str, exit_code: int) -> tuple[str, dict]:
    """返回（状态, 解析结果）：ok 有数据 / empty 暂无数据 / error 失败（不保存，下次补）。"""
    text = output.strip()
    if exit_code != 0 or "失败" in text[:200] or not text:
        return "error", {}
    if any(mk in text for mk in _EMPTY_MARKERS) and "|" not in text:
        return "empty", {}
    data = PARSERS[kind](text)
    if (kind == "consensus" and not data.get("years") and data.get("target_price") is None) or \
            (kind == "score" and not data):
        return "empty", {}
    return "ok", data


# ---------------------------------------------------------------- 运行


@dataclass
class RunStats:
    as_of: date
    saved: int = 0
    empty: int = 0
    failed: int = 0
    skipped: int = 0
    stopped: str = ""
    errors: list[str] = field(default_factory=list)

    def message(self) -> str:
        msg = (f"westock 快照 {self.as_of}：保存 {self.saved} 条（其中暂无数据 {self.empty} 条），"
               f"失败 {self.failed} 条，已有 {self.skipped} 条")
        return msg + (f"；{self.stopped}" if self.stopped else "")


def skill_row() -> SkillRow | None:
    """已安装并启用的 westockdata 技能（技能功能关闭时为 None）。"""
    from app.skills.gate import skills_enabled

    if not skills_enabled():
        return None
    with get_sessionmaker()() as s:
        row = s.scalars(select(SkillRow).where(SkillRow.name == SKILL_NAME, SkillRow.status == "installed",
                                               SkillRow.enabled.is_(True)).order_by(SkillRow.id)).first()
        if row is not None:
            s.expunge(row)
        return row


def symbols_of(universe: str = UNIVERSE) -> list[str]:
    from app.arena.deps import universe_resolver

    return [i.symbol for i in universe_resolver().resolve(universe) if i.type == "stock"]


def _westock_code(symbol: str) -> str:
    from app.market.symbols import to_sina

    return to_sina(symbol)


def _today() -> date:
    return to_market_time(utcnow(), "CN").date()


async def run(as_of: date | None = None, symbols: list[str] | None = None,
              kinds: tuple[str, ...] = KINDS) -> RunStats:
    """保存 as_of（默认今天）的快照，已保存的跳过。"""
    from app.skills import sandbox, service

    as_of = as_of or _today()
    stats = RunStats(as_of)
    row = skill_row()
    if row is None:
        stats.stopped = "未开启技能功能或没有安装启用 westockdata，跳过"
        return stats
    symbols = symbols if symbols is not None else await asyncio.to_thread(symbols_of)
    with get_sessionmaker()() as s:
        done = set(s.execute(select(WestockSnapshotRow.symbol, WestockSnapshotRow.kind)
                             .where(WestockSnapshotRow.as_of == as_of)).all())
        token = service._token(s.get(SkillRow, row.id))
    todo = [(sym, k) for sym in symbols for k in kinds if (sym, k) not in done]
    stats.skipped = len(symbols) * len(kinds) - len(todo)
    pending: list[WestockSnapshotRow] = []
    consecutive = 0
    try:
        for sym, kind in todo:
            try:
                res = await sandbox.run_batch(row.id, token, ["westock", kind, _westock_code(sym)], timeout=TIMEOUT_S)
                status, data = classify(kind, res.output, res.exit_code)
                output = res.output
            except Exception as e:  # noqa: BLE001 - 单只失败不影响其他
                status, data, output = "error", {}, f"{type(e).__name__}: {e}"
            if status == "error":
                stats.failed += 1
                consecutive += 1
                if len(stats.errors) < 5:
                    stats.errors.append(f"{sym} {kind}：{output.strip()[:120]}")
            else:
                consecutive = 0
                stats.saved += 1
                stats.empty += status == "empty"
                pending.append(WestockSnapshotRow(as_of=as_of, symbol=sym, kind=kind, status=status, data=data,
                                                  raw=output[:20000], fetched_at=utcnow()))
            if len(pending) >= COMMIT_EVERY:
                _flush(pending)
            if consecutive >= MAX_CONSECUTIVE_FAILS or (
                    stats.failed >= MAX_FAILS and stats.failed > MAX_FAIL_RATE * (stats.saved + stats.failed)):
                stats.stopped = "失败太多，停止当天的查询（可能被限流或网络异常，额度留给 AI 复核）"
                break
    finally:
        _flush(pending)
        await sandbox.stop_batch(row.id)
    level = logging.WARNING if stats.stopped or stats.failed else logging.INFO
    logger.log(level, "%s%s", stats.message(), ("；例：" + " | ".join(stats.errors[:2])) if stats.errors else "")
    return stats


def _flush(pending: list[WestockSnapshotRow]) -> None:
    if not pending:
        return
    with get_sessionmaker()() as s:
        s.add_all(pending)
        s.commit()
    pending.clear()


async def scheduled() -> None:
    """定时任务：只在 A 股交易日运行。"""
    from app.arena.deps import market_service

    today = _today()
    with get_sessionmaker()() as s:
        try:
            trading = market_service(s, "CN").calendar(today).is_trading_day(today)
        except ValueError:
            trading = today.weekday() < 5
    if trading:
        await run(today)


def summary(days: int = 7) -> list[dict]:
    with get_sessionmaker()() as s:
        rows = s.execute(select(WestockSnapshotRow.as_of, WestockSnapshotRow.kind, WestockSnapshotRow.status,
                                func.count(), func.max(WestockSnapshotRow.fetched_at))
                         .group_by(WestockSnapshotRow.as_of, WestockSnapshotRow.kind, WestockSnapshotRow.status)
                         .order_by(WestockSnapshotRow.as_of.desc())).all()
    out: dict[tuple, dict] = {}
    for d, kind, status, n, last in rows:
        item = out.setdefault((d, kind), {"as_of": d.isoformat(), "kind": kind, "ok": 0, "empty": 0, "last": None})
        item[status] = item.get(status, 0) + n
        item["last"] = max(filter(None, [item["last"], last.isoformat() if isinstance(last, datetime) else last]))
    return list(out.values())[: days * len(KINDS)]


def main(argv: list[str]) -> int:
    from app.core.config import get_settings
    from app.core.logsetup import setup_logging

    setup_logging("INFO", get_settings().log_dir)
    if argv[1:2] == ["run"]:
        stats = asyncio.run(run())
        print(stats.message())
        return 0
    if argv[1:2] == ["summary"]:
        for item in summary():
            print(item)
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
