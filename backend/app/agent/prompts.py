"""prompt 模板：从 config/prompts/*.yaml 加载，用 Jinja2 渲染。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml
from jinja2 import Environment, StrictUndefined

_env = Environment(trim_blocks=True, lstrip_blocks=True, undefined=StrictUndefined, autoescape=False)


@dataclass(frozen=True)
class TemplateSpec:
    name: str
    version: int
    title: str
    description: str
    system: str
    user: str


def load_template_specs(config_dir: Path) -> list[TemplateSpec]:
    specs = []
    for path in sorted((config_dir / "prompts").glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        specs.append(TemplateSpec(
            name=raw["name"], version=int(raw["version"]), title=raw.get("title", raw["name"]),
            description=raw.get("description", ""), system=raw["system"], user=raw["user"],
        ))
    return specs


def render(system_tpl: str, user_tpl: str, ctx: dict) -> tuple[str, str]:
    return _env.from_string(system_tpl).render(**ctx).strip(), _env.from_string(user_tpl).render(**ctx).strip()
