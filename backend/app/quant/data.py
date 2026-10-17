"""策略工坊的数据准备：把股票池的日线向前补到多年以前、刷新财报，并缓存行情矩阵。

数据源调用是同步阻塞的，在异步代码中请用 asyncio.to_thread 调用 prepare。
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from collections.abc import Callable
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.arena.deps import fundamentals_service, market_service, universe_resolver
from app.arena.lifecycle import latest_complete_date
from app.core.timeutils import to_market_time, utcnow
from app.db.session import get_sessionmaker
from app.market.types import Instrument
from app.market.universe import BENCHMARK_NAMES, index_instrument
from app.models.market import DailyBarRow, FundamentalRow
from app.quant.panel import Panel, load_panel

logger = logging.getLogger(__name__)

# 因子计算需要的预热期（日历日）：最长窗口 250 个交易日 + 上市天数过滤
WARMUP_DAYS = 550
_FUNDAMENTAL_TTL = timedelta(days=7)
_ACTIONS_TTL = timedelta(days=7)
_BATCH = 10
READY_SHARE = 0.5         # 股票池中至少这么多标的有行情，才算数据已就绪


def pit_index(universe_name: str) -> str | None:
    spec = universe_resolver().specs.get(universe_name)
    return spec.point_in_time if spec else None


def membership_code(universe_name: str) -> str | None:
    """成分记录的指数代码：全时点股票池用 Baostock 生成的记录（代码加 h），半时点用新浪的纳入日期。"""
    from app.quant import pit

    spec = universe_resolver().specs.get(universe_name)
    if not spec or not spec.point_in_time:
        return None
    return pit.hist_code(spec.point_in_time) if spec.history else spec.point_in_time


def pit_ready(s: Session, universe_name: str) -> bool | None:
    """全时点股票池的历史成分记录是否已经生成（Baostock 获取失败时没有）；不是全时点股票池时为 None。

    没有历史成分时，股票池只剩今天的成分，回测仍可运行，但带着幸存者偏差（见可实盘检查）。
    """
    from app.models.market import IndexMemberRow
    from app.quant import pit

    spec = universe_resolver().specs.get(universe_name)
    if not spec or not (spec.history and spec.point_in_time):
        return None
    code = pit.hist_code(spec.point_in_time)
    return s.scalar(select(IndexMemberRow.id).where(IndexMemberRow.index_code == code).limit(1)) is not None


def universe_members(universe_name: str) -> tuple[list[Instrument], Instrument, str]:
    resolver = universe_resolver()
    spec = resolver.specs.get(universe_name)
    if spec is None:
        raise ValueError(f"未知股票池：{universe_name}")
    insts = resolver.resolve(universe_name)
    if spec.history and spec.point_in_time:
        # 全时点：再加上历史上的全部成分（已剔除、已退市的股票）
        from app.quant import pit

        with get_sessionmaker()() as s:
            insts = [*insts, *pit.extra_instruments(s, spec.point_in_time, {i.symbol for i in insts})]
    bench = index_instrument(spec.benchmark, BENCHMARK_NAMES.get(spec.benchmark, spec.benchmark))
    return insts, bench, spec.market


def _current_constituents(index_code: str) -> dict[str, str]:
    """中证指数公司的当前成分 {代码: 名称}（与实时跟踪用的名单相同）。"""
    from app.market.symbols import normalize_cn

    resolver = universe_resolver()
    cons = resolver._cached(f"index_cons_{index_code}", timedelta(days=7), date.today(),
                            lambda: resolver.source.index_constituents(index_code))
    return {normalize_cn(code): name for code, name in cons}


def prepare_history(universe_name: str, say: Callable[[str], None]) -> dict:
    """全时点股票池：刷新 Baostock 成分快照（每周最多一次）并重写成分记录。"""
    from app.quant import pit

    spec = universe_resolver().specs[universe_name]
    say("历史成分（Baostock，首次约需几分钟）")
    current = _current_constituents(spec.point_in_time)
    info = pit.refresh(current=set(current))
    with get_sessionmaker()() as s:
        info |= pit.write_membership(s, spec.point_in_time, current)
        s.commit()
    return info


def _default_train_start() -> date:
    from app.quant.service import LabSettings

    return LabSettings().train_start


def data_start(train_start: date) -> date:
    return train_start - timedelta(days=WARMUP_DAYS)


def prepare(universe_name: str, start: date, progress: Callable[[str], None] | None = None) -> dict:
    """补齐 [start, 最新收盘] 的日线与股票财报。返回统计。"""
    say = progress or (lambda m: None)
    errors: dict[str, str] = {}
    spec = universe_resolver().specs.get(universe_name)
    history = bool(spec and spec.history and spec.point_in_time)
    if history:
        try:
            prepare_history(universe_name, say)
        except Exception as e:          # 成分快照获取失败时沿用旧记录
            logger.exception("全时点成分刷新失败")
            errors["pit"] = f"error: {e}"
    insts, bench, market = universe_members(universe_name)
    with get_sessionmaker()() as s:
        svc = market_service(s, market)
        svc.upsert_instruments([*insts, bench])
        idx = pit_index(universe_name)
        if idx and not history:
            say("指数成分纳入日期")
            try:
                svc.update_index_members(idx)
            except Exception as e:      # 成分记录只用于时点股票池，失败时沿用旧记录
                errors[f"index:{idx}"] = f"error: {e}"
        if history:
            delisted = [i for i in insts if i.delisted]
            say(f"已退市股票行情（Baostock）{len(delisted)} 只")
            from app.quant import pit

            try:
                for sym, msg in pit.fetch_delisted_bars(s, delisted, start).items():
                    if msg.startswith("error"):
                        errors[sym] = msg
            except Exception as e:  # noqa: BLE001
                errors["pit:bars"] = f"error: {e}"
            insts = [i for i in insts if not i.delisted]          # 已退市的不再更新行情、分红、财报
        cal = svc.calendar()
        latest = latest_complete_date(cal, to_market_time(utcnow(), market))
        s.commit()
        all_insts = [bench, *insts]
        for k in range(0, len(all_insts), _BATCH):
            batch = all_insts[k:k + _BATCH]
            say(f"行情 {k + len(batch)}/{len(all_insts)}")
            for sym, msg in svc.update_bars(batch, until=latest, history_start=start).items():
                if msg.startswith("error"):
                    errors[sym] = msg
            for sym, msg in svc.backfill_bars(batch, start).items():
                if msg.startswith("error"):
                    errors[sym] = msg
            s.commit()

        # 分红记录（股息率因子）与行业分类（行业中性化、行业限制），已拉取且未过期的跳过
        todo_actions = _actions_due([i for i in insts if i.type != "index"], latest)
        for k in range(0, len(todo_actions), _BATCH):
            batch = todo_actions[k:k + _BATCH]
            say(f"分红送配 {k + len(batch)}/{len(todo_actions)}")
            res = svc.update_corporate_actions(batch)
            _mark_actions([x for x, m in res.items() if m == "ok"], latest)
            s.commit()
        stock_insts = [i for i in insts if i.type == "stock"]
        for k in range(0, len(stock_insts), _BATCH * 5):
            batch = stock_insts[k:k + _BATCH * 5]
            say(f"行业分类 {k + len(batch)}/{len(stock_insts)}")
            for sym, msg in svc.update_industries(batch).items():
                if msg.startswith("error"):
                    errors[f"industry:{sym}"] = msg
            s.commit()

        stocks = [i.symbol for i in insts if i.type == "stock"]
        fresh_after = utcnow() - _FUNDAMENTAL_TTL
        fresh = set(s.scalars(select(FundamentalRow.symbol).where(
            FundamentalRow.symbol.in_(stocks), FundamentalRow.fetched_at >= fresh_after).distinct()))
        todo = [x for x in stocks if x not in fresh]
        fs = fundamentals_service(s)
        for k in range(0, len(todo), _BATCH):
            batch = todo[k:k + _BATCH]
            say(f"财报 {k + len(batch)}/{len(todo)}")
            fs.refresh(batch, market)
            s.commit()
    _panels.clear()
    msg = f"数据准备完成：{len(insts)} 个标的，最新 {latest}" + (f"，{len(errors)} 个失败" if errors else "")
    say(msg)
    return {"symbols": len(insts), "latest": latest.isoformat(), "errors": dict(list(errors.items())[:20]),
            "message": msg}


def _actions_file():
    from app.core.config import get_settings

    return get_settings().cache_dir / "corporate_actions_fetched.json"


def _actions_due(insts: list[Instrument], today: date) -> list[Instrument]:
    """超过 _ACTIONS_TTL 没有刷新分红送配记录的标的。"""
    import json

    path = _actions_file()
    done = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    return [i for i in insts if i.symbol not in done or date.fromisoformat(done[i.symbol]) + _ACTIONS_TTL < today]


def _mark_actions(symbols: list[str], today: date) -> None:
    import json

    path = _actions_file()
    done = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    done.update({x: today.isoformat() for x in symbols})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(done), encoding="utf-8")


def coverage(s: Session, universe_name: str) -> dict:
    """股票池的数据覆盖情况（不访问数据源，股票池成分使用缓存）。"""
    insts, bench, _ = universe_members(universe_name)
    symbols = [i.symbol for i in insts]
    rows = s.execute(select(DailyBarRow.symbol, func.min(DailyBarRow.date), func.max(DailyBarRow.date))
                     .where(DailyBarRow.symbol.in_([*symbols, bench.symbol])).group_by(DailyBarRow.symbol)).all()
    by = {r[0]: (r[1], r[2]) for r in rows}
    firsts = sorted(v[0] for k, v in by.items() if k != bench.symbol)
    fund = s.scalar(select(func.count(func.distinct(FundamentalRow.symbol))).where(
        FundamentalRow.symbol.in_(symbols))) or 0
    median_first = firsts[len(firsts) // 2] if firsts else None
    bench_first = by.get(bench.symbol, (None, None))[0]
    need = data_start(_default_train_start()) + timedelta(days=60)
    return {
        # 多数标的与基准都覆盖到默认训练区间之前的预热期，才能开始回测；
        # 还要求至少一半的标的已有行情（快速体验只下载了上证50，它们也属于沪深300，不能据此认为沪深300 已就绪）
        "ready": bool(median_first and bench_first and median_first <= need and bench_first <= need
                      and len(firsts) >= READY_SHARE * len(symbols)),
        "symbols": len(symbols), "with_data": len(firsts),
        "benchmark": bench.symbol, "bench_first": bench_first,
        "bench_last": by.get(bench.symbol, (None, None))[1],
        # 中位数标的的最早日期，能反映多数标的的历史长度
        "median_first": median_first,
        "fundamentals": fund, "stocks": sum(1 for i in insts if i.type == "stock"),
        # 全时点股票池的历史成分是否已生成（False 时有幸存者偏差，需要重新准备数据）
        "pit_ready": pit_ready(s, universe_name),
    }


# ---------------------------------------------------------------- 行情矩阵缓存

_panels: OrderedDict[tuple, Panel] = OrderedDict()
_PANEL_CACHE = 8          # 实时决策、因子研究、股票详情排名、策略页各用各的矩阵（服务器内存充足，单个 0.2～0.4 GB）
# 同一个矩阵同时只加载一次：每天新行情入库后缓存全部失效，页面同时发出的多个请求原来会各自从头加载
# 同一个矩阵（每份几秒到几十秒、0.2～0.4 GB），互相抢 CPU，接口一起超时。现在后到的请求等先到的加载完直接复用
_cache_guard = threading.Lock()
_loading: dict[tuple, threading.Lock] = {}


def get_panel(s: Session, universe_name: str, start: date, end: date) -> Panel:
    insts, bench, _ = universe_members(universe_name)
    last = s.scalar(select(func.max(DailyBarRow.date)).where(DailyBarRow.symbol == bench.symbol))
    key = (universe_name, start, end, last)
    with _cache_guard:
        p = _panels.get(key)
        if p is not None:
            _panels.move_to_end(key)
            return p
        lock = _loading.setdefault(key, threading.Lock())
    with lock:
        with _cache_guard:
            p = _panels.get(key)
        if p is None:
            try:
                p = _load_panel(s, universe_name, insts, bench, start, end)
            finally:
                with _cache_guard:
                    _loading.pop(key, None)
            with _cache_guard:
                _panels[key] = p
                while len(_panels) > _PANEL_CACHE:
                    _panels.popitem(last=False)
    return p


def _load_panel(s: Session, universe_name: str, insts: list[Instrument], bench: Instrument,
                start: date, end: date) -> Panel:
    idx = membership_code(universe_name)
    membership = None
    if idx:
        from app.arena.deps import market_service

        membership = market_service(s, "CN").index_membership(idx)
        if not membership:
            raise ValueError(f"时点股票池缺少指数 {idx} 的成分记录，请先准备数据")
    p = load_panel(s, [i.symbol for i in insts], bench.symbol, data_start(start), end, membership)
    delisted = {i.symbol: i.delisted for i in insts if i.delisted}
    if delisted:
        import numpy as np

        dates64 = np.array(p.dates, dtype="datetime64[D]")
        p.delisted = np.full(len(p.symbols), len(p.dates))
        for j, sym in enumerate(p.symbols):
            if sym in delisted:
                p.delisted[j] = int(np.searchsorted(dates64, np.datetime64(delisted[sym]), side="right"))
    return p
