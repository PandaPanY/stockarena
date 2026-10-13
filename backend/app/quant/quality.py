"""数据质量扫描：找出可能产生「假收益」的行情异常（之前发现过 ETF 份额折算导致的假跳变）。

检查项（按行情矩阵逐标的、逐日）：
- 跳变：复权后单日涨跌幅超过涨跌停限制（上市前 5 个交易日不检查），超过两倍限制为「错误」；
- 价格不一致：最高价低于开盘 / 收盘、最低价高于开盘 / 收盘、价格 ≤ 0；
- 复权因子下降：后复权因子只应随分红送转上升；
- 长期无行情：上市后连续 60 个交易日以上没有日线（长期停牌或数据缺失）；
- 数据未更新：仍在股票池中，但最近 5 个交易日以上没有日线（停牌或数据源未更新）。

扫描结果保存在 app_settings（data_quality），每晚数据更新后自动扫描；用户可以把确认无误的条目标记为忽略。
发现问题的标的可以在页面上重新下载日线与除权数据，之后自动重新回测所有在用策略。
"""

from __future__ import annotations

import logging
from datetime import date

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.timeutils import utcnow
from app.models.quant import QuantStrategyRow
from app.models.settings import AppSettingRow
from app.quant.panel import Panel

logger = logging.getLogger(__name__)

KEY = "data_quality"
IGNORED_KEY = "data_quality_ignored"
JUMP_TOL = 0.015            # 涨跌停价按分取整，留出余量
IPO_DAYS = 5                # 新股上市前几个交易日涨跌幅限制不同，不检查
GAP_DAYS = 60
STALE_DAYS = 5
MAX_ISSUES = 300
LEVEL_ORDER = {"error": 0, "warn": 1, "info": 2}


def issue_key(it: dict) -> str:
    return f"{it['symbol']}:{it['date']}:{it['kind']}"


def scan_panel(panel: Panel) -> list[dict]:
    """返回异常列表 [{symbol, name, date, kind, level, detail}]，错误在前、日期新的在前。"""
    T, N = panel.shape
    out: list[dict] = []

    def add(i: int, j: int, kind: str, level: str, detail: str) -> None:
        out.append({"symbol": panel.symbols[j], "name": panel.names[j], "date": panel.dates[i].isoformat(),
                    "kind": kind, "level": level, "detail": detail})

    has = panel.has_bar
    adj = np.where(has, panel.close * panel.factor, np.nan)
    prev_adj = np.full_like(adj, np.nan)
    last = np.full(N, np.nan)
    for i in range(T):
        prev_adj[i] = last
        last = np.where(has[i], adj[i], last)
    with np.errstate(divide="ignore", invalid="ignore"):
        ret = adj / prev_adj - 1
    lim = np.where(np.isfinite(panel.limit_pct), panel.limit_pct, 0.10)
    seasoned = panel.bars_count > IPO_DAYS

    # 1. 跳变
    jump = has & seasoned & np.isfinite(ret) & (np.abs(ret) > lim + JUMP_TOL)
    # 距上一根日线的交易日数：长期停牌后复牌（含恢复上市首日不设涨跌幅限制）的跳变多为真实情况
    since = np.zeros((T, N), int)
    run = np.zeros(N, int)
    for i in range(T):
        since[i] = run
        run = np.where(has[i], 0, run + 1)
    for i, j in zip(*np.where(jump), strict=True):
        r = float(ret[i, j])
        text = f"复权后单日{'上涨' if r > 0 else '下跌'} {abs(r):.1%}，超过涨跌停限制 {lim[j]:.0%}"
        if since[i, j] >= GAP_DAYS:
            add(int(i), int(j), "jump", "info", text + f"（停牌 {since[i, j]} 个交易日后复牌首日，多为真实涨跌）")
            continue
        level = "error" if abs(r) > 2 * lim[j] + 0.05 else "warn"
        add(int(i), int(j), "jump", level, text + "（可能是复权或份额折算错误，或涨跌停限制记录有误）")

    # 2. 价格不一致
    o, h, lo, c = panel.open, panel.high, panel.low, panel.close
    tick = panel.tick / 2
    with np.errstate(invalid="ignore"):
        bad = has & ((h < np.fmax(o, c) - tick) | (lo > np.fmin(o, c) + tick) | (c <= 0) | (o <= 0))
    for i, j in zip(*np.where(bad), strict=True):
        add(int(i), int(j), "ohlc", "error",
            f"开 {o[i, j]:g} 高 {h[i, j]:g} 低 {lo[i, j]:g} 收 {c[i, j]:g}：价格之间不一致")

    # 3. 复权因子下降
    f = panel.factor
    drop = np.zeros_like(has)
    drop[1:] = has[1:] & (f[1:] < f[:-1] * (1 - 1e-6))
    for i, j in zip(*np.where(drop), strict=True):
        text = f"复权因子由 {f[i - 1, j]:.4g} 降到 {f[i, j]:.4g}"
        if jump[i, j]:
            add(int(i), int(j), "factor", "warn", text + "，复权后价格不连续（只应随分红送转上升）")
        else:   # 基金份额合并（缩股）时因子下降、复权价连续，属正常
            add(int(i), int(j), "factor", "info", text + "，复权后价格连续（多为基金份额合并）")

    # 4. 长期无行情 / 5. 数据未更新
    member_now = panel.member[-1] if panel.member is not None else np.ones(N, bool)
    for j in range(N):
        idx = np.where(has[:, j])[0]
        if len(idx) == 0:
            continue
        gaps = np.diff(idx) - 1
        for k in np.where(gaps >= GAP_DAYS)[0]:
            add(int(idx[k + 1]), j, "gap", "info",
                f"{panel.dates[idx[k]]:%Y-%m-%d} 之后连续 {int(gaps[k])} 个交易日没有日线（长期停牌或数据缺失）")
        stale = T - 1 - int(idx[-1])
        if stale >= STALE_DAYS and member_now[j]:
            add(T - 1, j, "stale", "warn", f"最近 {stale} 个交易日没有日线（停牌或数据源未更新），"
                                         f"最后一根是 {panel.dates[idx[-1]]:%Y-%m-%d}")

    out.sort(key=lambda x: (LEVEL_ORDER[x["level"]], _neg_date(x["date"])))
    return out


def _neg_date(d: str) -> int:
    return -date.fromisoformat(d).toordinal()


# ---------------------------------------------------------------- 保存与读取


def _get(s: Session, key: str) -> dict:
    row = s.get(AppSettingRow, key)
    return dict(row.value or {}) if row else {}


def _put(s: Session, key: str, value: dict) -> None:
    row = s.get(AppSettingRow, key) or AppSettingRow(key=key)
    row.value = value
    s.add(row)


def ignored(s: Session) -> set[str]:
    return set(_get(s, IGNORED_KEY).get("keys", []))


def set_ignored(s: Session, key: str, ignore: bool) -> None:
    keys = ignored(s)
    (keys.add if ignore else keys.discard)(key)
    _put(s, IGNORED_KEY, {"keys": sorted(keys)})


def universes_in_use(s: Session) -> list[tuple[str, date]]:
    """在用策略的股票池与开始日期（同一股票池取最早的开始日期）。"""
    from app.quant.service import settings_of

    out: dict[str, date] = {}
    for st in s.scalars(select(QuantStrategyRow).where(QuantStrategyRow.status == "active")):
        start = settings_of(st).train_start
        out[st.universe_name] = min(out.get(st.universe_name, start), start)
    return sorted(out.items())


def scan(s: Session) -> dict:
    """扫描在用的全部股票池（只读，结果由调用方用 save 保存）。"""
    from app.quant.data import get_panel

    result: dict = {"checked_at": utcnow().isoformat(), "universes": {}}
    for universe, start in universes_in_use(s):
        try:
            panel = get_panel(s, universe, start, date.today())
        except Exception as e:  # noqa: BLE001 - 单个股票池失败不影响其他
            result["universes"][universe] = {"error": str(e)}
            continue
        issues = scan_panel(panel)
        counts = {lv: sum(1 for x in issues if x["level"] == lv) for lv in LEVEL_ORDER}
        result["universes"][universe] = {
            "symbols": panel.shape[1], "days": panel.shape[0], "start": panel.dates[0].isoformat(),
            "end": panel.dates[-1].isoformat(), "counts": counts, "total": len(issues),
            "issues": issues[:MAX_ISSUES]}
    return result


def save(s: Session, result: dict) -> list[dict]:
    """保存扫描结果；返回与上次相比新出现的、未忽略的错误（用于推送）。"""
    prev = _get(s, KEY)
    seen = {issue_key(x) for u in (prev.get("universes") or {}).values() for x in u.get("issues", [])}
    skip = ignored(s)
    new = [x for u in result["universes"].values() for x in u.get("issues", [])
           if x["level"] == "error" and issue_key(x) not in seen and issue_key(x) not in skip]
    _put(s, KEY, result)
    return new


def report(s: Session) -> dict:
    data = _get(s, KEY)
    skip = ignored(s)
    for u in (data.get("universes") or {}).values():
        for x in u.get("issues", []):
            x["key"] = issue_key(x)
            x["ignored"] = x["key"] in skip
    return data


def run_and_save(notify: bool = True) -> dict:
    """扫描、保存，并把新出现的错误推送到「异常提醒」。"""
    from app.db.session import get_sessionmaker

    with get_sessionmaker()() as s:
        result = scan(s)
    with get_sessionmaker()() as s:
        new = save(s, result)
        s.commit()
    if notify and new:
        try:
            from app.notify import service as notify_service

            notify_service.deliver(notify_service.data_quality_message(new))
        except Exception:
            logger.exception("数据异常推送失败")
    return result


def repair(s: Session, symbol: str) -> str:
    """重新下载该标的的全部已缓存日线与除权数据（覆盖），返回结果说明。调用方提交事务后应重新回测。"""
    from app.arena.deps import market_service
    from app.market.service import _instrument_from_row
    from app.models.market import InstrumentRow
    from app.quant import data

    row = s.get(InstrumentRow, symbol)
    if row is None:
        raise ValueError(f"未知标的 {symbol}")
    inst = _instrument_from_row(row)
    svc = market_service(s, inst.market)
    bars = svc.refetch_bars([inst])[symbol]
    acts = svc.update_corporate_actions([inst])[symbol]
    data._panels.clear()
    return f"{symbol} {inst.name}：日线 {bars}；除权事件 {acts}"
