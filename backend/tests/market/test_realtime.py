from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.market.realtime import MinuteBarBuilder, Quote, parse_response, sina_code

# 2026-10-02 实测返回（截取）
SAMPLE = (
    'var hq_str_rt_hk00700="TENCENT,腾讯控股,422.000,431.000,425.000,420.000,420.400,-10.600,-2.459,420.400,'
    '420.600,5659148894.922,13400156,15.276,0.000,674.640,411.000,2026/10/02,14:51:32,30|3,N|Y|Y";\n'
    'var hq_str_gb_aapl="苹果,330.3200,-0.81,2026-10-02 09:30:10,-2.7000,330.0000,332.4816,325.8100,345.3400,'
    '242.8900,36306344,40668070,4820749537930,8.30,39.800000,0.00,0.00,0.00,0.00,14594180001,63,330.7999,0.15,'
    '0.48,Oct 01 07:59PM EDT,Oct 01 04:00PM EDT,333.0200,2647933,1,2026";\n'
    'var hq_str_sh600519="贵州茅台,1239.530,1235.580,1258.620,1268.000,1236.050,1258.620,1258.650,3833098,'
    '4797246636.000,1445,1258.620,100,1258.440,100,1258.160,200,1258.050,4100,1258.000,200,1258.650,300,'
    '1258.660,100,1258.680,200,1258.690,8000,1258.750,2026-09-30,15:34:59,00";\n'
    'var hq_str_sz000001="";\n'
)
CODES = {"rt_hk00700": "00700.HK", "gb_aapl": "AAPL.US", "sh600519": "600519.SH", "sz000001": "000001.SZ"}


def test_sina_code():
    assert sina_code("00700.HK") == "rt_hk00700"
    assert sina_code("AAPL.US") == "gb_aapl"
    assert sina_code("510300.SH") == "sh510300"


def test_parse_three_markets():
    q = parse_response(SAMPLE, CODES)
    assert set(q) == {"00700.HK", "AAPL.US", "600519.SH"}           # 空行情被忽略
    hk = q["00700.HK"]
    assert (hk.price, hk.open, hk.high, hk.low, hk.prev_close, hk.volume) == (
        Decimal("420.400"), Decimal("422.000"), Decimal("425.000"), Decimal("420.000"), Decimal("431.000"), 13400156)
    assert hk.quote_date == "2026-10-02"
    us = q["AAPL.US"]
    assert (us.price, us.prev_close, us.open, us.volume) == (
        Decimal("330.3200"), Decimal("333.0200"), Decimal("330.0000"), 36306344)
    cn = q["600519.SH"]
    assert (cn.price, cn.prev_close, cn.volume, cn.quote_date) == (
        Decimal("1258.620"), Decimal("1235.580"), 3833098, "2026-09-30")


def _q(price: str, volume: int) -> dict[str, Quote]:
    p = Decimal(price)
    return {"X.US": Quote("X.US", p, p, p, p, Decimal("10"), volume, Decimal("0"), "2026-10-02")}


def test_minute_bar_builder():
    b = MinuteBarBuilder()
    t = lambda m, s: datetime(2026, 10, 2, 14, m, s, tzinfo=UTC)  # noqa: E731
    assert b.update(_q("10.0", 1000), t(30, 5)) == []
    b.update(_q("10.4", 1500), t(30, 20))
    b.update(_q("9.8", 1800), t(30, 50))
    [bar] = b.update(_q("10.1", 2000), t(31, 5))              # 分钟切换，输出 14:30 的 K 线
    assert (bar.open, bar.high, bar.low, bar.close) == (Decimal("10.0"), Decimal("10.4"), Decimal("9.8"),
                                                        Decimal("9.8"))
    assert bar.ts == t(30, 0) and bar.cum_volume == 1800
    [bar2] = b.flush()
    assert bar2.ts == t(31, 0) and bar2.volume == 200          # 2000 - 1800（上一分钟结束时的累计量）
