"""全时点股票池：用 Baostock 的每月指数成分快照生成历史成分记录（含已剔除、已退市的股票）。

- 指数成分：沪深300（000300）= hs300；中证800（000906）= hs300 ∪ zz500。
  每月月末查询一次快照（缓存，已查过的月份不再查），
  相邻快照之间成分不变，转为纳入 / 剔除区间，写入 index_members 表（指数代码加后缀 h，如 000906h，与半时点记录分开）。
- 最新一次快照之后：以中证指数公司的当前成分名单为准（实时跟踪用的就是这份名单），保证今天的可选范围与原来完全相同。
- 不在当前名单里的历史成分：查询上市 / 退市日期；已退市的股票从 Baostock 取日线与复权因子（其他数据源取不到），
  仍在上市的与其他股票一样用新浪 / 腾讯行情。
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import date, timedelta

import pandas as pd
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.market.types import Instrument
from app.models.market import DailyBarRow, IndexMemberRow, InstrumentRow

logger = logging.getLogger(__name__)

SNAP_START = date(2015, 12, 31)          # 数据从 2016 年起（训练期 2018 起 + 预热期）
HIST_INDEX = {"000300": ["hs300"], "000906": ["hs300", "zz500"]}
REFRESH_DAYS = 7
SAVE_EVERY = 50


HIST_OF = {"cn_csi300_pit_etf": "cn_csi300_hist", "cn_csi800_pit_etf": "cn_csi800_hist"}   # 半时点 → 全时点


def hist_code(index_code: str) -> str:
    return f"{index_code}h"


def switch_active(s: Session) -> list[str]:
    """把在用（未归档）策略从半时点股票池换成全时点股票池（2026-10-09 用户决定）。

    组合要求成员股票池相同，所以一起切换；之后须重新回测全部版本（tasks.reevaluate_all）。归档策略保持原样。
    """
    from app.models.quant import QuantStrategyRow

    out = []
    for st in s.scalars(select(QuantStrategyRow).where(QuantStrategyRow.status == "active",
                                                      QuantStrategyRow.universe_name.in_(list(HIST_OF)))):
        out.append(f"{st.id} {st.name}：{st.universe_name} → {HIST_OF[st.universe_name]}")
        st.universe_name = HIST_OF[st.universe_name]
    return out


def _cache_path():
    return get_settings().cache_dir / "baostock_pit.json"


def _load_cache() -> dict:
    p = _cache_path()
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"snapshots": {}, "basic": {}}


def _save_cache(c: dict) -> None:
    p = _cache_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")


def _months(today: date) -> list[date]:
    out = [d.date() for d in pd.date_range(SNAP_START, today, freq="ME")]
    if not out or out[-1] < today:
        out.append(today)              # 当月：查询今天的快照
    return out


def refresh(today: date | None = None, force: bool = False, current: set[str] | None = None) -> dict:
    """查询缺少的月度快照与历史成分的上市 / 退市日期（Baostock 登录较慢，每周最多一次）。返回统计。

    current：当前成分（都在上市，不必查询退市日期）。
    """
    from app.market.providers import baostock_cn as bsq

    today = today or date.today()
    cache = _load_cache()
    last = cache.get("refreshed")
    if not force and last and date.fromisoformat(last) + timedelta(days=REFRESH_DAYS) > today:
        return {"skipped": True, "refreshed": last}
    months = _months(today)
    queried = 0

    def tick():
        nonlocal queried
        queried += 1
        if queried % SAVE_EVERY == 0:          # 中途断掉时已查到的不丢
            _save_cache(cache)
            logger.info("Baostock 已查询 %d 次", queried)

    with bsq.session() as bs:
        for idx in ("hs300", "zz500"):
            snaps = cache["snapshots"].setdefault(idx, {})
            for m in months:
                key = m.isoformat()
                # 已查过的月份不再查；最近两个月重查（当月快照可能还没更新）
                if key in snaps and m < today - timedelta(days=62):
                    continue
                res = bsq.snapshot(bs, idx, m)
                if res:
                    snaps[key] = [res[0].isoformat(), res[1]]
                tick()
        hist = {s for idx in cache["snapshots"].values() for _eff, members in idx.values() for s in members}
        basic = cache["basic"]
        for sym in sorted(hist - (current or set())):
            if sym in basic and (basic[sym].get("delisted") or date.fromisoformat(basic[sym]["checked"])
                                 + timedelta(days=30) > today):
                continue
            info = bsq.stock_basic(bs, sym)
            basic[sym] = {"name": info["name"] if info else "", "checked": today.isoformat(),
                          "delisted": info["delisted"].isoformat() if info and info["delisted"] else None}
            tick()
    cache["refreshed"] = today.isoformat()
    _save_cache(cache)
    return {"queried": queried, "historical_members": len(hist),
            "delisted": sum(1 for v in cache["basic"].values() if v.get("delisted"))}


def snapshots(index_code: str) -> list[tuple[date, dict[str, str]]]:
    """某个指数（000300 / 000906）的月度成分快照，按生效日期排序：[(生效日期, {代码: 名称})]。"""
    cache = _load_cache()
    by_month: dict[str, list[tuple[date, dict[str, str]]]] = {}
    for idx in HIST_INDEX[index_code]:
        for m, (eff, members) in cache["snapshots"].get(idx, {}).items():
            by_month.setdefault(m, []).append((date.fromisoformat(eff), members))
    out = []
    for m in sorted(by_month):
        parts = by_month[m]
        if len(parts) != len(HIST_INDEX[index_code]):
            continue                    # 中证800 需要同一个月的两份快照
        eff = max(p[0] for p in parts)
        merged: dict[str, str] = {}
        for _, members in parts:
            merged.update(members)
        out.append((eff, merged))
    # 相同生效日期只保留一份
    dedup: dict[date, dict[str, str]] = {}
    for eff, members in out:
        dedup[eff] = members
    return sorted(dedup.items())


def intervals(snaps: list[tuple[date, dict[str, str]]], current: set[str],
              cutover: date | None = None) -> dict[str, list[tuple[date, date | None]]]:
    """快照 → 每只股票的纳入 / 剔除区间。cutover（默认最新快照的生效日期）之后以当前名单为准。"""
    out: dict[str, list[tuple[date, date | None]]] = {}
    if not snaps:
        return out
    cutover = cutover or snaps[-1][0]
    open_at: dict[str, date] = {}
    for eff, members in snaps:
        if eff >= cutover:
            break
        for sym in members:
            open_at.setdefault(sym, eff)
        for sym in list(open_at):
            if sym not in members:
                out.setdefault(sym, []).append((open_at.pop(sym), eff))
    for sym, start in open_at.items():
        out.setdefault(sym, []).append((start, cutover))
    for sym in current:
        out.setdefault(sym, []).append((cutover, None))
    # 合并首尾相接的区间（cutover 前后都在成分里）
    merged: dict[str, list[tuple[date, date | None]]] = {}
    for sym, ivs in out.items():
        ivs.sort(key=lambda x: x[0])
        res: list[tuple[date, date | None]] = []
        for a, b in ivs:
            if res and res[-1][1] == a:
                res[-1] = (res[-1][0], b)
            else:
                res.append((a, b))
        merged[sym] = res
    return merged


def write_membership(s: Session, index_code: str, current: dict[str, str]) -> dict:
    """生成并写入全时点成分记录（index_code + h）。current：当前成分 {代码: 名称}。"""
    snaps = snapshots(index_code)
    if not snaps:
        raise ValueError("还没有 Baostock 成分快照，请先刷新")
    ivs = intervals(snaps, set(current))
    names = {}
    for _eff, members in snaps:
        names.update(members)               # 后面的快照覆盖前面的：保留最后一次在成分中时的名称
    names.update(current)
    code = hist_code(index_code)
    s.execute(delete(IndexMemberRow).where(IndexMemberRow.index_code == code))
    n = 0
    for sym, lst in ivs.items():
        for a, b in lst:
            s.add(IndexMemberRow(index_code=code, symbol=sym, name=names.get(sym, ""), in_date=a, out_date=b,
                                 source="baostock"))
            n += 1
    s.flush()
    return {"symbols": len(ivs), "intervals": n, "first": snaps[0][0].isoformat(), "cutover": snaps[-1][0].isoformat()}


def extra_instruments(s: Session, index_code: str, have: set[str]) -> list[Instrument]:
    """全时点股票池里、不在当前名单中的历史成分股（含已退市的，带退市日期）。"""
    from app.market.universe import UniverseResolver

    basic = _load_cache().get("basic", {})
    rows = s.execute(select(IndexMemberRow.symbol, IndexMemberRow.name).where(
        IndexMemberRow.index_code == hist_code(index_code))).all()
    names: dict[str, str] = {}
    for sym, name in rows:
        names[sym] = name or names.get(sym, "")
    out = []
    for sym in sorted(set(names) - have):
        try:
            inst = UniverseResolver._cn_instrument({"symbol": sym, "name": names[sym] or sym.split(".")[0]})
        except ValueError:
            continue
        d = (basic.get(sym) or {}).get("delisted")
        out.append(replace(inst, delisted=date.fromisoformat(d)) if d else inst)
    return out


def fetch_delisted_bars(s: Session, insts: list[Instrument], start: date) -> dict:
    """已退市股票的日线（Baostock）：库里已有数据的跳过。返回 {代码: 结果}。"""
    from app.market.providers import baostock_cn as bsq

    # 数据起始日之前就已退市的（如 2015 年底退市的招商地产）没有行情可取
    todo = [i for i in insts if i.delisted and i.delisted > start and s.scalar(
        select(DailyBarRow.date).where(DailyBarRow.symbol == i.symbol).limit(1)) is None]
    res: dict[str, str] = {}
    if not todo:
        return res
    with bsq.session() as bs:
        for inst in todo:
            try:
                bars = bsq.daily_bars(bs, inst.symbol, start, inst.delisted)
            except Exception as e:  # noqa: BLE001
                res[inst.symbol] = f"error: {e}"
                continue
            for b in bars:
                s.merge(DailyBarRow(symbol=b.symbol, date=b.date, open=b.open, high=b.high, low=b.low, close=b.close,
                                    volume=b.volume, amount=b.amount, prev_close=b.prev_close, adj_factor=b.adj_factor))
            row = s.get(InstrumentRow, inst.symbol)
            if row is not None:
                row.delisted_date = inst.delisted
            s.commit()
            res[inst.symbol] = f"ok {len(bars)}"
    return res
