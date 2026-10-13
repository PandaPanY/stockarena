"""策略规格：规则化策略的 JSON 描述（PRD 3.8）。AI 只能输出这种结构，程序校验后才回测，不执行任何代码。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.quant.factors import FACTORS, normalize_params


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# 申万 2021 一级行业（31 个）
SW_L1 = ("农林牧渔", "基础化工", "钢铁", "有色金属", "电子", "汽车", "家用电器", "食品饮料", "纺织服饰", "轻工制造",
         "医药生物", "公用事业", "交通运输", "房地产", "商贸零售", "社会服务", "银行", "非银金融", "综合", "建筑材料",
         "建筑装饰", "电力设备", "机械设备", "国防军工", "计算机", "传媒", "通信", "煤炭", "石油石化", "环保",
         "美容护理")


def _check_industries(names: list[str]) -> list[str]:
    bad = [n for n in names if n not in SW_L1]
    if bad:
        raise ValueError(f"未知行业 {', '.join(bad)}，可用（申万一级）：{'、'.join(SW_L1)}")
    return names


class UniverseFilter(_Strict):
    types: list[Literal["stock", "etf"]] = Field(default_factory=lambda: ["stock"], min_length=1,
                                                 description="可选的品种：stock 股票 / etf 基金")
    symbols: list[str] = Field(default_factory=list, max_length=60, description="非空时只在这些标的中选")
    exclude: list[str] = Field(default_factory=list, max_length=60, description="排除的标的")
    exclude_st: bool = True
    min_listed_days: int = Field(120, ge=0, le=1000, description="上市（有行情）满多少个交易日")
    min_amount: float = Field(0.5, ge=0, le=50, description="20 日平均成交额下限（亿元）")
    industries: list[str] = Field(default_factory=list, max_length=31, description="非空时只选这些申万一级行业的股票")
    exclude_industries: list[str] = Field(default_factory=list, max_length=31, description="排除的申万一级行业")

    @model_validator(mode="after")
    def _check_ind(self):
        _check_industries(self.industries)
        _check_industries(self.exclude_industries)
        return self


class FactorTerm(_Strict):
    name: str
    params: dict[str, float] = Field(default_factory=dict)
    weight: float = Field(1.0, gt=0, le=10)
    direction: Literal["high", "low"] = Field("high", description="high：值越大越好；low：值越小越好")
    neutralize: Literal["none", "industry"] = Field(
        "none", description="industry：在同一申万一级行业内排名（比较同行业的股票，避免集中押注某个行业）")

    @model_validator(mode="after")
    def _check(self):
        self.params = normalize_params(self.name, self.params)
        return self


class FilterRule(_Strict):
    name: str
    params: dict[str, float] = Field(default_factory=dict)
    op: Literal[">", ">=", "<", "<="]
    value: float

    @model_validator(mode="after")
    def _check(self):
        self.params = normalize_params(self.name, self.params)
        return self


class Selection(_Strict):
    top_n: int = Field(10, ge=1, le=50, description="持有标的数量")
    buffer: int = Field(0, ge=0, le=50, description="已持有标的排名在 top_n + buffer 以内时继续持有（降低换手）")
    max_per_industry: int | None = Field(None, ge=1, le=50,
                                         description="同一申万一级行业最多持有几只股票（不限为 null）")


class Weighting(_Strict):
    method: Literal["equal", "score", "inverse_vol"] = "equal"
    vol_window: int = Field(60, ge=10, le=250, description="inverse_vol 使用的波动率窗口")
    max_weight: float = Field(0.2, ge=0.02, le=1.0, description="单个标的权重上限")


class Rebalance(_Strict):
    every_days: int = Field(5, ge=1, le=60, description="每隔多少个交易日调仓")
    min_trade_pct: float = Field(0.01, ge=0, le=0.1, description="权重变化小于该值时不交易（新建仓与清仓除外）")


class Timing(_Strict):
    """大盘择时：基准指数满足条件时正常仓位，否则降到 exposure_off，剩余资金可放入防守资产。"""

    enabled: bool = False
    name: str = "ma_bias"
    params: dict[str, float] = Field(default_factory=lambda: {"window": 60})
    op: Literal[">", ">=", "<", "<="] = ">"
    value: float = 0.0
    exposure_off: float = Field(0.0, ge=0, le=1, description="条件不满足时的股票总仓位")
    defensive: str | None = Field(None, description="条件不满足时剩余资金买入的防守标的（如国债 ETF 511010.SH）")

    @model_validator(mode="after")
    def _check(self):
        if self.enabled:
            fd = FACTORS.get(self.name)
            if fd is None or not fd.timing:
                raise ValueError(f"大盘择时不能使用因子 {self.name}，可用："
                                 f"{', '.join(k for k, f in FACTORS.items() if f.timing)}")
            self.params = normalize_params(self.name, self.params)
        return self


class Risk(_Strict):
    exposure: float = Field(0.98, ge=0.1, le=1.0, description="正常情况下的总仓位")
    stop_loss: float | None = Field(None, ge=0.02, le=0.5, description="相对买入成本亏损达到该比例时卖出")
    trailing_stop: float | None = Field(None, ge=0.02, le=0.5, description="相对持有期最高价回撤达到该比例时卖出")


class StrategySpec(_Strict):
    universe: UniverseFilter = Field(default_factory=UniverseFilter)
    factors: list[FactorTerm] = Field(min_length=1, max_length=6)
    filters: list[FilterRule] = Field(default_factory=list, max_length=6)
    selection: Selection = Field(default_factory=Selection)
    weighting: Weighting = Field(default_factory=Weighting)
    rebalance: Rebalance = Field(default_factory=Rebalance)
    timing: Timing = Field(default_factory=Timing)
    risk: Risk = Field(default_factory=Risk)

    @model_validator(mode="after")
    def _consistency(self):
        if "stock" not in self.universe.types:
            fund = [f.name for f in self.factors if FACTORS[f.name].fundamental]
            fund += [f.name for f in self.filters if FACTORS[f.name].fundamental]
            if fund:
                raise ValueError(f"只选 ETF 时不能使用财报因子（{', '.join(fund)}），ETF 没有财报数据")
        return self


def parse_spec(data: dict) -> StrategySpec:
    """校验规格；失败时抛出 ValueError，消息是给 AI 看的中文说明。"""
    try:
        return StrategySpec.model_validate(data)
    except ValidationError as e:
        msgs = []
        for err in e.errors()[:8]:
            loc = ".".join(str(x) for x in err["loc"])
            msg = err["msg"].removeprefix("Value error, ")
            msgs.append(f"{loc}: {msg}" if loc else msg)
        raise ValueError("；".join(msgs)) from None


# ---------------------------------------------------------------- 中文说明

_OPS = {">": "大于", ">=": "不低于", "<": "小于", "<=": "不高于"}


def _factor_text(name: str, params: dict) -> str:
    fd = FACTORS[name]
    if not params:
        return fd.title
    ps = "、".join(f"{fd.params[k].title or k} {v:g}" for k, v in params.items())
    return f"{fd.title}（{ps}）"


def _pct(x: float) -> str:
    return f"{x * 100:g}%"


def describe(spec: StrategySpec | dict) -> list[str]:
    """把规格翻译成中文规则列表，供界面展示和 AI 阅读。"""
    if isinstance(spec, dict):
        spec = parse_spec(spec)
    u = spec.universe
    kinds = "、".join({"stock": "股票", "etf": "ETF"}[t] for t in u.types)
    pool = f"指定的 {len(u.symbols)} 个标的" if u.symbols else f"股票池中的{kinds}"
    lines = [f"选股范围：{pool}" + ("，排除 ST" if u.exclude_st and "stock" in u.types else "")
             + (f"，上市满 {u.min_listed_days} 个交易日" if u.min_listed_days else "")
             + (f"，20 日平均成交额 ≥ {u.min_amount:g} 亿元" if u.min_amount else "")
             + (f"，排除 {', '.join(u.exclude)}" if u.exclude else "")
             + (f"，只选行业：{'、'.join(u.industries)}" if u.industries else "")
             + (f"，排除行业：{'、'.join(u.exclude_industries)}" if u.exclude_industries else "")]
    for f in spec.filters:
        lines.append(f"过滤：{_factor_text(f.name, f.params)} {_OPS[f.op]} {f.value:g}")
    terms = [f"{_factor_text(f.name, f.params)}{'越高越好' if f.direction == 'high' else '越低越好'}"
             f"{'（行业内排名）' if f.neutralize == 'industry' else ''}（权重 {f.weight:g}）" for f in spec.factors]
    lines.append("打分：" + "；".join(terms))
    s = spec.selection
    lines.append(f"选股：得分最高的 {s.top_n} 只" + (f"，已持有的排名在前 {s.top_n + s.buffer} 名内继续持有"
                                               if s.buffer else "")
                 + (f"，同一行业最多 {s.max_per_industry} 只" if s.max_per_industry else ""))
    w = spec.weighting
    method = {"equal": "等权", "score": "按得分加权", "inverse_vol": f"按 {w.vol_window} 日波动率倒数加权"}[w.method]
    lines.append(f"权重：{method}，单只不超过 {_pct(w.max_weight)}，总仓位 {_pct(spec.risk.exposure)}")
    r = spec.rebalance
    lines.append(f"调仓：每 {r.every_days} 个交易日一次" + (f"，已有持仓权重变化小于 {_pct(r.min_trade_pct)} 不加减仓"
                                                    if r.min_trade_pct else ""))
    t = spec.timing
    if t.enabled:
        off = f"仓位降到 {_pct(t.exposure_off)}" + (f"，其余资金买入 {t.defensive}" if t.defensive else "")
        lines.append(f"大盘择时：基准{_factor_text(t.name, t.params)} {_OPS[t.op]} {t.value:g} 时正常持仓，否则{off}")
    rk = spec.risk
    stops = []
    if rk.stop_loss:
        stops.append(f"亏损 {_pct(rk.stop_loss)} 止损")
    if rk.trailing_stop:
        stops.append(f"从持有期高点回撤 {_pct(rk.trailing_stop)} 卖出")
    if stops:
        lines.append("风控：" + "，".join(stops) + "（收盘判断，次日开盘卖出，到下次调仓前不再买回）")
    return lines


def numeric_knobs(spec: StrategySpec) -> list[tuple[tuple, float, bool]]:
    """可做稳健性扰动的数值参数：[(路径, 当前值, 是否整数)]。"""
    knobs: list[tuple[tuple, float, bool]] = []
    for i, f in enumerate(spec.factors):
        for k, v in f.params.items():
            knobs.append((("factors", i, "params", k), v, FACTORS[f.name].params[k].integer))
    for i, f in enumerate(spec.filters):
        for k, v in f.params.items():
            knobs.append((("filters", i, "params", k), v, FACTORS[f.name].params[k].integer))
        if f.value != 0:
            knobs.append((("filters", i, "value"), f.value, False))
    knobs.append((("selection", "top_n"), spec.selection.top_n, True))
    knobs.append((("rebalance", "every_days"), spec.rebalance.every_days, True))
    if spec.risk.stop_loss:
        knobs.append((("risk", "stop_loss"), spec.risk.stop_loss, False))
    if spec.risk.trailing_stop:
        knobs.append((("risk", "trailing_stop"), spec.risk.trailing_stop, False))
    if spec.timing.enabled:
        for k, v in spec.timing.params.items():
            knobs.append((("timing", "params", k), v, FACTORS[spec.timing.name].params[k].integer))
    return knobs
