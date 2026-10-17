"""AI 优化时间线：把策略从初始版本起的每一轮 AI 优化、每个候选改了什么、为什么、检验结果和是否采纳，
按时间串成一条可读的记录（策略详情「优化时间线」与体检报告共用）。

只放开发期的结果（AI 研究和升级检验都只看开发期），不放留出期，避免读者按留出期回头挑版本。
"""

from __future__ import annotations

from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.quant import QuantStrategyRow, QuantVersionRow, ResearchRunRow
from app.quant.spec import describe

OUTCOME = {"champion": "升级为冠军", "retired": "曾是冠军（已被替换）", "rejected": "未采纳", "candidate": "检验中",
           "duplicate": "与已试过的版本相同，跳过"}


def _rules(spec: dict | None) -> list[str]:
    if not spec:
        return []
    try:
        return describe(spec)
    except ValueError as e:
        return [f"规格无法解析：{e}"]


def rule_diff(old: dict | None, new: dict | None) -> dict:
    """逐条比较两版规则的中文描述：{"removed": 旧版有、新版没有的, "added": 新版多出的}。"""
    a, b = Counter(_rules(old)), Counter(_rules(new))
    return {"removed": list((a - b).elements()), "added": list((b - a).elements())}


def _metrics(v: QuantVersionRow) -> dict:
    dev = v.dev or {}
    vs = dev.get("vs_champion") or {}
    return {"sharpe": dev.get("sharpe"), "annualized_return": dev.get("annualized_return"),
            "max_drawdown": dev.get("max_drawdown"), "turnover": dev.get("turnover"),
            "p": vs.get("p"), "n_tests": vs.get("n_tests"), "excess_annual": vs.get("excess_annual"),
            "year_wins": f"{vs['year_wins']}/{vs['years']}" if vs.get("years") else None}


def _candidate(v: QuantVersionRow, by_id: dict[int, QuantVersionRow]) -> dict:
    parent = by_id.get(v.parent_id) if v.parent_id else None
    return {"number": v.number, "title": v.title, "source": v.source, "status": v.status,
            "outcome": OUTCOME.get(v.status, v.status), "rationale": v.rationale, "decision": v.decision,
            "parent_number": parent.number if parent else None,
            "diff": rule_diff(parent.spec if parent else None, v.spec),
            "metrics": _metrics(v), "parent_sharpe": (parent.dev or {}).get("sharpe") if parent else None,
            "created_at": v.created_at.isoformat()}


def build(s: Session, st: QuantStrategyRow) -> dict:
    versions = list(s.scalars(select(QuantVersionRow).where(QuantVersionRow.strategy_id == st.id)
                              .order_by(QuantVersionRow.number)))
    by_id = {v.id: v for v in versions}
    runs = {r.id: r for r in s.scalars(select(ResearchRunRow).where(ResearchRunRow.strategy_id == st.id))}
    initial = versions[0] if versions else None
    origin = st.origin or {}
    start = {"number": initial.number if initial else None, "title": initial.title if initial else "",
             "rules": _rules(initial.spec if initial else None), "metrics": _metrics(initial) if initial else {},
             "created_at": initial.created_at.isoformat() if initial else None,
             "forked_from": ({"strategy_id": origin.get("strategy_id"), "strategy_name": origin.get("strategy_name"),
                              "version_number": origin.get("version_number"), "prior_tests": origin.get("prior_tests")}
                             if origin.get("strategy_id") else None),
             "trials_before_create": origin.get("prior_tests") if origin.get("kind") == "new" else None}

    events: list[dict] = []
    seen: set[int] = set()
    for run in sorted(runs.values(), key=lambda r: r.id):
        for entry in run.log or []:
            cands = []
            for c in entry.get("candidates") or []:
                v = by_id.get(c.get("version_id")) if c.get("version_id") else None
                if v is not None:
                    seen.add(v.id)
                    cands.append(_candidate(v, by_id))
                else:
                    cands.append({"title": c.get("title"), "status": c.get("status"), "decision": c.get("reason"),
                                  "outcome": OUTCOME.get(c.get("status"), c.get("status"))})
            events.append({"type": "round", "run_id": run.id, "trigger": run.trigger, "round": entry.get("round"),
                           "at": run.created_at.isoformat(), "status": entry.get("status"),
                           "analysis": entry.get("analysis") or "", "lessons": entry.get("lessons") or [],
                           "error": entry.get("error") or "", "candidates": cands,
                           "promoted": entry.get("promoted")})
    for v in versions[1:]:
        if v.id not in seen:                                  # 手动修改的版本，或研究记录已不完整的旧版本
            events.append({"type": "manual" if v.source == "manual" else "version", "at": v.created_at.isoformat(),
                           "candidates": [_candidate(v, by_id)], "promoted": v.number if v.promoted_at else None})
    events.sort(key=lambda e: (e["at"], e.get("run_id") or 0, e.get("round") or 0))

    champ = by_id.get(st.champion_id) if st.champion_id else None
    ai = [v for v in versions if v.source == "ai"]
    return {"start": start, "events": events,
            "summary": {"versions": len(versions), "ai_candidates": len(ai),
                        "ai_promoted": sum(1 for v in ai if v.promoted_at),
                        "manual": sum(1 for v in versions if v.source == "manual"),
                        "rounds": sum(1 for e in events if e["type"] == "round"),
                        "champion": champ.number if champ else None}}
