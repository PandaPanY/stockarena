"""策略导入导出：把冠军版本连同股票池、检验设置、来源和测试历史存成一个 YAML 文件，可以分享给别人导入。

- 导出：规则（spec）、股票池、检验设置（区间、门槛、本金）、来源（平台、策略名、版本）、测试历史
  （累计尝试的开发期夏普 trial_scores、当前冠军以来已检验的候选数 prior_tests）、导出时的开发期夏普。
- 导入：校验规则 → 按导入者自己的数据和交易成本重新回测 → 测试历史从文件继承（升级门槛按 prior_tests 收紧，
  DSR 把 trial_scores 算作尝试），避免「导出再导入」把测试次数洗掉；重新回测的开发期夏普与文件差别大时提示。
- 文件可以手工修改，继承测试次数是一种诚实约定，挡不住故意篡改；导入的策略来源会一直标注在策略页上。
"""

from __future__ import annotations

from datetime import date

import yaml
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.timeutils import utcnow
from app.models.quant import QuantStrategyRow, QuantVersionRow
from app.quant import service
from app.quant.service import LabError, LabSettings
from app.quant.spec import parse_spec

FORMAT = "stockarena-strategy/1"
MAX_TRIALS = 2000
SHARPE_WARN = 0.15            # 重新回测的开发期夏普与文件相差超过该值时提示（数据或成本不同）


def export(s: Session, st: QuantStrategyRow) -> dict:
    champ = s.get(QuantVersionRow, st.champion_id) if st.champion_id else None
    if champ is None:
        raise LabError("策略还没有冠军版本，无法导出")
    trials = [round(x, 4) for x in service.trial_scores(s, st)]
    return {
        "format": FORMAT,
        "name": st.name,
        "description": st.description,
        "style": st.style,
        "universe": st.universe_name,
        "settings": service.settings_of(st).to_dict(),
        "spec": champ.spec,
        "provenance": {
            "exported_at": utcnow().isoformat(timespec="seconds"),
            "platform": "StockArena", "source_url": get_settings().source_url,
            "strategy": st.name, "version": champ.number, "version_title": champ.title,
            "dev_sharpe": (champ.dev or {}).get("sharpe"),
            "tests": {"trials": len(trials), "trial_scores": trials,
                      "prior_tests": service.prior_tests(s, st, champ.id)},
            "origin": st.origin or None,
        },
    }


def dumps(data: dict) -> str:
    head = ("# StockArena 策略文件：在「策略库 → 导入策略」导入。规则（spec）可以手工修改，\n"
            "# 但 provenance.tests（测试历史）请保持原样：导入后会继承，用来收紧升级门槛、计算折算可信度。\n")
    return head + yaml.safe_dump(data, allow_unicode=True, sort_keys=False)


def parse(text: str) -> dict:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise LabError(f"文件不是有效的 YAML：{e}") from None
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise LabError(f"不是 StockArena 策略文件（format 应为 {FORMAT}）")
    if not isinstance(data.get("spec"), dict):
        raise LabError("文件缺少规则（spec）")
    try:
        parse_spec(data["spec"])
    except ValueError as e:
        raise LabError(f"规则不合法：{e}") from None
    return data


def _trials(data: dict) -> tuple[list[float], int]:
    tests = ((data.get("provenance") or {}).get("tests")) or {}
    scores = [float(x) for x in (tests.get("trial_scores") or []) if isinstance(x, int | float)][:MAX_TRIALS]
    prior = tests.get("prior_tests")
    prior = int(prior) if isinstance(prior, int | float) and prior >= 0 else 0
    return scores, prior


def import_strategy(s: Session, text: str, user_id: int, name: str | None = None,
                    universe: str | None = None) -> tuple[QuantStrategyRow, list[str]]:
    """导入策略（新建私有策略并回测）。返回（策略, 提示列表）。"""
    from app.arena.deps import universe_resolver

    data = parse(text)
    warnings: list[str] = []
    uname = universe or data.get("universe") or service.DEFAULT_UNIVERSE
    uspec = universe_resolver().specs.get(uname)
    if uspec is None:
        warnings.append(f"本平台没有股票池「{uname}」，改用默认的 {service.DEFAULT_UNIVERSE}")
        uname = service.DEFAULT_UNIVERSE
    elif not (uspec.history and uspec.point_in_time) and uname != service.QUICK_UNIVERSE:
        warnings.append(f"「{uspec.title}」不是全时点股票池，回测有幸存者偏差")
    settings = LabSettings.from_dict(data.get("settings") or {}).to_dict()
    scores, prior = _trials(data)
    prov = data.get("provenance") or {}
    title = f"导入自「{prov.get('strategy') or data.get('name') or '策略文件'}」v{prov.get('version', '?')}"
    st = service.create_strategy(
        s, name=(name or data.get("name") or "导入的策略")[:64], style=str(data.get("style") or "custom")[:32],
        description=str(data.get("description") or ""), spec=data["spec"], universe_name=uname,
        settings=settings, title=title, reason=title, user_id=user_id)
    st.origin = {"kind": "import", "strategy_name": prov.get("strategy"), "version_number": prov.get("version"),
                 "exported_at": prov.get("exported_at"), "source_url": prov.get("source_url"),
                 "prior_tests": prior, "trial_scores": scores, "base_version_id": st.champion_id,
                 "at": utcnow().isoformat()}
    champ = s.get(QuantVersionRow, st.champion_id)
    old, new = prov.get("dev_sharpe"), (champ.dev or {}).get("sharpe")
    if isinstance(old, int | float) and new is not None and abs(new - old) > SHARPE_WARN:
        warnings.append(f"按本平台的数据和交易成本重新回测，开发期夏普为 {new:.2f}，文件中为 {old:.2f}，"
                        "差别较大（可能是数据、股票池或成本设置不同）")
    if not scores and not prior:
        warnings.append("文件里没有测试历史，按全新策略计算测试次数")
    return st, warnings


def filename(st: QuantStrategyRow) -> str:
    return f"{st.name}-{date.today().isoformat()}.stockarena.yaml"
