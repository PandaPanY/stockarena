"""股东户数变化因子的一次性检验——严格按事先写定的预注册（预注册文档已归档，未包含在本仓库）。

命令（在服务器上运行，数据与结果在 data/research/holders/）：
  uv run python -m app.quant.holders fetch      # 下载中证800 半时点股票池全部股票的股东户数（可重复运行，只补缺的）
  uv run python -m app.quant.holders dev        # 开发期 P1、P2、P4 与参考信息
  uv run python -m app.quant.holders holdout    # 留出期 P3：只有开发期全部通过才计算，且只算一次（结果存档后不再重算）
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import time
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from app.core.config import get_settings

logger = logging.getLogger(__name__)

UNIVERSE = "cn_csi800_pit_etf"
HORIZON, STEP, MIN_STOCKS = 20, 20, 30
DEV = (date(2018, 1, 1), date(2024, 12, 31))
HOLDOUT_START = date(2025, 1, 1)
STALE_DAYS = 200
MOM_WINDOW = 60


def out_dir() -> Path:
    d = get_settings().data_dir / "research" / "holders"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------- 数据


def _panel():
    from app.db.session import get_sessionmaker
    from app.quant.data import get_panel
    from app.quant.service import LabSettings

    with get_sessionmaker()() as s:
        return get_panel(s, UNIVERSE, LabSettings().train_start, date.today())


def _parse_records(df: pd.DataFrame) -> list[dict]:
    out = []
    for r in df.itertuples(index=False):
        rec = dict(zip(df.columns, r, strict=True))
        try:
            out.append({"period": str(pd.Timestamp(rec["股东户数统计截止日"]).date()),
                        "announced": str(pd.Timestamp(rec["股东户数公告日期"]).date()),
                        "now": float(rec["股东户数-本次"]), "prev": float(rec["股东户数-上次"])})
        except (TypeError, ValueError, KeyError):
            continue
    return out


def fetch(sleep: float = 0.3) -> dict:
    """下载股东户数（每只一次请求，带重试）；已下载的跳过。"""
    import akshare as ak

    panel = _panel()
    symbols = [s for s, t in zip(panel.symbols, panel.types, strict=True) if t == "stock"]
    path = out_dir() / "gdhs.json"
    data: dict = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    todo = [s for s in symbols if s not in data]
    print(f"股票 {len(symbols)} 只，已有 {len(symbols) - len(todo)} 只，待下载 {len(todo)} 只")
    for k, sym in enumerate(todo, 1):
        for attempt in range(3):
            try:
                df = ak.stock_zh_a_gdhs_detail_em(symbol=sym.split(".")[0])
                data[sym] = _parse_records(df)
                break
            except Exception as e:  # noqa: BLE001
                if attempt == 2:
                    print(f"  {sym} 失败：{e}")
                time.sleep(2 * (attempt + 1))
        time.sleep(sleep)
        if k % 50 == 0:
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            print(f"  {k}/{len(todo)}")
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    missing = [s for s in symbols if s not in data]
    return {"symbols": len(symbols), "downloaded": len(data), "missing": missing}


def load_records() -> dict[str, list[dict]]:
    return json.loads((out_dir() / "gdhs.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 因子


def build_factor(panel, records: dict[str, list[dict]]) -> tuple[np.ndarray, dict]:
    """(T, N) 因子：最近一条已可用记录的 −(本次/上次 − 1)；统计截止日超过 STALE_DAYS 天记为缺失。"""
    T, N = panel.shape
    out = np.full((T, N), np.nan)
    dates64 = np.array(panel.dates, dtype="datetime64[D]")
    lags = []
    for sym, recs in records.items():
        j = panel.col.get(sym)
        if j is None:
            continue
        rows = []
        for r in recs:
            if r["prev"] <= 0 or r["now"] <= 0 or r["announced"] < r["period"]:
                continue
            rows.append((date.fromisoformat(r["announced"]), date.fromisoformat(r["period"]),
                         -(r["now"] / r["prev"] - 1)))
        # 同一天公告多条：取统计截止日最新的一条（按公告日、截止日排序后，后面的覆盖前面的）
        rows.sort()
        for k, (ann, period, val) in enumerate(rows):
            lags.append((ann - period).days)
            # 记在公告日当天或之前最后一个交易日
            i0 = int(np.searchsorted(dates64, np.datetime64(ann), side="right")) - 1
            if i0 < 0:
                continue
            nxt = rows[k + 1][0] if k + 1 < len(rows) else None
            i1 = (int(np.searchsorted(dates64, np.datetime64(nxt), side="right")) - 1) if nxt else T
            stale = int(np.searchsorted(dates64, np.datetime64(period + timedelta(days=STALE_DAYS)), side="right"))
            i1 = min(i1, stale)
            if i1 > i0:
                out[i0:i1, j] = val
    return out, {"announce_lag_days": round(float(np.mean(lags)), 1) if lags else None}


# ---------------------------------------------------------------- 检验


def _residual_rank(v: np.ndarray, controls: list[np.ndarray]) -> np.ndarray:
    """因子的秩对控制变量的秩做截面回归，返回残差（P4）。"""
    from app.quant.factor_research import _rank

    y = _rank(v)
    x = np.column_stack([np.ones(len(v)), *[_rank(c) for c in controls]])
    beta, *_ = np.linalg.lstsq(x, y, rcond=None)
    return y - x @ beta


def ic_test(panel, factor: np.ndarray, start: date, end: date, neutral: bool = False) -> dict:
    from app.quant import factors
    from app.quant.factor_research import _rank, _universe

    adj_open = panel.open * panel.factor
    size = factors.compute(panel, "size")
    mom = factors.compute(panel, "momentum", {"window": MOM_WINDOW})
    i0, i1 = panel.locate(start, "after"), min(panel.locate(end, "before"), len(panel.dates) - 1 - HORIZON)
    ics, years, coverage, corr_size, corr_mom, quint = [], [], [], [], [], []
    for i in range(i0, i1 + 1, STEP):
        fwd = panel.adj_close[i + HORIZON] / adj_open[i + 1] - 1
        uni = _universe(panel, i) & panel.has_bar[i + 1] & np.isfinite(fwd)
        v = factor[i]
        m = uni & np.isfinite(v)
        if neutral:
            m &= np.isfinite(size[i]) & np.isfinite(mom[i])
        if m.sum() < MIN_STOCKS or np.ptp(v[m]) == 0:
            continue
        coverage.append(m.sum() / max(uni.sum(), 1))
        x = _residual_rank(v[m], [size[i][m], mom[i][m]]) if neutral else v[m]
        ic = np.corrcoef(_rank(x), _rank(fwd[m]))[0, 1]
        if not np.isfinite(ic):
            continue
        ics.append(float(ic))
        years.append(panel.dates[i].year)
        if not neutral:
            ok = m & np.isfinite(size[i]) & np.isfinite(mom[i])
            if ok.sum() >= MIN_STOCKS:
                corr_size.append(np.corrcoef(_rank(v[ok]), _rank(size[i][ok]))[0, 1])
                corr_mom.append(np.corrcoef(_rank(v[ok]), _rank(mom[i][ok]))[0, 1])
            q = pd.qcut(_rank(v[m]), 5, labels=False)
            quint.append([float(np.mean(fwd[m][q == k])) for k in range(5)])
    arr = np.array(ics)
    if len(arr) < 6:
        return {"periods": len(arr)}
    mean, std = float(arr.mean()), float(arr.std(ddof=1))
    per_year = pd.Series(ics).groupby(years).mean()
    out = {"periods": len(arr), "ic": round(mean, 4), "t": round(mean / std * math.sqrt(len(arr)), 2) if std else None,
           "positive": round(float((arr > 0).mean()), 3),
           "years": {int(y): round(float(x), 4) for y, x in per_year.items()},
           "coverage": round(float(np.mean(coverage)), 3)}
    if not neutral:
        out["corr_size"] = round(float(np.nanmean(corr_size)), 3) if corr_size else None
        out["corr_momentum"] = round(float(np.nanmean(corr_mom)), 3) if corr_mom else None
        if quint:
            out["quintiles"] = [round(float(x), 4) for x in np.mean(np.array(quint), axis=0)]
    return out


def dev_verdict(raw: dict, neutral: dict) -> dict:
    p1 = bool(raw.get("ic", 0) > 0 and (raw.get("t") or 0) >= 2.0)
    years = raw.get("years", {})
    dev_years = [y for y in range(DEV[0].year, DEV[1].year + 1) if y in years]
    p2 = sum(1 for y in dev_years if years[y] > 0) >= 5 and len(dev_years) == 7
    p4 = bool(neutral.get("ic", 0) > 0 and (neutral.get("t") or 0) >= 2.0)
    return {"P1": p1, "P2": p2, "P4": p4, "pass": p1 and p2 and p4}


def run_dev() -> dict:
    panel = _panel()
    factor, info = build_factor(panel, load_records())
    raw = ic_test(panel, factor, *DEV)
    neutral = ic_test(panel, factor, *DEV, neutral=True)
    res = {"raw": raw, "neutral": neutral, "verdict": dev_verdict(raw, neutral), **info,
           "data_end": panel.dates[-1].isoformat()}
    (out_dir() / "dev.json").write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    return res


def run_holdout() -> dict:
    """留出期只算一次：开发期没通过时拒绝；已有结果时直接返回存档。"""
    path = out_dir() / "holdout.json"
    if path.exists():
        return {"note": "留出期已计算过，以下为存档结果（不重算）", **json.loads(path.read_text(encoding="utf-8"))}
    dev_path = out_dir() / "dev.json"
    if not dev_path.exists():
        raise SystemExit("请先运行 dev")
    dev = json.loads(dev_path.read_text(encoding="utf-8"))
    if not dev["verdict"]["pass"]:
        raise SystemExit("开发期未通过，按预注册不计算留出期")
    panel = _panel()
    factor, _ = build_factor(panel, load_records())
    hold = ic_test(panel, factor, HOLDOUT_START, panel.dates[-1])
    dev_ic = dev["raw"]["ic"]
    p3 = bool(hold.get("ic", 0) > 0 and hold.get("ic", 0) >= 0.5 * dev_ic)
    res = {"holdout": hold, "P3": p3, "dev_ic": dev_ic, "data_end": panel.dates[-1].isoformat()}
    path.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    return res


def main() -> None:
    ap = argparse.ArgumentParser(description="股东户数变化因子检验（按预注册）")
    ap.add_argument("cmd", choices=["fetch", "dev", "holdout"])
    args = ap.parse_args()
    fn = {"fetch": fetch, "dev": run_dev, "holdout": run_holdout}[args.cmd]
    print(json.dumps(fn(), ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
