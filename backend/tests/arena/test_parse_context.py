from __future__ import annotations

from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from app.agent.context import build_market_table, indicators, rsi
from app.agent.parse import DecisionParseError, extract_json, parse_decision
from app.market.types import DailyBar, Instrument


@pytest.mark.parametrize(
    "text",
    [
        '{"a": 1}',
        '```json\n{"a": 1}\n```',
        '好的，以下是决策：{"a": 1} 希望有帮助',
        '<think>先想想 {"x": 0}</think>{"a": 1}',
        '{"a": 1, "s": "含有}括号{的字符串"}',
    ],
)
def test_extract_json_tolerant(text):
    assert extract_json(text)["a"] == 1


@pytest.mark.parametrize("text", ["", "没有 JSON", '{"a": 1', '{"a": }'])
def test_extract_json_errors(text):
    with pytest.raises(DecisionParseError):
        extract_json(text)


def test_parse_targets_percent_and_symbol_normalization():
    d = parse_decision('{"analysis": "x", "targets": [{"symbol": "600519", "weight": 15},'
                       '{"symbol": "sh510300", "weight": "30%"}]}', "targets")
    assert d.targets == {"600519.SH": Decimal("0.15"), "510300.SH": Decimal("0.3")}
    assert any("百分数" in n for n in d.notes)


def test_parse_targets_unknown_symbol_noted():
    d = parse_decision('{"targets": [{"symbol": "ABC", "weight": 0.1}]}', "targets")
    assert d.targets == {} and d.notes


def test_parse_orders_with_chinese_side():
    d = parse_decision('{"orders": [{"symbol": "600519.SH", "side": "买入", "qty": "200", "confidence": 0.8}]}',
                       "orders")
    assert d.intents[0].side == "buy" and d.intents[0].qty == 200 and d.intents[0].confidence == 0.8


@pytest.mark.parametrize(
    ("text", "mode"),
    [
        ('{"orders": [{"symbol": "600519", "side": "hold", "qty": 100}]}', "orders"),
        ('{"orders": [{"symbol": "600519", "side": "buy", "qty": 1.5}]}', "orders"),
        ('{"analysis": "x"}', "targets"),
        ('{"targets": {"600519": 0.1}}', "targets"),
    ],
)
def test_parse_errors(text, mode):
    with pytest.raises(DecisionParseError):
        parse_decision(text, mode)


def test_rsi_and_indicators():
    up = pd.Series([float(i) for i in range(1, 30)])
    assert rsi(up) == 100.0
    ind = indicators(pd.DataFrame({"close": [10.0] * 20 + [11.0]}))
    assert ind["chg5"] == pytest.approx(0.1)
    assert ind["ma5"] == pytest.approx(10.2)


def test_market_table_flags_limit_up_and_suspension():
    d = date(2026, 10, 8)
    a = Instrument("600519.SH", "CN", "贵州茅台", "stock", "main", price_limit_pct=Decimal("0.10"))
    b = Instrument("601318.SH", "CN", "中国平安", "stock", "main", price_limit_pct=Decimal("0.10"))
    bars = {
        a.symbol: DailyBar(a.symbol, d, Decimal("11"), Decimal("11"), Decimal("10.5"), Decimal("11"), 100,
                           Decimal("1e9"), Decimal("10")),
        b.symbol: DailyBar(b.symbol, date(2026, 9, 30), Decimal("50"), Decimal("50"), Decimal("50"), Decimal("50"),
                           100, Decimal("1e9"), Decimal("50")),
    }
    lines = build_market_table({a.symbol: a, b.symbol: b}, bars, {}, d).splitlines()
    assert lines[1].endswith("涨停") and "+10.00%" in lines[1]
    assert lines[2].endswith("停牌")
