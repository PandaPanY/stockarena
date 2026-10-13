"""历史回放的代码匿名化（PRD 3.5）：在 prompt 中隐去证券代码、名称和绝对日期，减少模型凭记忆「认出」行情的可能。

- 证券：股票 S01、S02…，ETF E01…（按代码排序，同一赛道固定）；名称替换为「主板股票 / 科创板股票 / ETF」等，
  保留交易规则需要的板块信息
- 日期：换成相对决策日的交易日偏移（T、T-3、T+1）；不在交易日历中的日期换成「(日期)」
- AI 输出中的别名在解析前换回真实代码；工具参数同样换回，工具结果同样匿名化
局限：价格水平、指数名称、成交额等仍可能暴露身份，匿名化只能降低而不能消除前视偏差。
"""

from __future__ import annotations

import re
from datetime import date

from app.market.types import Instrument

_DATE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
_ALIAS = re.compile(r"\b([SE]\d{2,3})\b", re.IGNORECASE)
_BOARD_LABELS = {"star": "科创板股票", "gem": "创业板股票", "main": "主板股票"}


def _label(inst: Instrument) -> str:
    if inst.type == "etf":
        return "ETF"
    if inst.type == "lof":
        return "LOF"
    if inst.market != "CN":
        return "股票"
    return ("ST " if inst.is_st else "") + _BOARD_LABELS.get(inst.board, "股票")


class Anonymizer:
    def __init__(self, instruments: dict[str, Instrument], trading_days: list[date], anchor: date):
        self.alias: dict[str, str] = {}
        counters = {"S": 0, "E": 0}
        width = 3 if len(instruments) >= 100 else 2
        for sym in sorted(instruments):
            prefix = "E" if instruments[sym].type in ("etf", "lof") else "S"
            counters[prefix] += 1
            self.alias[sym] = f"{prefix}{counters[prefix]:0{width}d}"
        self.real = {v: k for k, v in self.alias.items()}
        # 替换顺序：完整代码 → 纯数字代码 → 名称（长的优先，避免部分匹配）
        pairs: list[tuple[str, str]] = []
        for sym, a in self.alias.items():
            pairs.append((sym, a))
            pairs.append((sym.split(".")[0], a))
        names = {inst.name: _label(inst) for inst in instruments.values() if inst.name}
        pairs += sorted(names.items(), key=lambda kv: -len(kv[0]))
        self._pattern = re.compile("|".join(
            (rf"(?<![0-9A-Za-z]){re.escape(k)}(?![0-9A-Za-z])" if k[:1].isalnum() and k.isascii() else re.escape(k))
            for k, _ in pairs))
        self._map = dict(pairs)
        self.days = sorted(trading_days)
        self.index = {d: i for i, d in enumerate(self.days)}
        self.anchor = anchor

    def _date(self, m: re.Match) -> str:
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return "(日期)"
        if d not in self.index or self.anchor not in self.index:
            return "(日期)"
        k = self.index[d] - self.index[self.anchor]
        return "T" if k == 0 else f"T{k:+d}"

    def text(self, s: str | None) -> str:
        if not s:
            return s or ""
        s = self._pattern.sub(lambda m: self._map[m.group(0)], s)
        return _DATE.sub(self._date, s)

    def restore(self, s: str | None) -> str:
        """把 AI 输出中的别名换回真实代码（解析前调用）。"""
        if not s:
            return s or ""
        return _ALIAS.sub(lambda m: self.real.get(m.group(1).upper(), m.group(1)), s)

    def restore_args(self, value):
        if isinstance(value, str):
            return self.restore(value)
        if isinstance(value, list):
            return [self.restore_args(v) for v in value]
        if isinstance(value, dict):
            return {k: self.restore_args(v) for k, v in value.items()}
        return value
