"""从 CSV 导入日线：在线数据源都不可用时的备选，也可以导入自己从别处下载的数据。

命令（在 backend/ 目录）：
  uv run python -m app.market.csv_import 文件或目录 [文件或目录 ...]

CSV 第一行是表头（大小写不敏感），一个文件可以包含多只证券：
  symbol,date,open,high,low,close,volume[,amount][,prev_close][,adj_factor]

- symbol：600519.SH、600519 或 sh600519（A股六位代码自动补交易所后缀）；港股、美股写完整代码（如 00700.HK、AAPL.US）；
- date：2026-07-10 或 20260710；
- volume：成交量（股 / 份）；amount：成交额（元），缺省时按 收盘价 × 成交量 估算；
- prev_close：交易所前收盘价（除权日为除权参考价）；缺省时取上一行的收盘价；
- adj_factor：后复权因子；缺省时按 前一天收盘 ÷ 当天 prev_close 逐日推算（没有 prev_close 列时为 1，
  即不复权：有分红送转的股票在除权日会出现价格跳变，回测结果不准确）。

已缓存的日期会被覆盖；与缓存重叠时复权因子自动换算到缓存的基准。只导入数据库里已有的证券（在策略库、股票池里
用过的），其余的列出来跳过。
"""

from __future__ import annotations

import logging
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pandas as pd
from sqlalchemy.orm import Session

from app.market.types import DailyBar
from app.models.market import DailyBarRow, InstrumentRow

logger = logging.getLogger(__name__)

REQUIRED = ("symbol", "date", "open", "high", "low", "close", "volume")


class CsvImportError(ValueError):
    pass


def _symbol(s: str) -> str:
    from app.market.symbols import normalize_cn

    s = str(s).strip()
    if "." in s:
        return s.upper()
    return normalize_cn(s.zfill(6) if s.isdigit() else s)


def _dec(v) -> Decimal | None:
    if v is None or (isinstance(v, float) and pd.isna(v)) or str(v).strip() == "":
        return None
    try:
        return Decimal(str(v).strip())
    except InvalidOperation as e:
        raise CsvImportError(f"无法识别的数字：{v}") from e


def read_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, dtype=str, encoding="utf-8-sig")
    df.columns = [c.strip().lower() for c in df.columns]
    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        raise CsvImportError(f"{path.name} 缺少列：{', '.join(missing)}")
    return df


def bars_from_frame(df: pd.DataFrame) -> dict[str, list[DailyBar]]:
    """按证券分组、按日期排序，补齐成交额、前收盘价和复权因子。"""
    out: dict[str, list[DailyBar]] = {}
    df = df.copy()
    df["symbol"] = df["symbol"].map(_symbol)
    df["date"] = pd.to_datetime(df["date"].astype(str).str.strip(), format="mixed").dt.date
    for sym, g in df.groupby("symbol", sort=True):
        g = g.drop_duplicates("date", keep="last").sort_values("date")
        bars: list[DailyBar] = []
        prev_close: Decimal | None = None
        factor = Decimal("1")
        for r in g.to_dict("records"):
            close = _dec(r["close"])
            if close is None or close <= 0:
                raise CsvImportError(f"{sym} {r['date']} 收盘价无效")
            volume = int(_dec(r["volume"]) or 0)
            amount = _dec(r.get("amount")) if "amount" in r else None
            pre = _dec(r.get("prev_close")) if "prev_close" in r else None
            given = _dec(r.get("adj_factor")) if "adj_factor" in r else None
            if given is not None:
                factor = given
            elif pre is not None and prev_close is not None and pre > 0 and pre != prev_close:
                factor = (factor * prev_close / pre).quantize(Decimal("0.0000000001"))
            bars.append(DailyBar(
                symbol=sym, date=r["date"], open=_dec(r["open"]), high=_dec(r["high"]), low=_dec(r["low"]),
                close=close, volume=volume, amount=amount if amount is not None else close * volume,
                prev_close=pre if pre is not None else prev_close, adj_factor=factor))
            prev_close = close
        out[sym] = bars
    return out


def import_bars(s: Session, data: dict[str, list[DailyBar]]) -> dict[str, str]:
    """写入日线缓存（逐只提交）。返回 {代码: 结果}。"""
    from app.market.service import MarketDataService

    result: dict[str, str] = {}
    for sym, bars in data.items():
        row = s.get(InstrumentRow, sym)
        if row is None:
            result[sym] = "跳过：数据库里没有这只证券（先在策略库或股票池里用到它）"
            continue
        svc = MarketDataService(s, _NoProvider(row.market))
        bars = svc._align_factors(sym, bars)
        for b in bars:
            s.merge(DailyBarRow(symbol=b.symbol, date=b.date, open=b.open, high=b.high, low=b.low, close=b.close,
                                volume=b.volume, amount=b.amount, prev_close=b.prev_close, adj_factor=b.adj_factor))
        first = bars[0].date
        if row.history_from is None or first < row.history_from:
            row.history_from = first        # 之后不再向在线数据源请求更早的历史
        s.commit()
        result[sym] = f"导入 {len(bars)} 根（{first} ～ {bars[-1].date}）"
    return result


class _NoProvider:
    def __init__(self, market: str):
        self.market = market


def _files(args: list[str]) -> list[Path]:
    out: list[Path] = []
    for a in args:
        p = Path(a)
        if p.is_dir():
            out += sorted(p.glob("*.csv"))
        elif p.is_file():
            out.append(p)
        else:
            raise CsvImportError(f"找不到：{a}")
    return out


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO)
    if not argv:
        print(__doc__)
        return 2
    from app.db.session import get_sessionmaker

    try:
        files = _files(argv)
        frames = [read_csv(f) for f in files]
        data = bars_from_frame(pd.concat(frames, ignore_index=True)) if frames else {}
    except (CsvImportError, ValueError) as e:
        print(f"导入失败：{e}")
        return 1
    with get_sessionmaker()() as s:
        result = import_bars(s, data)
    for sym, msg in result.items():
        print(f"{sym}：{msg}")
    return 0 if all(not m.startswith("跳过") for m in result.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
