"""实时行情：新浪行情接口（hq.sinajs.cn）批量快照，A股 / 港股 / 美股通用。

实测（2026-10-02）：三个市场都可直连访问，港股 rt_ 前缀为实时行情；单次请求可包含上百个代码，耗时 0.02～0.4 秒。
分钟级赛道每 15 秒轮询一次，由 MinuteBarBuilder 合成 1 分钟 K 线。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation

import requests

from app.core.timeutils import ensure_utc

logger = logging.getLogger(__name__)

SINA_HQ = "https://hq.sinajs.cn/list="
BATCH = 80
_LINE = re.compile(r'var hq_str_([A-Za-z0-9_$.]+)="(.*)";')


@dataclass(frozen=True)
class Quote:
    symbol: str
    price: Decimal
    open: Decimal
    high: Decimal
    low: Decimal
    prev_close: Decimal
    volume: int            # 当日累计成交量
    amount: Decimal        # 当日累计成交额
    quote_date: str        # 行情自带的日期（用于判断是否是当天的行情）


def sina_code(symbol: str) -> str:
    code, market = symbol.rsplit(".", 1)
    if market in ("SH", "SZ", "BJ"):
        return f"{market.lower()}{code}"
    if market == "HK":
        return f"rt_hk{code}"
    if market == "US":
        return f"gb_{code.lower().replace('.', '$')}"
    raise ValueError(f"不支持的代码：{symbol}")


def _d(text: str) -> Decimal:
    try:
        return Decimal(text.strip() or "0")
    except InvalidOperation:
        return Decimal("0")


def _i(text: str) -> int:
    try:
        return int(float(text.strip() or 0))
    except ValueError:
        return 0


def parse_quote(symbol: str, raw: str) -> Quote | None:
    """解析单只证券的行情字段；无效（空、停牌未开盘价格为 0）时返回 None。"""
    f = raw.split(",")
    market = symbol.rsplit(".", 1)[1]
    try:
        if market in ("SH", "SZ", "BJ"):
            q = Quote(symbol, _d(f[3]), _d(f[1]), _d(f[4]), _d(f[5]), _d(f[2]), _i(f[8]), _d(f[9]), f[30])
        elif market == "HK":
            q = Quote(symbol, _d(f[6]), _d(f[2]), _d(f[4]), _d(f[5]), _d(f[3]), _i(f[12]), _d(f[11]),
                      f[17].replace("/", "-"))
        elif market == "US":
            price = _d(f[1])
            prev = _d(f[26]) if len(f) > 26 and _d(f[26]) > 0 else price - _d(f[4])
            q = Quote(symbol, price, _d(f[5]), _d(f[6]), _d(f[7]), prev, _i(f[10]), Decimal("0"), f[3][:10])
        else:
            return None
    except IndexError:
        return None
    return q if q.price > 0 else None


def parse_response(text: str, codes: dict[str, str]) -> dict[str, Quote]:
    """codes：新浪代码 → 统一代码。"""
    out: dict[str, Quote] = {}
    for m in _LINE.finditer(text):
        symbol = codes.get(m.group(1))
        if symbol and m.group(2):
            q = parse_quote(symbol, m.group(2))
            if q:
                out[symbol] = q
    return out


class SinaQuoteClient:
    def __init__(self, timeout: float = 10):
        self.session = requests.Session()
        self.session.trust_env = False      # 国内行情直连，不走系统代理
        self.session.headers.update({"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"})
        self.timeout = timeout

    def fetch(self, symbols: list[str]) -> dict[str, Quote]:
        out: dict[str, Quote] = {}
        for i in range(0, len(symbols), BATCH):
            batch = symbols[i:i + BATCH]
            codes = {sina_code(s): s for s in batch}
            resp = self.session.get(SINA_HQ + ",".join(codes), timeout=self.timeout)
            resp.raise_for_status()
            out.update(parse_response(resp.content.decode("gbk", errors="replace"), codes))
        return out


@dataclass
class _Building:
    minute: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    start_volume: int
    volume: int
    prev_close: Decimal


@dataclass
class MinuteBar:
    """与 DailyBar 字段兼容的分钟 K 线，交易引擎可直接使用。ts 为分钟开始时刻（UTC）。"""

    symbol: str
    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    cum_volume: int
    prev_close: Decimal | None
    amount: Decimal = Decimal("0")

    @property
    def date(self):
        return self.ts.date()

    @property
    def suspended(self) -> bool:
        return False


@dataclass
class MinuteBarBuilder:
    """把连续的行情快照合成 1 分钟 K 线：分钟切换时输出上一分钟的 K 线。

    成交量：行情给的是当日累计量，本分钟成交量 = 本分钟最后一次累计量 - 上一分钟结束时的累计量。
    """

    current: dict[str, _Building] = field(default_factory=dict)
    last_volume: dict[str, int] = field(default_factory=dict)

    def update(self, quotes: dict[str, Quote], now: datetime) -> list[MinuteBar]:
        minute = ensure_utc(now).replace(second=0, microsecond=0)
        done: list[MinuteBar] = []
        for symbol, q in quotes.items():
            b = self.current.get(symbol)
            if b is not None and b.minute != minute:
                done.append(self._finish(symbol, b))
                b = None
            if b is None:
                base = self.last_volume.get(symbol)
                start = base if base is not None and base <= q.volume else q.volume
                self.current[symbol] = _Building(minute, q.price, q.price, q.price, q.price, start, q.volume,
                                                 q.prev_close)
            else:
                b.high, b.low, b.close, b.volume = max(b.high, q.price), min(b.low, q.price), q.price, q.volume
        return done

    def flush(self) -> list[MinuteBar]:
        """收盘或休市时输出所有未完成的分钟 K 线。"""
        done = [self._finish(s, b) for s, b in self.current.items()]
        self.current.clear()
        return done

    def reset_day(self) -> None:
        self.current.clear()
        self.last_volume.clear()

    def _finish(self, symbol: str, b: _Building) -> MinuteBar:
        self.last_volume[symbol] = b.volume
        return MinuteBar(symbol, b.minute, b.open, b.high, b.low, b.close, max(b.volume - b.start_volume, 0),
                         b.volume, b.prev_close)
