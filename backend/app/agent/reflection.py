"""复盘：模板加载与输出解析（PRD 4.4.4）。"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from app.agent.parse import DecisionParseError, extract_json

MAX_STRATEGY_CHARS = 2000        # 策略笔记长度上限（字符）
MAX_CHANGES = 3                  # 每次复盘最多修改的规则条数（写第一版时不限）
_MAX_REVIEW_CHARS = 1500
_MAX_CHANGES_CHARS = 800
_NO_CHANGE = {"", "无", "无修改", "没有修改", "不修改", "none", "no change"}


@dataclass(frozen=True)
class ReflectionTemplate:
    name: str
    version: int
    system: str
    user: str


@dataclass
class ParsedReflection:
    review: str
    changes: str
    strategy: str
    raw: dict


@lru_cache
def load_reflection_template(config_dir: Path) -> ReflectionTemplate:
    raw = yaml.safe_load((config_dir / "reflection" / "default.yaml").read_text(encoding="utf-8"))
    return ReflectionTemplate(name=raw["name"], version=int(raw["version"]), system=raw["system"], user=raw["user"])


def _text(obj: dict, *keys: str) -> str:
    for k in keys:
        v = obj.get(k)
        if isinstance(v, list):      # 有的模型会把要点写成数组
            v = "\n".join(f"- {x}" if not str(x).lstrip().startswith(("-", "•")) else str(x) for x in v)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _change_line(item) -> str:
    """把一条结构化修改说明格式化为一行文字。"""
    if not isinstance(item, dict):
        return str(item).strip()
    head = f"{item.get('type') or '修改'}：{item.get('rule') or ''}".strip()
    extra = "；".join(f"{label}{item[k]}" for k, label in (("reason", "原因："), ("turnover_impact", "换手影响："))
                     if item.get(k))
    return f"{head}（{extra}）" if extra else head


def parse_reflection(text: str, max_changes: int | None = None) -> ParsedReflection:
    """max_changes：最多允许的修改条数（已有策略时为 MAX_CHANGES，写第一版时不限）。"""
    obj = extract_json(text)
    strategy = _text(obj, "strategy", "strategy_notes", "notes", "策略笔记")
    if not strategy:
        raise DecisionParseError("缺少 strategy（完整的新版策略笔记）")
    if len(strategy) > MAX_STRATEGY_CHARS:
        raise DecisionParseError(f"strategy 超过 {MAX_STRATEGY_CHARS} 字（当前 {len(strategy)} 字），请精简")
    review = _text(obj, "review", "analysis", "复盘")[:_MAX_REVIEW_CHARS]
    raw_changes = next((obj[k] for k in ("changes", "change", "修改") if k in obj), "")
    if isinstance(raw_changes, list):
        items = [x for x in raw_changes if x]
        if max_changes is not None and len(items) > max_changes:
            raise DecisionParseError(f"本次修改了 {len(items)} 条规则，每次最多修改 {max_changes} 条，"
                                     "请只保留证据最充分的修改，其余规则保持原样")
        changes = "\n".join(f"{i}. {_change_line(x)}" for i, x in enumerate(items, 1)) if items else "无修改"
    else:
        changes = str(raw_changes or "").strip()
    return ParsedReflection(review=review, changes=changes[:_MAX_CHANGES_CHARS], strategy=strategy, raw=obj)


def is_no_change(changes: str) -> bool:
    return changes.strip().strip("。.").lower() in _NO_CHANGE
