"""股票池：读取 config/universes/*.yaml 并解析为证券列表。"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Protocol

import yaml

from app.core.money import to_decimal
from app.market.symbols import (
    cn_board,
    cn_fund_type,
    cn_stock_price_limit,
    is_st_name,
    normalize_cn,
    normalize_hk,
    normalize_us,
)
from app.market.types import Instrument, UniverseSpec

logger = logging.getLogger(__name__)

_NAMES_TTL = timedelta(days=1)
_CONS_TTL = timedelta(days=7)


class UniverseSource(Protocol):
    def stock_names(self) -> dict[str, str]: ...
    def index_constituents(self, index_code: str) -> list[tuple[str, str]]: ...


def load_universe_specs(config_dir: Path) -> dict[str, UniverseSpec]:
    specs: dict[str, UniverseSpec] = {}
    for path in sorted((config_dir / "universes").glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        spec = UniverseSpec(
            name=raw["name"], title=raw.get("title", raw["name"]), market=raw["market"],
            members=raw.get("members") or [], index=raw.get("index"), include=raw.get("include") or [],
            benchmark=raw.get("benchmark"), description=raw.get("description", ""),
            point_in_time=str(raw["point_in_time"]) if raw.get("point_in_time") else None,
            live_universe=raw.get("live_universe"), history=bool(raw.get("history", False)),
        )
        specs[spec.name] = spec
    return specs


class _JsonCache:
    """带有效期的 JSON 文件缓存，减少对数据源的重复请求。"""

    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir

    def get(self, key: str, ttl: timedelta, today: date):
        path = self.cache_dir / f"{key}.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if date.fromisoformat(data["fetched"]) + ttl < today:
            return None
        return data["value"]

    def get_stale(self, key: str):
        path = self.cache_dir / f"{key}.json"
        return json.loads(path.read_text(encoding="utf-8"))["value"] if path.exists() else None

    def put(self, key: str, value, today: date) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        (self.cache_dir / f"{key}.json").write_text(
            json.dumps({"fetched": today.isoformat(), "value": value}, ensure_ascii=False), encoding="utf-8")


class HKSecuritiesSource(Protocol):
    def securities(self) -> dict[str, dict]: ...


class UniverseResolver:
    def __init__(self, specs: dict[str, UniverseSpec], sources: UniverseSource | dict[str, object], cache_dir: Path):
        """sources：{市场: 数据源}；只传一个对象时视为 A股数据源。"""
        self.specs = specs
        self.sources = sources if isinstance(sources, dict) else {"CN": sources}
        self.source = self.sources.get("CN")
        self.cache = _JsonCache(cache_dir)

    def _cached(self, key: str, ttl: timedelta, today: date, fetch):
        value = self.cache.get(key, ttl, today)
        if value is not None:
            return value
        try:
            value = fetch()
        except Exception:
            stale = self.cache.get_stale(key)
            if stale is None:
                raise
            logger.warning("刷新 %s 失败，使用过期缓存", key)
            return stale
        self.cache.put(key, value, today)
        return value

    def stock_names(self, today: date) -> dict[str, str]:
        return self._cached("cn_stock_names", _NAMES_TTL, today, self.source.stock_names)

    def resolve(self, name: str, today: date | None = None) -> list[Instrument]:
        """解析股票池为证券列表（去重，保持顺序）。"""
        today = today or date.today()
        spec = self.specs[name]
        result: dict[str, Instrument] = {}
        for inc in spec.include:
            for inst in self.resolve(inc, today):
                result.setdefault(inst.symbol, inst)
        if spec.market == "HK":
            securities = self._cached("hkex_securities", _CONS_TTL, today, self.sources["HK"].securities)
            for member in spec.members:
                inst = self._hk_instrument(member, securities)
                result.setdefault(inst.symbol, inst)
            return list(result.values())
        if spec.market == "US":
            for member in spec.members:
                inst = self._us_instrument(member)
                result.setdefault(inst.symbol, inst)
            return list(result.values())
        if spec.index:
            cons = self._cached(f"index_cons_{spec.index}", _CONS_TTL, today,
                                lambda: self.source.index_constituents(spec.index))
            names = self.stock_names(today)
            for code, cons_name in cons:
                # 交易所当日简称可能带 XD/XR/DR 等除权标记，显示用成分表的正式简称；ST 状态以交易所简称为准
                exchange_name = names.get(code, cons_name)
                inst = self._cn_instrument({"symbol": code, "name": cons_name or exchange_name,
                                            "st": is_st_name(exchange_name)})
                result.setdefault(inst.symbol, inst)
        for member in spec.members:
            inst = self._cn_instrument(member)
            result.setdefault(inst.symbol, inst)
        return list(result.values())

    @staticmethod
    def _hk_instrument(member: dict, securities: dict[str, dict]) -> Instrument:
        symbol = normalize_hk(str(member["symbol"]))
        info = securities.get(symbol.split(".")[0], {})
        lot = info.get("lot") or member.get("lot")
        if not lot:
            raise ValueError(f"无法获取 {symbol} 的每手股数，请在股票池配置中填写 lot")
        is_fund = member.get("type") == "etf" or info.get("category", "").startswith("Exchange Traded")
        return Instrument(
            symbol=symbol, market="HK", name=str(member.get("name") or info.get("name_en") or symbol),
            type="etf" if is_fund else "stock", board="fund" if is_fund else "main", lot_size=int(lot),
            tick=Decimal("0.001"), t0=True, price_limit_pct=None,
            stamp_duty=bool(info.get("stamp_duty", not is_fund)),
        )

    @staticmethod
    def _us_instrument(member: dict) -> Instrument:
        symbol = normalize_us(str(member["symbol"]))
        is_fund = member.get("type") == "etf"
        return Instrument(
            symbol=symbol, market="US", name=str(member.get("name") or symbol.split(".")[0]),
            type="etf" if is_fund else "stock", board="fund" if is_fund else "main", lot_size=1,
            tick=Decimal("0.01"), t0=True, price_limit_pct=None, stamp_duty=False,
        )

    @staticmethod
    def _cn_instrument(member: dict) -> Instrument:
        symbol = normalize_cn(str(member["symbol"]))
        code = symbol.split(".")[0]
        name = str(member.get("name") or code)
        fund_type = member.get("type") or cn_fund_type(code)
        if fund_type in ("etf", "lof"):
            limit = member.get("price_limit_pct")
            return Instrument(
                symbol=symbol, market="CN", name=name, type=fund_type, board="fund",
                lot_size=100, tick=Decimal("0.001"), t0=bool(member.get("t0", False)),
                price_limit_pct=to_decimal(limit) if limit is not None else Decimal("0.10"),
            )
        board = cn_board(code)
        st = bool(member["st"]) if "st" in member else is_st_name(name)
        return Instrument(
            symbol=symbol, market="CN", name=name, type="stock", board=board,
            lot_size=100, tick=Decimal("0.01"), is_st=st,
            price_limit_pct=cn_stock_price_limit(board, st),
        )


def index_instrument(symbol: str, name: str) -> Instrument:
    """基准指数（仅用于取行情，不可交易）。市场由代码后缀判断：.HK 港股，.US 美股，其余 A股。"""
    market = "HK" if symbol.endswith(".HK") else "US" if symbol.endswith(".US") else "CN"
    return Instrument(symbol=symbol, market=market, name=name, type="index", board="index",
                      lot_size=1, tick=Decimal("0.001"))


BENCHMARK_NAMES = {
    "000016.SH": "上证50",
    "000300.SH": "沪深300",
    "000905.SH": "中证500",
    "000906.SH": "中证800",
    "399006.SZ": "创业板指",
    "HSI.HK": "恒生指数",
    "NDX.US": "纳斯达克100",
    "INX.US": "标普500",
}
