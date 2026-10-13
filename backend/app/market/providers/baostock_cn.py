"""Baostock（证券宝，免费、免注册）：历史指数成分（沪深300、中证500）与已退市股票的日线、复权因子、上市 / 退市日期。

2026-10-08 在服务器上实测：登录约 40～50 秒（服务端慢），之后每次查询约 1 秒；成分快照每月更新一次，带生效日期，
与中证指数公司的定期调整日基本一致（误差在一个月以内）。见 docs/research/2026-10-08-pit-universe.md。

只在准备全时点股票池（每周最多一次）时使用；同一时间只允许一个登录会话（baostock 是进程级的全局连接）。
"""

from __future__ import annotations

import contextlib
import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from decimal import Decimal, InvalidOperation

from app.market.types import DailyBar

logger = logging.getLogger(__name__)

_lock = threading.Lock()
INDEX_QUERIES = {"hs300": "query_hs300_stocks", "zz500": "query_zz500_stocks"}
TIMEOUT = 120                 # 秒：单次收发
RETRIES = 2                   # 通信失败后重新登录再试的次数
_END = b"<![CDATA[]]>\n"
_failed = 0                   # 通信失败次数（用于发现翻页时被吞掉的失败）


class BaostockError(Exception):
    pass


def _send_msg(msg: str) -> str | None:
    """替换 baostock 的 send_msg。

    原版没有超时，服务器断开连接后 recv 一直返回空、死循环占满 CPU（2026-10-08 在服务器上卡了两个多小时）；
    翻页时的通信失败也会被当成「没有更多数据」，结果被悄悄截断。这里加超时、断开即失败，并记下失败次数。
    报文解析参照 baostock.util.socketutil.send_msg 改写（baostock 采用 BSD 许可证）。
    """
    import zlib

    import baostock.common.contants as cons
    import baostock.common.context as context

    global _failed
    sock = getattr(context, "default_socket", None)
    if sock is None:
        return None
    try:
        sock.settimeout(TIMEOUT)
        sock.sendall(bytes(msg + "\n", encoding="utf-8"))
        receive = b""
        while not receive.endswith(_END):
            chunk = sock.recv(8192)
            if not chunk:
                raise ConnectionError("服务器已断开连接")
            receive += chunk
        head = bytes.decode(receive[:cons.MESSAGE_HEADER_LENGTH])
        parts = head.split(cons.MESSAGE_SPLIT)
        if parts[1] in cons.COMPRESSED_MESSAGE_TYPE_TUPLE:
            body = receive[cons.MESSAGE_HEADER_LENGTH:cons.MESSAGE_HEADER_LENGTH + int(parts[2])]
            return head + bytes.decode(zlib.decompress(body))
        return bytes.decode(receive)
    except (OSError, ValueError, IndexError, zlib.error) as e:
        _failed += 1
        logger.warning("Baostock 通信失败：%s", e)
        return None


def _install() -> None:
    import baostock.util.socketutil as su

    su.send_msg = _send_msg       # 各查询模块都通过 socketutil.send_msg 调用，替换这一处即可


def _login(bs) -> None:
    lg = bs.login()
    if lg.error_code != "0":
        raise BaostockError(f"登录失败：{lg.error_msg}")


def _relogin(bs) -> None:
    import baostock.common.context as context

    with contextlib.suppress(Exception):
        context.default_socket.close()
    _login(bs)


@contextmanager
def session() -> Iterator[object]:
    import baostock as bs

    with _lock:
        _install()
        _login(bs)
        try:
            yield bs
        finally:
            with contextlib.suppress(Exception):
                bs.logout()


def to_symbol(code: str) -> str:
    """sh.600000 → 600000.SH"""
    ex, num = code.split(".")
    return f"{num}.{ex.upper()}"


def to_code(symbol: str) -> str:
    """600000.SH → sh.600000"""
    num, ex = symbol.split(".")
    return f"{ex.lower()}.{num}"


def _rows(rs) -> list[list[str]]:
    if rs.error_code != "0":
        raise BaostockError(rs.error_msg)
    out = []
    while rs.next():
        out.append(rs.get_row_data())
    return out


def _fetch(bs, query) -> list[list[str]]:
    """执行查询（query() 返回 baostock 结果集）。通信失败（含翻页时）重新登录后重试；数据本身的错误直接抛出。"""
    for attempt in range(RETRIES + 1):
        before = _failed
        try:
            rows = _rows(query())
        except BaostockError:
            if _failed == before or attempt == RETRIES:
                raise
        else:
            if _failed == before:
                return rows
            if attempt == RETRIES:
                raise BaostockError("通信失败，结果不完整")
        logger.warning("Baostock 重新登录后重试（第 %d 次）", attempt + 1)
        _relogin(bs)
    raise AssertionError("unreachable")


def snapshot(bs, index: str, d: date) -> tuple[date, dict[str, str]] | None:
    """d 当天有效的指数成分：(生效日期, {代码: 名称})；没有数据时返回 None。"""
    rows = _fetch(bs, lambda: getattr(bs, INDEX_QUERIES[index])(date=d.isoformat()))
    if not rows:
        return None
    eff = date.fromisoformat(rows[0][0])
    return eff, {to_symbol(r[1]): r[2] for r in rows}


def stock_basic(bs, symbol: str) -> dict | None:
    """{name, ipo, delisted}；delisted 为 None 表示仍在上市。"""
    rows = _fetch(bs, lambda: bs.query_stock_basic(code=to_code(symbol)))
    if not rows:
        return None
    r = rows[0]
    return {"name": r[1], "ipo": date.fromisoformat(r[2]) if r[2] else None,
            "delisted": date.fromisoformat(r[3]) if r[3] else None}


def _dec(x: str) -> Decimal | None:
    try:
        return Decimal(x) if x not in ("", None) else None
    except InvalidOperation:
        return None


def daily_bars(bs, symbol: str, start: date, end: date) -> list[DailyBar]:
    """不复权日线 + 后复权因子；停牌日（tradestatus=0 或成交量为 0）不返回，与其他数据源一致。"""
    code = to_code(symbol)
    rows = _fetch(bs, lambda: bs.query_history_k_data_plus(
        code, "date,open,high,low,close,preclose,volume,amount,tradestatus",
        start_date=start.isoformat(), end_date=end.isoformat(), frequency="d", adjustflag="3"))
    factors = [(date.fromisoformat(r[1]), Decimal(r[3])) for r in
               _fetch(bs, lambda: bs.query_adjust_factor(code=code, start_date="1990-01-01",
                                                         end_date=end.isoformat()))
               if r[3]]
    factors.sort()
    out: list[DailyBar] = []
    k, fac = 0, Decimal("1")
    for d_s, o, h, lo, c, pc, vol, amt, status in rows:
        d = date.fromisoformat(d_s)
        while k < len(factors) and factors[k][0] <= d:
            fac = factors[k][1]
            k += 1
        o_, h_, l_, c_ = _dec(o), _dec(h), _dec(lo), _dec(c)
        v = int(float(vol)) if vol else 0
        if status != "1" or v <= 0 or None in (o_, h_, l_, c_) or c_ <= 0:
            continue
        out.append(DailyBar(symbol=symbol, date=d, open=o_, high=h_, low=l_, close=c_, volume=v,
                            amount=_dec(amt) or Decimal("0"), prev_close=_dec(pc), adj_factor=fac))
    return out
