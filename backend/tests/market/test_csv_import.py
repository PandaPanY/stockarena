"""从 CSV 导入日线：代码规范化、复权因子推算、与缓存对齐、未知证券跳过、缺列报错。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.market import csv_import
from app.market.service import MarketDataService
from tests.market.test_service import INST, FakeProvider, session  # noqa: F401

CSV = """Symbol,Date,Open,High,Low,Close,Volume,Prev_Close
600036,2026-07-08,37.0,38.1,36.9,37.94,100000,37.10
sh600036,20260709,37.9,38.0,37.4,37.55,150000,37.94
600036.SH,2026-07-10,36.0,36.8,35.9,36.55,200000,36.55
000001,2026-07-10,10,10,10,10,100,
"""


def test_import_derives_factor_and_aligns_with_cache(session, tmp_path):  # noqa: F811
    svc = MarketDataService(session, FakeProvider())
    svc.upsert_instruments([INST])
    svc.update_bars([INST], until=date(2026, 7, 8), history_start=date(2026, 7, 8))       # 缓存：7-8 因子 6.5
    f = tmp_path / "bars.csv"
    f.write_text(CSV, encoding="utf-8-sig")
    data = csv_import.bars_from_frame(csv_import.read_csv(f))
    assert set(data) == {"600036.SH", "000001.SZ"}
    bars = data["600036.SH"]
    assert bars[0].adj_factor == bars[1].adj_factor == 1                # 7-9 昨收等于前一天收盘：没有除权
    assert bars[2].adj_factor == (Decimal("37.55") / Decimal("36.55")).quantize(Decimal("0.0000000001"))
    assert bars[1].amount == Decimal("37.55") * 150000                  # 缺成交额：收盘 × 成交量
    assert data["000001.SZ"][0].prev_close is None

    result = csv_import.import_bars(session, data)
    assert result["600036.SH"].startswith("导入 3 根") and result["000001.SZ"].startswith("跳过")
    got = {b.date: b for b in svc.get_bars("600036.SH", date(2026, 7, 1), date(2026, 7, 31))}
    assert got[date(2026, 7, 8)].adj_factor == Decimal("6.5")          # 按缓存的基准换算
    assert got[date(2026, 7, 10)].adj_factor == (Decimal("6.5") * bars[2].adj_factor).quantize(
        Decimal("0.0000000001"))
    assert got[date(2026, 7, 9)].volume == 150000


def test_missing_columns_and_cli(tmp_path, capsys):
    bad = tmp_path / "bad.csv"
    bad.write_text("symbol,date,close\n600036,2026-07-10,36.55\n", encoding="utf-8")
    with pytest.raises(csv_import.CsvImportError) as e:
        csv_import.read_csv(bad)
    assert "open" in str(e.value) and "volume" in str(e.value)
    assert csv_import.main([str(bad)]) == 1 and "缺少列" in capsys.readouterr().out
    assert csv_import.main([str(tmp_path / "nope.csv")]) == 1
