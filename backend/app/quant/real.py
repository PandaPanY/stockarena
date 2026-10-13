"""实盘对照（PRD 3.9 G8）：用户录入 / 导入真实成交，与对应策略或组合的实时跟踪（模拟账户）比较。

- 实盘净值：从初始资金出发，按成交逐日重建持仓与现金，用收盘价估值；分红送转按复权因子折算进持股数
  （与交易引擎、快速回测一致，相当于分红再投资，和真实账户的现金分红略有差别）。
- 对比：收益差、跟踪误差、相关性；逐笔执行对照（实盘成交与模拟成交按标的、方向、日期就近配对），
  得到执行价差（基点）、延迟天数、漏做与多做的交易；期末持仓权重差。
- 只保存用户提供的成交数据，不连接券商、不保存券商账号或密码。
"""

from __future__ import annotations

import csv
import io
import math
import re
from dataclasses import dataclass
from datetime import date, datetime

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import FillRow, InstrumentRow, NavSnapshotRow
from app.models.quant import QuantPortfolioRow, QuantStrategyRow, RealAccountRow, RealFillRow
from app.quant.panel import load_panel

MATCH_DAYS = 5                       # 实盘成交与模拟成交配对：实盘日期在模拟日期之后 0～5 个交易日内
TE_OK, GAP_OK = 0.05, 0.02           # 实盘与模拟：跟踪误差（年化）、年化收益差——一致
TE_WARN, GAP_WARN = 0.10, 0.05       # 有偏差；超过为偏差较大
MIN_DAYS = 5


class RealError(ValueError):
    pass


# ---------------------------------------------------------------- 代码与成交导入


def normalize_symbol(raw: str) -> str | None:
    """'600000'、'sh600000'、'600000.SH'、'SZ000001' → 带交易所后缀的代码；无法识别返回 None。"""
    t = str(raw).strip().upper().replace("'", "").replace("=", "").replace('"', "")
    m = re.fullmatch(r"(SH|SZ|BJ)?\.?(\d{6})(?:\.(SH|SZ|BJ))?", t)
    if not m:
        return None
    code, ex = m.group(2), m.group(1) or m.group(3)
    if not ex:
        if code[0] in "569":
            ex = "SH"
        elif code[0] in "0123":
            ex = "SZ"
        elif code[0] in "48":
            ex = "BJ"
        else:
            return None
    return f"{code}.{ex}"


_ALIASES = {
    "date": ("成交日期", "交易日期", "发生日期", "日期", "date", "trade_date"),
    "symbol": ("证券代码", "股票代码", "代码", "symbol", "code"),
    "name": ("证券名称", "股票名称", "名称", "name"),
    "side": ("买卖标志", "买卖方向", "操作", "方向", "业务名称", "交易类别", "委托类别", "side"),
    "qty": ("成交数量", "成交股数", "数量", "qty", "quantity"),
    "price": ("成交价格", "成交均价", "价格", "price"),
    "amount": ("成交金额", "amount"),
    "fee_total": ("费用合计", "总费用", "交易费用", "fee", "fees"),
}
_FEE_PARTS = ("手续费", "佣金", "印花税", "过户费", "规费", "经手费", "证管费", "其他费", "其他费用")


def _find(headers: list[str], key: str) -> str | None:
    for alias in _ALIASES[key]:
        for h in headers:
            if h.strip().lower() == alias.lower():
                return h
    return None


def _num(v) -> float | None:
    if v is None:
        return None
    t = str(v).strip().replace(",", "").replace("¥", "").replace("'", "")
    if t in ("", "-", "--"):
        return None
    try:
        return float(t)
    except ValueError:
        return None


def parse_date(v) -> date | None:
    t = str(v or "").strip().replace("'", "")
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d", "%Y.%m.%d", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(t, fmt).date()
        except ValueError:
            continue
    return None


def _side(v) -> str | None:
    t = str(v).strip().lower()
    if not t:
        return None
    if "买" in t or t in ("buy", "b"):
        return "buy" if "卖" not in t else None
    if "卖" in t or t in ("sell", "s"):
        return "sell"
    return None


def decode(content: bytes) -> str:
    for enc in ("utf-8-sig", "gb18030"):
        try:
            return content.decode(enc)
        except UnicodeDecodeError:
            continue
    raise RealError("无法识别文件编码（支持 UTF-8 与 GBK）")


@dataclass
class ParsedFill:
    trade_date: date
    symbol: str
    name: str
    side: str
    qty: float
    price: float
    fee: float

    def as_dict(self) -> dict:
        return {"trade_date": self.trade_date.isoformat(), "symbol": self.symbol, "name": self.name,
                "side": self.side, "qty": self.qty, "price": self.price, "fee": round(self.fee, 2)}


def parse_fills(text: str) -> dict:
    """解析券商导出的成交明细（CSV / 制表符分隔）。返回 {rows, skipped, columns}。

    识别常见列名：成交日期、证券代码、证券名称、买卖标志、成交数量、成交价格 / 成交均价、成交金额、
    手续费 / 佣金 / 印花税 / 过户费（相加）或费用合计。非买卖记录（如红利入账、转账）会被跳过并说明原因。
    """
    lines = [ln for ln in text.splitlines() if ln.strip()]
    # 跳过表头之前的说明行：找第一行同时包含日期列与代码列名称的
    head_idx = next((k for k, ln in enumerate(lines[:30])
                     if any(a in ln for a in _ALIASES["symbol"]) and any(a in ln for a in _ALIASES["date"])), 0)
    body = "\n".join(lines[head_idx:])
    sample = lines[head_idx] if lines else ""
    delim = "\t" if sample.count("\t") > sample.count(",") else ","
    reader = csv.DictReader(io.StringIO(body), delimiter=delim)
    headers = [h for h in (reader.fieldnames or []) if h]
    cols = {k: _find(headers, k) for k in _ALIASES}
    missing = [k for k in ("date", "symbol", "side", "qty", "price") if not cols[k]]
    if missing:
        names = {"date": "成交日期", "symbol": "证券代码", "side": "买卖方向", "qty": "成交数量", "price": "成交价格"}
        raise RealError("缺少必要的列：" + "、".join(names[k] for k in missing)
                        + f"（识别到的列：{'、'.join(headers)}）")
    fee_cols = [] if cols["fee_total"] else [h for h in headers if h.strip() in _FEE_PARTS]
    rows, skipped = [], []
    for n, r in enumerate(reader, start=head_idx + 2):
        d, sym = parse_date(r.get(cols["date"], "")), normalize_symbol(r.get(cols["symbol"], ""))
        side = _side(r.get(cols["side"], ""))
        qty, price = _num(r.get(cols["qty"])), _num(r.get(cols["price"]))
        why = None
        if side is None:
            why = f"不是买卖记录（{str(r.get(cols['side'], '')).strip() or '空'}）"
        elif d is None:
            why = "日期无法识别"
        elif sym is None:
            why = f"代码无法识别（{r.get(cols['symbol'], '')}）"
        elif not qty or not price or price <= 0:
            why = "数量或价格为空"
        if why:
            skipped.append({"line": n, "reason": why})
            continue
        if cols["fee_total"]:
            fee = _num(r.get(cols["fee_total"])) or 0.0
        else:
            fee = sum(_num(r.get(h)) or 0.0 for h in fee_cols)
        rows.append(ParsedFill(d, sym, str(r.get(cols["name"], "") or "").strip() if cols["name"] else "",
                               side, abs(qty), price, abs(fee)))
    return {"rows": rows, "skipped": skipped,
            "columns": {k: v for k, v in cols.items() if v} | ({"fee_parts": fee_cols} if fee_cols else {})}


def add_fills(s: Session, acc: RealAccountRow, rows: list[ParsedFill], source: str) -> dict:
    """写入成交；与已有记录完全相同（日期、代码、方向、数量、价格）的视为重复跳过。"""
    have = {(f.trade_date, f.symbol, f.side, round(f.qty, 4), round(f.price, 4))
            for f in s.scalars(select(RealFillRow).where(RealFillRow.real_account_id == acc.id))}
    added = dup = 0
    for r in rows:
        key = (r.trade_date, r.symbol, r.side, round(r.qty, 4), round(r.price, 4))
        if key in have:
            dup += 1
            continue
        name = r.name
        if not name:
            inst = s.get(InstrumentRow, r.symbol)
            name = inst.name if inst else ""
        s.add(RealFillRow(real_account_id=acc.id, trade_date=r.trade_date, symbol=r.symbol, name=name,
                          side=r.side, qty=r.qty, price=r.price, fee=r.fee, source=source))
        have.add(key)
        added += 1
    return {"added": added, "duplicates": dup}


# ---------------------------------------------------------------- 实盘净值


def sim_account_id(s: Session, acc: RealAccountRow) -> int | None:
    ref = s.get(QuantStrategyRow if acc.kind == "strategy" else QuantPortfolioRow, acc.ref_id)
    return ref.follow_account_id if ref else None


def benchmark_of(s: Session, acc: RealAccountRow) -> str:
    """对应策略（组合取第一个成员）的基准；只用来确定交易日历。"""
    if acc.kind == "strategy":
        st = s.get(QuantStrategyRow, acc.ref_id)
    else:
        pf = s.get(QuantPortfolioRow, acc.ref_id)
        st = s.get(QuantStrategyRow, int(pf.members[0]["strategy_id"])) if pf and pf.members else None
    return st.benchmark if st else "000300.SH"


def ref_name(s: Session, acc: RealAccountRow) -> str:
    ref = s.get(QuantStrategyRow if acc.kind == "strategy" else QuantPortfolioRow, acc.ref_id)
    return ref.name if ref else "（已删除）"


def real_nav(s: Session, acc: RealAccountRow, end: date | None = None) -> dict:
    """按成交重建每日持仓、现金与净值。返回 {nav: Series, positions: {代码: 股数}, warnings}。"""
    fills = list(s.scalars(select(RealFillRow).where(RealFillRow.real_account_id == acc.id)
                           .order_by(RealFillRow.trade_date, RealFillRow.id)))
    end = end or date.today()
    symbols = sorted({f.symbol for f in fills})
    panel = load_panel(s, symbols, benchmark_of(s, acc), acc.start_date, end)
    dates = [d for d in panel.dates if d >= acc.start_date]
    if not dates:
        raise RealError("开始日期之后还没有行情数据")
    flows: dict[date, float] = {}
    for c in acc.cash_flows or []:
        d = parse_date(c.get("date", ""))
        if d:
            flows[d] = flows.get(d, 0.0) + float(c.get("amount") or 0)
    by_day: dict[date, list[RealFillRow]] = {}
    warnings: list[str] = []
    for f in fills:
        if f.trade_date < acc.start_date:
            warnings.append(f"{f.trade_date} {f.symbol} 的成交早于账户开始日期，已忽略")
            continue
        by_day.setdefault(f.trade_date, []).append(f)
    missing = [x for x in symbols if x not in panel.col]
    if missing:
        warnings.append("以下标的没有本地行情，按最近成交价估值：" + "、".join(missing))
    cash = float(acc.initial_cash)
    shares: dict[str, float] = {}
    last_px: dict[str, float] = {}
    navs, cash_series = [], []
    i0 = panel.locate(dates[0], "after")
    pending_days = sorted(d for d in by_day if d < dates[0])
    for d in pending_days:                     # 开始日期是非交易日时，把之前的成交并到第一个交易日
        by_day.setdefault(dates[0], []).extend(by_day.pop(d))
    flow_days = sorted(flows)
    fk = 0
    for i in range(i0, len(panel.dates)):
        d = panel.dates[i]
        if i > i0:                              # 分红送转：按复权因子变化调整持股数
            for x in list(shares):
                j = panel.col.get(x)
                if j is not None:
                    ratio = panel.factor[i, j] / panel.factor[i - 1, j]
                    if np.isfinite(ratio) and abs(ratio - 1) > 1e-9:
                        shares[x] *= float(ratio)
        while fk < len(flow_days) and flow_days[fk] <= d:
            cash += flows[flow_days[fk]]
            fk += 1
        for f in by_day.get(d, []):
            amt = f.qty * f.price
            if f.side == "buy":
                cash -= amt + f.fee
                shares[f.symbol] = shares.get(f.symbol, 0.0) + f.qty
            else:
                cash += amt - f.fee
                shares[f.symbol] = shares.get(f.symbol, 0.0) - f.qty
                if shares[f.symbol] < -1e-6:
                    warnings.append(f"{d} 卖出 {f.symbol} 超过持仓（缺少更早的买入记录？）")
                if abs(shares[f.symbol]) < 1e-6:
                    shares.pop(f.symbol)
            last_px[f.symbol] = f.price
        value = 0.0
        for x, q in shares.items():
            j = panel.col.get(x)
            px = float(panel.close_ff[i, j]) if j is not None and np.isfinite(panel.close_ff[i, j]) else None
            if px is not None:
                last_px[x] = px
            value += q * (px if px is not None else last_px.get(x, 0.0))
        navs.append(cash + value)
        cash_series.append(cash)
    if cash < -1:
        warnings.append(f"现金为负（{cash:,.0f} 元）：可能缺少资金转入记录或初始资金填写偏小")
    idx = pd.Index(panel.dates[i0:])
    return {"nav": pd.Series(navs, index=idx), "cash": pd.Series(cash_series, index=idx),
            "flows": flows, "positions": shares, "last_px": last_px, "panel": panel,
            "warnings": list(dict.fromkeys(warnings))}


def _twr(nav: pd.Series, flows: dict[date, float]) -> pd.Series:
    """扣除资金转入转出后的时间加权净值（从 1 开始）。"""
    r = []
    prev = None
    for d, v in nav.items():
        if prev is None:
            r.append(0.0)
        else:
            base = prev + flows.get(d, 0.0)
            r.append(v / base - 1 if base > 0 else 0.0)
        prev = v
    return (1 + pd.Series(r, index=nav.index)).cumprod()


# ---------------------------------------------------------------- 实盘 vs 模拟


def report(s: Session, acc: RealAccountRow) -> dict:
    """实盘与对应实时跟踪（模拟账户）的对比报告。"""
    base = {"name": acc.name, "ref_name": ref_name(s, acc), "kind": acc.kind,
            "start_date": acc.start_date.isoformat(), "initial_cash": acc.initial_cash}
    rn = real_nav(s, acc)
    nav, warnings = rn["nav"], rn["warnings"]
    real_w = _twr(nav, rn["flows"])
    sim_id = sim_account_id(s, acc)
    fills = list(s.scalars(select(RealFillRow).where(RealFillRow.real_account_id == acc.id)
                           .order_by(RealFillRow.trade_date, RealFillRow.id)))
    out = base | {"warnings": warnings, "fills": len(fills),
                  "real_return": round(float(real_w.iloc[-1] - 1), 4), "end": nav.index[-1].isoformat(),
                  "nav_last": round(float(nav.iloc[-1]), 2), "fees_real": round(sum(f.fee for f in fills), 2)}
    if not sim_id:
        return out | {"status": "无模拟", "message": "对应的策略 / 组合还没有开始实时跟踪，无法对比"}
    sim_rows = s.execute(select(NavSnapshotRow.date, NavSnapshotRow.nav).where(
        NavSnapshotRow.account_id == sim_id, NavSnapshotRow.date >= acc.start_date)
        .order_by(NavSnapshotRow.date)).all()
    if len(sim_rows) < 2:
        return out | {"status": "数据不足", "message": "模拟账户在实盘开始日期之后的净值不足两个交易日"}
    sim = pd.Series([float(v) for _, v in sim_rows], index=pd.Index([d for d, _ in sim_rows]))
    common = real_w.index.intersection(sim.index)
    if len(common) < 2:
        return out | {"status": "数据不足", "message": "实盘与模拟重叠的交易日不足两天"}
    rw, sw = real_w.reindex(common), sim.reindex(common)
    rw, sw = rw / rw.iloc[0], sw / sw.iloc[0]
    rr, sr = rw.pct_change().dropna(), sw.pct_change().dropna()
    days = len(rr)
    te = float((rr - sr).std(ddof=1) * math.sqrt(252)) if days > 1 else 0.0
    real_ret, sim_ret = float(rw.iloc[-1] - 1), float(sw.iloc[-1] - 1)
    gap = real_ret - sim_ret
    gap_annual = gap * 252 / max(days, 1) if days >= 60 else gap
    if days < MIN_DAYS:
        status = "数据不足"
    elif te <= TE_OK and abs(gap_annual) <= GAP_OK:
        status = "一致"
    elif te <= TE_WARN and abs(gap_annual) <= GAP_WARN:
        status = "有偏差"
    else:
        status = "偏差较大"
    execution = _execution(s, fills, sim_id, common[0], common[-1], rn["panel"])
    holdings = _holdings(s, rn, sim_id, common[-1])
    msg = {"一致": "实盘与模拟基本一致，执行损耗在可接受范围内",
           "有偏差": "实盘与模拟有一定偏差，查看下方的执行对照找原因",
           "偏差较大": "实盘与模拟偏差较大：检查是否漏做 / 多做了交易、下单时间与价格，或成交记录是否完整",
           "数据不足": f"重叠交易日不足 {MIN_DAYS} 天，继续积累"}[status]
    return out | {
        "status": status, "message": msg, "days": days, "compare_start": common[0].isoformat(),
        "real_return": round(real_ret, 4), "sim_return": round(sim_ret, 4), "gap": round(gap, 4),
        "gap_annual": round(gap_annual, 4), "tracking_error": round(te, 4),
        "correlation": round(float(rr.corr(sr)), 3) if days > 2 else None,
        "curve": {"dates": [d.isoformat() for d in common], "real": [round(float(v), 5) for v in rw],
                  "sim": [round(float(v), 5) for v in sw]},
        "execution": execution, "holdings": holdings,
    }


def _execution(s: Session, fills: list[RealFillRow], sim_id: int, start: date, end: date, panel) -> dict:
    """逐笔执行对照：实盘成交按（标的、方向）与模拟成交配对，实盘日期在模拟日期后 0～MATCH_DAYS 个交易日内。"""
    sims = list(s.scalars(select(FillRow).where(FillRow.account_id == sim_id, FillRow.trade_date >= start,
                                                FillRow.trade_date <= end).order_by(FillRow.trade_date)))
    pos = {d: k for k, d in enumerate(panel.dates)}
    names = {x: panel.names[j] for x, j in panel.col.items()}
    real = [f for f in fills if start <= f.trade_date <= end]
    used: set[int] = set()
    pairs, extra = [], []
    for f in real:
        best, best_lag = None, None
        for k, m in enumerate(sims):
            side = "buy" if m.side in ("buy", "cover") else "sell"
            if k in used or m.symbol != f.symbol or side != f.side:
                continue
            lag = pos.get(f.trade_date, 0) - pos.get(m.trade_date, 0)
            if 0 <= lag <= MATCH_DAYS and (best_lag is None or lag < best_lag):
                best, best_lag = k, lag
        if best is None:
            extra.append(f)
            continue
        used.add(best)
        m = sims[best]
        dev = f.price / float(m.price) - 1
        dev = dev if f.side == "buy" else -dev                  # 正数表示实盘成交价更差
        pairs.append({"date": f.trade_date.isoformat(), "sim_date": m.trade_date.isoformat(), "symbol": f.symbol,
                      "name": f.name or names.get(f.symbol, f.symbol), "side": f.side, "lag": best_lag,
                      "real_qty": f.qty, "sim_qty": m.qty, "real_price": f.price, "sim_price": float(m.price),
                      "dev_bps": round(dev * 1e4, 1), "cost": round(dev * f.qty * f.price, 2)})
    missed = [m for k, m in enumerate(sims) if k not in used]
    amt = sum(p["real_qty"] * p["real_price"] for p in pairs)
    return {
        "matched": len(pairs), "real_trades": len(real), "sim_trades": len(sims),
        "avg_dev_bps": round(sum(p["dev_bps"] * p["real_qty"] * p["real_price"] for p in pairs) / amt, 1)
        if amt else None,
        "price_cost": round(sum(p["cost"] for p in pairs), 2),
        "avg_lag": round(float(np.mean([p["lag"] for p in pairs])), 2) if pairs else None,
        "fees_sim": round(sum(float(m.total_fee) for m in sims), 2),
        "pairs": sorted(pairs, key=lambda p: -abs(p["cost"]))[:50],
        "missed": [{"date": m.trade_date.isoformat(), "symbol": m.symbol, "name": names.get(m.symbol, m.symbol),
                    "side": "buy" if m.side in ("buy", "cover") else "sell", "qty": m.qty,
                    "amount": round(float(m.amount), 2)} for m in missed][:50],
        "missed_count": len(missed),
        "extra": [{"date": f.trade_date.isoformat(), "symbol": f.symbol, "name": f.name or names.get(f.symbol, ""),
                   "side": f.side, "qty": f.qty, "amount": round(f.qty * f.price, 2)} for f in extra][:50],
        "extra_count": len(extra),
    }


def _holdings(s: Session, rn: dict, sim_id: int, d: date) -> dict:
    """最新持仓权重对照（实盘按重建的持股，模拟按跟踪账户当前持仓）。"""
    from app.models import PositionRow

    panel = rn["panel"]
    nav_real = float(rn["nav"].iloc[-1])
    real_w = {x: q * rn["last_px"].get(x, 0.0) / nav_real for x, q in rn["positions"].items() if q > 1e-6}
    sim_nav = s.scalars(select(NavSnapshotRow.nav).where(NavSnapshotRow.account_id == sim_id)
                        .order_by(NavSnapshotRow.date.desc()).limit(1)).first()
    sim_w: dict[str, float] = {}
    if sim_nav:
        i = panel.locate(d, "before")
        for p in s.scalars(select(PositionRow).where(PositionRow.account_id == sim_id)):
            if not p.qty:
                continue
            j = panel.col.get(p.symbol)
            px = float(panel.close_ff[i, j]) if j is not None and np.isfinite(panel.close_ff[i, j]) else None
            if px is None:
                inst_px = s.scalars(select(FillRow.price).where(FillRow.account_id == sim_id,
                                                                FillRow.symbol == p.symbol)
                                    .order_by(FillRow.trade_date.desc()).limit(1)).first()
                px = float(inst_px) if inst_px is not None else 0.0
            sim_w[p.symbol] = p.qty * px / float(sim_nav)
    syms = set(real_w) | set(sim_w)
    names = {x: panel.names[j] for x, j in panel.col.items()}
    for x in syms - set(names):
        inst = s.get(InstrumentRow, x)
        names[x] = inst.name if inst else x
    rows = sorted(({"symbol": x, "name": names.get(x, x), "real": round(real_w.get(x, 0.0), 4),
                    "sim": round(sim_w.get(x, 0.0), 4)} for x in syms), key=lambda r: -abs(r["real"] - r["sim"]))
    return {"overlap": round(sum(min(real_w.get(x, 0), sim_w.get(x, 0)) for x in syms), 4),
            "real_exposure": round(sum(real_w.values()), 4), "sim_exposure": round(sum(sim_w.values()), 4),
            "rows": rows[:40]}


def account_dict(s: Session, acc: RealAccountRow) -> dict:
    n = len(s.scalars(select(RealFillRow.id).where(RealFillRow.real_account_id == acc.id)).all())
    return {"id": acc.id, "name": acc.name, "kind": acc.kind, "ref_id": acc.ref_id, "ref_name": ref_name(s, acc),
            "initial_cash": acc.initial_cash, "start_date": acc.start_date.isoformat(),
            "cash_flows": acc.cash_flows or [], "note": acc.note, "fills": n,
            "sim_account_id": sim_account_id(s, acc), "created_at": acc.created_at.isoformat()}


def fill_dict(f: RealFillRow) -> dict:
    return {"id": f.id, "trade_date": f.trade_date.isoformat(), "symbol": f.symbol, "name": f.name, "side": f.side,
            "qty": f.qty, "price": f.price, "amount": round(f.qty * f.price, 2), "fee": f.fee, "source": f.source,
            "note": f.note}
