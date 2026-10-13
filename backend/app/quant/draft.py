"""一句话创建策略：用户用中文描述选股想法，AI 按策略规格生成规则（只生成规则，不回测）。

- 只能使用规格支持的字段与因子库里的因子；描述里无法用规格表达的条件必须逐条写进 unsupported，不能悄悄丢掉；
- 生成的规则经过与 AI 研究员相同的规格校验（parse_spec），不合法时带着错误让模型重试一次；
- 生成本身不看任何回测结果；之后在新建策略对话框里试算，每次试算都计入新策略的测试次数
  （见 service.create_strategy 的 trial_scores），避免「反复改到开发期好看为止」而不自知。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from app.agent.parse import DecisionParseError
from app.agent.runner import run_llm_json
from app.arena.strategy import llm_config_for
from app.db.session import get_sessionmaker
from app.llm import calc_cost, create_client
from app.quant.research import _factor_doc, lenient_json, load_prompt
from app.quant.service import LabError
from app.quant.spec import describe, parse_spec

logger = logging.getLogger(__name__)

TIMEOUT_S = 300
MAX_TEXT = 500

SYSTEM = """你是量化策略助手。用户会用中文描述一个 A 股选股想法，你要把它翻译成平台的「策略规格」JSON。

要求：
1. 只能使用下面「策略规格」里列出的字段和「因子库」里的因子，参数必须在给定范围内；不要编造不存在的因子或字段。
2. 用户没有说的部分用稳妥的常用值（如持有 20～30 只、每 20 个交易日调仓、等权、排除 ST）。
3. 用户描述里**无法用规格表达**的条件（例如分析师评级、新闻或舆情、特定事件、日内交易、做空、杠杆、期权、
   某个具体的财务科目而因子库里没有），必须逐条写进 unsupported，说明为什么做不到、用了什么近似（如果有）。
   不能悄悄丢掉，也不能假装已经实现。
4. 规格描述的是一条规则化策略：程序按规则回测，不执行任何代码。

只输出一个 JSON 对象：
{
  "name": "策略名称（不超过 12 个字）",
  "description": "一句话说明这条策略想抓住什么",
  "spec": { ...策略规格... },
  "unsupported": ["描述中没有实现的条件及原因，没有则为空列表"],
  "notes": "你做了哪些取舍（例如参数怎么定、哪些地方做了近似）"
}"""


@dataclass
class ParsedDraft:
    name: str
    description: str
    spec: object
    unsupported: list[str] = field(default_factory=list)
    notes: str = ""


def parse_draft(text: str) -> ParsedDraft:
    data = lenient_json(text)
    if not isinstance(data.get("spec"), dict):
        raise DecisionParseError("缺少 spec 对象")
    try:
        spec = parse_spec(data["spec"])
    except ValueError as e:
        raise DecisionParseError(f"规格不合法：{e}") from None
    raw = data.get("unsupported") or []
    if isinstance(raw, str):
        raw = [raw]
    unsupported = [str(x).strip()[:200] for x in raw if isinstance(x, str | int | float) and str(x).strip()][:10]
    return ParsedDraft(name=str(data.get("name") or "").strip()[:64], description=str(data.get("description") or "")
                       .strip()[:300], spec=spec, unsupported=unsupported, notes=str(data.get("notes") or "")[:1000])


def build_prompt(text: str, base_spec: dict | None) -> str:
    parts = [f"## 用户的描述\n{text.strip()}", f"## 策略规格\n{load_prompt()['spec_doc']}",
             f"## 因子库\n{_factor_doc()}"]
    if base_spec:
        parts.append("## 当前规则（用户希望在此基础上按描述修改；没提到的部分保持不变）\n"
                     + json.dumps(base_spec, ensure_ascii=False))
    return "\n\n".join(parts)


async def draft(provider_id: int, model: str | None, text: str, base_spec: dict | None = None) -> dict:
    """调用模型生成规则。调用方负责检查模型供应商属于当前用户。"""
    text = (text or "").strip()
    if not text:
        raise LabError("请描述你的选股想法")
    if len(text) > MAX_TEXT:
        raise LabError(f"描述不要超过 {MAX_TEXT} 个字")
    if base_spec is not None:
        try:
            base_spec = parse_spec(base_spec).model_dump()
        except ValueError as e:
            raise LabError(f"当前规则不合法：{e}") from None
    with get_sessionmaker()() as s:
        try:
            cfg, model_name, params, json_mode = llm_config_for(s, provider_id, model)
        except Exception as e:  # noqa: BLE001 - 供应商不存在、密钥无法解密等
            raise LabError(str(e)) from None
    out = await run_llm_json(create_client(cfg), model=model_name, system=SYSTEM, user=build_prompt(text, base_spec),
                             params={"temperature": 0.2, **params}, json_mode=json_mode, timeout_s=TIMEOUT_S,
                             parse=parse_draft)
    if out.status != "ok":
        logger.warning("一句话创建策略失败：%s", out.error)
        raise LabError(f"AI 没有生成可用的规则：{out.error}")
    d: ParsedDraft = out.parsed
    spec = d.spec.model_dump()
    return {"name": d.name, "description": d.description, "spec": spec, "rules": describe(spec),
            "unsupported": d.unsupported, "notes": d.notes, "tokens_in": out.tokens_in, "tokens_out": out.tokens_out,
            "cost": float(calc_cost(cfg, out.tokens_in, out.tokens_out))}
