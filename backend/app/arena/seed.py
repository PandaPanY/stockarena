"""启动时写入预置数据：规则集、prompt 模板、基线选手。已存在的不覆盖（模板按 名称+版本 判断）。"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.prompts import load_template_specs
from app.baselines import BASELINE_TYPES
from app.core.config import get_settings
from app.models import AgentRow, PromptTemplateRow, RuleSetRow
from app.trading.params import RuleParams

logger = logging.getLogger(__name__)


def resolve_rule_params(ruleset_params: dict, global_defaults: dict) -> tuple[dict, dict]:
    """三级继承（PRD US-3.3）：系统默认 → 全局配置 rule_defaults → 规则集。返回 (生效参数, 每个参数的来源)。"""
    unknown = set(global_defaults) - set(RuleParams.model_fields)
    if unknown:
        logger.warning("config.yaml 的 rule_defaults 中有未知参数，已忽略：%s", sorted(unknown))
    merged = {k: v for k, v in global_defaults.items() if k in RuleParams.model_fields}
    merged.update(ruleset_params)
    params = RuleParams(**merged).model_dump(mode="json")   # 校验并补全默认值
    sources = {k: "ruleset" if k in ruleset_params else ("global" if k in merged else "system") for k in params}
    return params, sources


def seed_rule_sets(session: Session, config_dir: Path) -> None:
    global_defaults = get_settings().rule_defaults
    for path in sorted((config_dir / "rulesets").glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        params, sources = resolve_rule_params(raw.get("params") or {}, global_defaults)
        row = session.scalars(select(RuleSetRow).where(RuleSetRow.name == raw["name"])).first()
        if row is None:
            row = RuleSetRow(name=raw["name"], is_preset=True)
            session.add(row)
        if row.is_preset:   # 预置规则集随配置文件更新；已启动的赛道使用冻结的副本，不受影响
            row.title, row.market, row.params = raw.get("title", raw["name"]), raw["market"], params
            row.param_sources = sources
            row.description = raw.get("description", "")


def seed_prompt_templates(session: Session, config_dir: Path) -> None:
    for spec in load_template_specs(config_dir):
        exists = session.scalars(select(PromptTemplateRow).where(
            PromptTemplateRow.name == spec.name, PromptTemplateRow.version == spec.version)).first()
        if exists is None:
            session.add(PromptTemplateRow(name=spec.name, version=spec.version, title=spec.title,
                                          description=spec.description, system_tpl=spec.system, user_tpl=spec.user))
            logger.info("新增 prompt 模板 %s v%d", spec.name, spec.version)


def seed_baseline_agents(session: Session) -> None:
    for kind, (title, desc) in BASELINE_TYPES.items():
        exists = session.scalars(select(AgentRow).where(AgentRow.kind == "baseline",
                                                        AgentRow.baseline_type == kind)).first()
        if exists is None:
            session.add(AgentRow(name=f"基线·{title}", kind="baseline", baseline_type=kind, description=desc,
                                 params={}, persona="", memory_rounds=0))
        else:
            exists.description = desc   # 说明随代码更新


def seed_defaults(session: Session, config_dir: Path) -> None:
    seed_rule_sets(session, config_dir)
    seed_prompt_templates(session, config_dir)
    seed_baseline_agents(session)
    session.commit()
