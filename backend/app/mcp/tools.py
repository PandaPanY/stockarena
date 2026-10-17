"""MCP 工具：在进程内转调平台自己的接口（权限、数据隔离、交易成本与网页完全一致），再整理成适合 AI 阅读的精简结果。

研究方法的约定在这里同样适用：
- **不提供留出期结果**（以及用到留出期的排名相关、滚动选择检验）：AI 客户端可以反复试，如果它能看到留出期，
  就会按留出期挑策略，留出期就失去了检验意义。留出期和实时跟踪请在网页上看。
- **试算计入测试次数**：preview_backtest 的结果按令牌记在内存里，下次 create_strategy 时作为新建前的试算一并提交
  （与网页新建对话框相同），不能试一百次只交最好的那一个。服务重启会清空未提交的记录。
- 修改已有策略只能用 submit_version：每个版本都会保存、计入测试次数，并且要通过开发期的显著性检验才会升级。
"""

from __future__ import annotations

import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.auth import core
from app.core.config import get_settings


class ToolError(Exception):
    """工具执行失败（返回给 AI 的 isError 结果，不是协议错误）。"""


@dataclass
class Context:
    app: Any                         # FastAPI 应用（进程内转调）
    user_id: int
    token_id: int | None
    can_write: bool


@dataclass
class Tool:
    name: str
    title: str
    description: str
    schema: dict
    handler: Callable[[Context, dict], Awaitable[Any]]
    write: bool = False
    read_only_hint: bool = field(init=False)

    def __post_init__(self) -> None:
        self.read_only_hint = not self.write

    def describe(self) -> dict:
        return {"name": self.name, "title": self.title, "description": self.description, "inputSchema": self.schema,
                "annotations": {"title": self.title, "readOnlyHint": self.read_only_hint,
                                "destructiveHint": False, "openWorldHint": False}}


# ---------------------------------------------------------------- 进程内转调


async def call_api(ctx: Context, method: str, path: str, json: dict | None = None,
                   params: dict | None = None) -> Any:
    headers = {}
    if get_settings().auth_enabled:
        st = core.user_state(ctx.user_id)
        headers["cookie"] = f"{core.COOKIE_NAME}={core.issue_token(ctx.user_id, st.gen)}"
    transport = httpx.ASGITransport(app=ctx.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://stockarena.mcp", timeout=900) as c:
        r = await c.request(method, path, json=json, params=params, headers=headers)
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail")
        except ValueError:
            detail = r.text[:300]
        if isinstance(detail, list):         # 参数校验错误
            detail = "；".join(f"{'.'.join(str(x) for x in d.get('loc', [])[1:])}：{d.get('msg')}" for d in detail)
        raise ToolError(str(detail or f"HTTP {r.status_code}"))
    return r.json()


# ---------------------------------------------------------------- 整理结果

HIDDEN_KEYS = {"curves", "champion_curves", "rank_corr", "rank_n", "walk_forward", "tracking", "readiness"}
METRIC_KEYS = ("annualized_return", "cumulative_return", "sharpe", "calmar", "max_drawdown", "excess_return",
               "turnover", "days", "vs_champion", "yearly")


def dev_only(o: Any) -> Any:
    """去掉留出期及用到留出期的字段、曲线等大块数据。"""
    if isinstance(o, dict):
        return {k: dev_only(v) for k, v in o.items() if "holdout" not in k and k not in HIDDEN_KEYS}
    if isinstance(o, list):
        return [dev_only(x) for x in o]
    return o


def _metrics(m: dict | None) -> dict | None:
    return {k: m[k] for k in METRIC_KEYS if k in m} if m else None


def _version(v: dict | None, full: bool = False) -> dict | None:
    if not v:
        return None
    out = {k: v.get(k) for k in ("id", "number", "status", "source", "title", "decision", "score")}
    out["dev"] = _metrics(v.get("dev"))
    if full:
        out |= {"rationale": v.get("rationale"), "rules": v.get("rules"), "spec": v.get("spec"),
                "train": _metrics(v.get("train")), "valid": _metrics(v.get("valid")),
                "robustness": v.get("robustness"), "created_at": v.get("created_at")}
    return dev_only(out)


def _origin(o: dict | None) -> dict | None:
    if not o:
        return None
    out = {k: v for k, v in o.items() if k != "trial_scores"}
    out["trial_count"] = len(o.get("trial_scores") or [])
    return out


def _run(r: dict | None, log: int = 0) -> dict | None:
    if not r:
        return None
    out = {k: r.get(k) for k in ("id", "strategy_id", "trigger", "status", "rounds", "rounds_done", "promoted",
                                 "progress", "summary", "error", "created_at", "finished_at")}
    if log:
        out["log"] = dev_only((r.get("log") or [])[-log:])
    return out


# ---------------------------------------------------------------- 试算记录（计入下次新建的测试次数）

_previews: dict[tuple[int, int | None], list[float | None]] = {}
_previews_lock = threading.Lock()
MAX_PENDING = 500


def _record_preview(ctx: Context, sharpe: float | None) -> int:
    with _previews_lock:
        lst = _previews.setdefault((ctx.user_id, ctx.token_id), [])
        if len(lst) < MAX_PENDING:
            lst.append(sharpe)
        return len(lst)


def pending_previews(ctx: Context) -> list[float | None]:
    with _previews_lock:
        return list(_previews.get((ctx.user_id, ctx.token_id), []))


def _clear_previews(ctx: Context) -> None:
    with _previews_lock:
        _previews.pop((ctx.user_id, ctx.token_id), None)


# ---------------------------------------------------------------- 工具实现

def _int(args: dict, key: str) -> int:
    v = args.get(key)
    if isinstance(v, bool) or not isinstance(v, int | float) or int(v) != v:
        raise ToolError(f"参数 {key} 必须是整数")
    return int(v)


async def list_strategies(ctx: Context, args: dict) -> Any:
    data = await call_api(ctx, "GET", "/api/quant/strategies")
    out = []
    for st in data["strategies"]:
        out.append({k: st.get(k) for k in ("id", "name", "style", "universe_name", "mine", "is_public", "versions",
                                           "promotions", "dsr", "auto_optimize")}
                   | {"champion": _version(st.get("champion")), "last_run": _run(st.get("last_run"))})
    return {"strategies": out,
            "note": "mine=false 的是公共策略：可以查看、试算，不能修改。"
                    "dev 是开发期（训练+验证）指标；留出期结果请在网页上看。"}


async def get_strategy(ctx: Context, args: dict) -> Any:
    sid = _int(args, "strategy_id")
    d = await call_api(ctx, "GET", f"/api/quant/strategies/{sid}")
    return {**{k: d.get(k) for k in ("id", "name", "style", "description", "universe_name", "benchmark", "mine",
                                     "is_public", "auto_optimize", "model", "versions", "promotions", "dsr")},
            "settings": d.get("settings_full"),
            "origin": _origin(d.get("origin")),
            "champion": _version(d.get("champion_detail"), full=True),
            "quality": dev_only(d.get("quality")),
            "versions_list": [_version(v) for v in (d.get("version_list") or [])[:30]],
            "research_notes": (d.get("research_notes") or [])[-10:],
            "recent_runs": [_run(r) for r in (d.get("runs") or [])[:5]]}


async def get_version(ctx: Context, args: dict) -> Any:
    vid = _int(args, "version_id")
    return _version(await call_api(ctx, "GET", f"/api/quant/versions/{vid}"), full=True)


async def get_timeline(ctx: Context, args: dict) -> Any:
    sid = _int(args, "strategy_id")
    limit = min(max(int(args.get("limit") or 20), 1), 100)
    d = await call_api(ctx, "GET", f"/api/quant/strategies/{sid}/timeline")
    events = d.get("events") or []
    return dev_only({"start": d.get("start"), "summary": d.get("summary"), "events": events[:limit],
                     "omitted": max(len(events) - limit, 0)})


async def get_signal(ctx: Context, args: dict) -> Any:
    sid = _int(args, "strategy_id")
    params = {"capital": args["capital"]} if args.get("capital") else None
    return dev_only(await call_api(ctx, "GET", f"/api/quant/strategies/{sid}/signal", params=params))


async def get_spec_reference(ctx: Context, args: dict) -> Any:
    factors_ = await call_api(ctx, "GET", "/api/quant/factors")
    options = await call_api(ctx, "GET", "/api/quant/spec-options")
    return {"spec_options": options, "factors": factors_,
            "hint": "策略规格（spec）的完整示例可以用 get_strategy 查看任一策略冠军版本的 spec。"}


async def preview_backtest(ctx: Context, args: dict) -> Any:
    if not isinstance(args.get("spec"), dict):
        raise ToolError("参数 spec 必须是策略规格对象")
    body = {"spec": args["spec"]}
    if args.get("universe_name"):
        body["universe_name"] = args["universe_name"]
    d = await call_api(ctx, "POST", "/api/quant/preview", json=body)
    n = _record_preview(ctx, (d.get("dev") or {}).get("sharpe"))
    return dev_only(d) | {"pending_trials": n,
                          "note": f"已记录本次试算（共 {n} 次未提交），下次 create_strategy 时计入测试次数。"}


async def get_run(ctx: Context, args: dict) -> Any:
    rid = _int(args, "run_id")
    return _run(await call_api(ctx, "GET", f"/api/quant/runs/{rid}"), log=int(args.get("log") or 10))


async def create_strategy(ctx: Context, args: dict) -> Any:
    if not isinstance(args.get("spec"), dict) or not str(args.get("name") or "").strip():
        raise ToolError("需要 name（名称）和 spec（策略规格）")
    trials = pending_previews(ctx)
    body = {"name": str(args["name"]).strip()[:64], "spec": args["spec"],
            "description": str(args.get("description") or ""), "style": str(args.get("style") or "custom"),
            "trial_scores": trials}
    if args.get("universe_name"):
        body["universe_name"] = args["universe_name"]
    d = await call_api(ctx, "POST", "/api/quant/strategies", json=body)
    _clear_previews(ctx)
    return {"id": d.get("id"), "name": d.get("name"), "universe_name": d.get("universe_name"),
            "champion": _version(d.get("champion")), "prior_trials_counted": len(trials),
            "note": "已新建私有策略并完整回测。之后修改请用 submit_version；"
                    "启动 AI 优化需要先在网页上为策略选择研究模型。"}


async def submit_version(ctx: Context, args: dict) -> Any:
    sid = _int(args, "strategy_id")
    if not isinstance(args.get("spec"), dict):
        raise ToolError("参数 spec 必须是策略规格对象")
    body = {"spec": args["spec"], "title": str(args.get("title") or "")[:100],
            "rationale": str(args.get("rationale") or "")[:2000]}
    return _version(await call_api(ctx, "POST", f"/api/quant/strategies/{sid}/versions", json=body), full=True)


async def start_optimization(ctx: Context, args: dict) -> Any:
    sid = _int(args, "strategy_id")
    body = {"rounds": _int(args, "rounds")} if args.get("rounds") is not None else {}
    d = await call_api(ctx, "POST", f"/api/quant/strategies/{sid}/research", json=body)
    return _run(d) | {"note": "已在后台开始，用 get_run 查看进度（每轮通常要几分钟）。"}


# ---------------------------------------------------------------- 工具清单

_SID = {"strategy_id": {"type": "integer", "description": "策略 ID（list_strategies 返回的 id）"}}
_SPEC = {"type": "object", "description": "策略规格（格式见 get_spec_reference，或参考已有策略冠军版本的 spec）"}
_LIMIT = {"type": "integer", "description": "最多返回的事件数，默认 20"}
_CAPITAL = {"type": "number", "description": "按多少资金折算股数（元）"}
_UNI = {"type": "string", "description": "股票池，默认 cn_csi300_hist（沪深300 全时点）；可选值见 get_spec_reference"}

TOOLS: list[Tool] = [
    Tool("list_strategies", "策略列表",
         "列出自己的策略和公共策略：ID、风格、股票池、冠军版本的开发期指标、版本数、折算可信度（DSR）、"
         "最近一次 AI 优化。",
         {"type": "object", "properties": {}}, list_strategies),
    Tool("get_strategy", "策略详情",
         "一条策略的说明、检验设置、来源与测试历史、冠军版本（规则文字、spec、训练/验证/开发期指标、稳健性）、"
         "研究质量（PBO、当前冠军以来已检验的候选数）、版本列表、研究笔记和最近的 AI 优化。只含开发期结果。",
         {"type": "object", "properties": _SID, "required": ["strategy_id"]}, get_strategy),
    Tool("get_version", "版本详情",
         "某个版本的规则、spec、开发期指标、检验结论（只含开发期）。",
         {"type": "object", "properties": {"version_id": {"type": "integer"}}, "required": ["version_id"]},
         get_version),
    Tool("get_timeline", "优化时间线",
         "策略从初始版本起的 AI 优化过程：每轮诊断、每个候选与当时冠军的规则差异、开发期指标、检验 p 值、是否采纳。",
         {"type": "object", "properties": _SID | {"limit": _LIMIT}, "required": ["strategy_id"]}, get_timeline),
    Tool("get_signal", "今日信号",
         "按冠军版本计算的最新目标持仓（股票、权重、按资金折算的股数）。仅供研究，不构成投资建议。",
         {"type": "object", "properties": _SID | {"capital": _CAPITAL}, "required": ["strategy_id"]}, get_signal),
    Tool("get_spec_reference", "规格说明",
         "策略规格（spec）可用的字段、选项和因子库（含参数范围）。写或改 spec 之前先看这里。",
         {"type": "object", "properties": {}}, get_spec_reference),
    Tool("preview_backtest", "试算",
         "按给定规格只回测开发期（不保存、不返回留出期）。每次试算都会被记录，下次 create_strategy 时计入测试次数。"
         "修改已有策略请用 submit_version。",
         {"type": "object", "properties": {"spec": _SPEC, "universe_name": _UNI}, "required": ["spec"]},
         preview_backtest),
    Tool("get_run", "AI 优化进度", "一次 AI 优化的状态、进度、总结和最近的日志。",
         {"type": "object", "properties": {"run_id": {"type": "integer"},
                                           "log": {"type": "integer", "description": "返回最近几条日志，默认 10"}},
          "required": ["run_id"]}, get_run),
    Tool("create_strategy", "新建策略",
         "新建自己的私有策略并完整回测；之前用 preview_backtest 试算的次数会一并计入测试次数。需要令牌有修改权限。",
         {"type": "object", "properties": {"name": {"type": "string", "maxLength": 64}, "spec": _SPEC,
                                           "description": {"type": "string"}, "style": {"type": "string"},
                                           "universe_name": _UNI},
          "required": ["name", "spec"]}, create_strategy, write=True),
    Tool("submit_version", "提交新版本",
         "给自己的策略提交一个修改后的规格：完整回测并做升级检验（开发期显著好于冠军才会升级），计入测试次数。需要修改权限。",
         {"type": "object", "properties": _SID | {"spec": _SPEC, "title": {"type": "string", "description": "改了什么"},
                                                  "rationale": {"type": "string", "description": "为什么这样改"}},
          "required": ["strategy_id", "spec"]}, submit_version, write=True),
    Tool("start_optimization", "启动 AI 优化",
         "用策略设置的研究模型在后台跑几轮 AI 优化（会消耗该模型的调用额度）。需要修改权限，且策略已选择研究模型。",
         {"type": "object", "properties": _SID | {"rounds": {"type": "integer", "minimum": 1, "maximum": 50}},
          "required": ["strategy_id"]}, start_optimization, write=True),
]

BY_NAME = {t.name: t for t in TOOLS}
