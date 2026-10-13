"""只读的元数据：prompt 模板、规则集、股票池。"""

from __future__ import annotations

import yaml
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import get_db
from app.market.markets import MARKETS
from app.market.universe import BENCHMARK_NAMES, load_universe_specs
from app.models import PromptTemplateRow, RuleSetRow
from app.quant.service import QUICK_UNIVERSE
from app.trading.params import RuleParams

router = APIRouter(prefix="/api/meta", tags=["meta"])


@router.get("/prompt-templates")
def prompt_templates(db: Session = Depends(get_db)) -> list[dict]:
    return [{"id": t.id, "name": t.name, "version": t.version, "title": t.title, "description": t.description}
            for t in db.scalars(select(PromptTemplateRow).order_by(PromptTemplateRow.name,
                                                                   PromptTemplateRow.version.desc()))]


@router.get("/prompt-templates/{template_id}")
def prompt_template(template_id: int, db: Session = Depends(get_db)) -> dict:
    t = db.get(PromptTemplateRow, template_id)
    if t is None:
        raise HTTPException(404, "模板不存在")
    return {"id": t.id, "name": t.name, "version": t.version, "title": t.title, "description": t.description,
            "system": t.system_tpl, "user": t.user_tpl}


@router.get("/rulesets")
def rulesets(db: Session = Depends(get_db)) -> list[dict]:
    descriptions = {name: f.description for name, f in RuleParams.model_fields.items()}
    return [{"id": r.id, "name": r.name, "title": r.title, "market": r.market, "description": r.description,
             "params": r.params, "param_sources": r.param_sources or {}, "param_descriptions": descriptions}
            for r in db.scalars(select(RuleSetRow).order_by(RuleSetRow.id))]


@router.get("/markets")
def markets() -> list[dict]:
    return [{"code": m.code, "name": m.name, "currency": m.currency, "timezone": m.tz,
             "default_capital": m.default_capital,
             "run_times": [f"{h:02d}:{mi:02d}" for h, mi in m.run_times]} for m in MARKETS.values()]


@router.get("/universes")
def universes() -> list[dict]:
    specs = load_universe_specs(get_settings().config_dir)
    return [{"name": s.name, "title": s.title, "market": s.market, "description": s.description,
             "benchmark": s.benchmark, "benchmark_name": BENCHMARK_NAMES.get(s.benchmark or "", s.benchmark),
             "index": s.index, "include": s.include, "member_count": len(s.members),
             "point_in_time": s.point_in_time, "history": s.history,
             # 快速体验股票池：有幸存者偏差，只在完整数据准备好之前用来试用
             "quick": s.name == QUICK_UNIVERSE}
            for s in specs.values()]


@router.get("/personas")
def personas() -> list[dict]:
    """策略人格库（config/personas.yaml），选手设置中可一键套用。"""
    path = get_settings().config_dir / "personas.yaml"
    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [{"name": p["name"], "title": p.get("title", p["name"]), "description": p.get("description", ""),
             "persona": str(p.get("persona", "")).strip()} for p in raw.get("personas", [])]
