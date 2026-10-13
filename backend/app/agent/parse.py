"""解析 AI 输出：提取 JSON、宽松校验、归一化为下单意图或目标权重。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from app.market.symbols import normalize
from app.trading.types import OrderIntent

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_SIDE_ALIASES = {
    "buy": "buy", "买": "buy", "买入": "buy", "sell": "sell", "卖": "sell", "卖出": "sell",
    "short": "short", "sell_short": "short", "卖空": "short", "做空": "short", "融券卖出": "short", "开空": "short",
    "cover": "cover", "buy_to_cover": "cover", "平空": "cover", "买券还券": "cover", "回补": "cover",
}
_TYPE_ALIASES = {"market": "market", "市价": "market", "limit": "limit", "限价": "limit", "stop": "stop",
                 "stop_loss": "stop", "止损": "stop", "take_profit": "take_profit", "止盈": "take_profit"}


class DecisionParseError(ValueError):
    pass


@dataclass
class ParsedDecision:
    analysis: str = ""
    intents: list[OrderIntent] = field(default_factory=list)      # 订单模式
    targets: dict[str, Decimal] | None = None                     # 目标仓位模式
    reasons: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    cancels: list[int] = field(default_factory=list)                                   # 要撤销的订单号
    risk_levels: dict[str, tuple[Decimal | None, Decimal | None]] = field(default_factory=dict)  # 标的 → (止损, 止盈)
    raw: dict = field(default_factory=dict)


def extract_json(text: str) -> dict:
    """从模型输出中提取第一个完整的 JSON 对象（容忍代码块、<think> 标签和前后说明文字）。"""
    if not text or not text.strip():
        raise DecisionParseError("输出为空")
    cleaned = _THINK_RE.sub("", text)
    start = cleaned.find("{")
    if start < 0:
        raise DecisionParseError("输出中没有 JSON 对象")
    depth, in_str, escape = 0, False, False
    for i in range(start, len(cleaned)):
        ch = cleaned[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(cleaned[start:i + 1])
                except json.JSONDecodeError as e:
                    raise DecisionParseError(f"JSON 格式错误：{e}") from e
                if not isinstance(obj, dict):
                    raise DecisionParseError("JSON 顶层必须是对象")
                return obj
    raise DecisionParseError("JSON 不完整（括号未闭合，可能输出被截断）")


def _symbol(value, notes: list[str], market: str) -> str | None:
    try:
        return normalize(str(value), market)
    except (ValueError, IndexError):
        notes.append(f"无法识别的代码：{value}")
        return None


def _decimal(value) -> Decimal:
    try:
        return Decimal(str(value).strip().rstrip("%"))
    except (InvalidOperation, AttributeError) as e:
        raise DecisionParseError(f"无效的数值：{value}") from e


def _price(value, name: str) -> Decimal | None:
    if value is None or value == "":
        return None
    v = _decimal(value)
    if v <= 0:
        raise DecisionParseError(f"{name} 必须为正数：{value}")
    return v


def parse_decision(text: str, output_mode: str, market: str = "CN") -> ParsedDecision:
    obj = extract_json(text)
    out = ParsedDecision(analysis=str(obj.get("analysis") or "")[:2000], raw=obj)
    for oid in obj.get("cancel_order_ids") or []:
        try:
            out.cancels.append(int(oid))
        except (TypeError, ValueError):
            out.notes.append(f"无效的撤单编号：{oid}")
    if output_mode == "targets":
        items = obj.get("targets")
        if items is None:
            raise DecisionParseError("缺少 targets 字段")
        if not isinstance(items, list):
            raise DecisionParseError("targets 必须是数组")
        targets: dict[str, Decimal] = {}
        for item in items:
            if not isinstance(item, dict) or "symbol" not in item or "weight" not in item:
                raise DecisionParseError("targets 中每一项都需要 symbol 和 weight")
            sym = _symbol(item["symbol"], out.notes, market)
            if sym is None:
                continue
            targets[sym] = targets.get(sym, Decimal("0")) + _decimal(item["weight"])
            if item.get("reason"):
                out.reasons[sym] = str(item["reason"])[:300]
            stop, tp = _price(item.get("stop_loss"), "stop_loss"), _price(item.get("take_profit"), "take_profit")
            if stop or tp:
                out.risk_levels[sym] = (stop, tp)
        # 兼容按百分数填写（如 15 表示 15%；负数为空头）
        if targets and max(abs(w) for w in targets.values()) > 1 and sum(abs(w) for w in targets.values()) <= 300:
            targets = {s: w / 100 for s, w in targets.items()}
            out.notes.append("权重按百分数填写，已自动换算为小数")
        out.targets = targets
        return out

    items = obj.get("orders")
    if items is None:
        raise DecisionParseError("缺少 orders 字段")
    if not isinstance(items, list):
        raise DecisionParseError("orders 必须是数组")
    for item in items:
        if not isinstance(item, dict):
            raise DecisionParseError("orders 中每一项都必须是对象")
        side = _SIDE_ALIASES.get(str(item.get("side", "")).strip().lower())
        if side is None:
            raise DecisionParseError(f"无效的 side：{item.get('side')}（buy / sell / short / cover）")
        sym = _symbol(item.get("symbol"), out.notes, market)
        if sym is None:
            continue
        qty = _decimal(item.get("qty"))
        if qty != qty.to_integral_value():
            raise DecisionParseError(f"qty 必须是整数：{item.get('qty')}")
        conf = item.get("confidence")
        otype = _TYPE_ALIASES.get(str(item.get("order_type") or "market").strip().lower())
        if otype is None:
            raise DecisionParseError(
                f"无效的 order_type：{item.get('order_type')}（market / limit / stop / take_profit）")
        if otype in ("stop", "take_profit") and side not in ("sell", "cover"):
            raise DecisionParseError("止损单和止盈单只能用于平仓（多头用 sell，空头用 cover）")
        tif = str(item.get("time_in_force") or item.get("tif") or ("gtc" if otype in ("stop", "take_profit")
                                                                  else "day")).lower()
        if tif not in ("day", "gtc"):
            raise DecisionParseError(f"无效的 time_in_force：{tif}（day / gtc）")
        intent = OrderIntent(
            symbol=sym, side=side, qty=int(qty), order_type=otype, reason=str(item.get("reason") or "")[:300],
            confidence=float(conf) if isinstance(conf, int | float) else None,
            limit_price=_price(item.get("limit_price"), "limit_price"),
            stop_price=_price(item.get("stop_price") or (item.get("trigger_price")), "stop_price"), tif=tif,
        )
        if otype == "limit" and intent.limit_price is None:
            raise DecisionParseError("限价单需要 limit_price")
        if otype in ("stop", "take_profit") and intent.stop_price is None:
            raise DecisionParseError(f"{otype} 订单需要 stop_price（触发价）")
        if side in ("buy", "short"):
            intent.attach_stop = _price(item.get("stop_loss"), "stop_loss")
            intent.attach_take_profit = _price(item.get("take_profit"), "take_profit")
        out.intents.append(intent)
    return out
