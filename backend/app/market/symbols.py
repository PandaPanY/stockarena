"""A股证券代码工具：统一代码（600519.SH）与各数据源代码之间的转换、板块识别。"""

from __future__ import annotations

from decimal import Decimal

from app.market.types import Board, InstrumentType


def cn_exchange(code: str) -> str:
    """根据 6 位代码判断交易所：SH / SZ / BJ。"""
    if code.startswith(("60", "68", "51", "52", "56", "58", "50", "11", "13")):
        return "SH"
    if code.startswith(("00", "30", "15", "16", "18", "12")):
        return "SZ"
    if code.startswith(("43", "83", "87", "88", "92")):
        return "BJ"
    raise ValueError(f"无法识别交易所：{code}")


def normalize_cn(symbol: str) -> str:
    """接受 600519 / sh600519 / 600519.SH，统一为 600519.SH。"""
    s = symbol.strip().upper()
    if "." in s:
        code, ex = s.split(".", 1)
        return f"{code}.{ex}"
    if s[:2] in ("SH", "SZ", "BJ") and s[2:].isdigit():
        return f"{s[2:]}.{s[:2]}"
    if s.isdigit() and len(s) == 6:
        return f"{s}.{cn_exchange(s)}"
    raise ValueError(f"无法识别的A股代码：{symbol}")


def to_sina(symbol: str) -> str:
    """600519.SH → sh600519"""
    code, ex = normalize_cn(symbol).split(".")
    return f"{ex.lower()}{code}"


def cn_fund_type(code: str) -> InstrumentType | None:
    """场内基金识别（按代码段）。返回 None 表示不是基金。"""
    if code.startswith(("51", "52", "56", "58", "159")):
        return "etf"
    if code.startswith(("50", "16")):
        return "lof"
    return None


def cn_board(code: str) -> Board:
    if cn_fund_type(code):
        return "fund"
    if code.startswith(("688", "689")):
        return "star"
    if code.startswith("30"):       # 创业板：300、301，以及 302（如 302132 中航成飞）
        return "gem"
    if code.startswith(("43", "83", "87", "88", "92")):
        return "bse"
    return "main"


def cn_stock_price_limit(board: Board, is_st: bool) -> Decimal:
    """A股股票涨跌幅限制。科创板、创业板的 ST 股同样为 20%。"""
    if board in ("star", "gem"):
        return Decimal("0.20")
    if board == "bse":
        return Decimal("0.30")
    return Decimal("0.05") if is_st else Decimal("0.10")


def is_st_name(name: str) -> bool:
    return "ST" in name.upper().replace(" ", "")


# ---------------- 港股 / 美股 ----------------

def normalize_hk(symbol: str) -> str:
    """接受 700 / 0700 / 00700 / hk00700 / 0700.HK，统一为 00700.HK。"""
    s = symbol.strip().upper().removesuffix(".HK").removeprefix("HK")
    if not s.isdigit() or len(s) > 5:
        raise ValueError(f"无法识别的港股代码：{symbol}")
    return f"{s.zfill(5)}.HK"


def normalize_us(symbol: str) -> str:
    """接受 aapl / AAPL / AAPL.US / gb_aapl，统一为 AAPL.US。"""
    s = symbol.strip().upper().removesuffix(".US").removeprefix("GB_")
    if not s or not all(c.isalnum() or c in "-." for c in s):
        raise ValueError(f"无法识别的美股代码：{symbol}")
    return f"{s}.US"


def normalize(symbol: str, market: str) -> str:
    if market == "CN":
        return normalize_cn(symbol)
    if market == "HK":
        return normalize_hk(symbol)
    if market == "US":
        return normalize_us(symbol)
    raise ValueError(f"未知市场：{market}")
