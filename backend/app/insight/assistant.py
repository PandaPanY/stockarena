"""问答助手：用户在页面上提问，模型通过查询工具读取该用户跟踪中的策略 / 组合的数据后回答。

工具只读，只能查当前用户自己跟踪的账户（data.owned_account 校验）；对话历史由页面保存，每次请求带上最近几轮。
"""

from __future__ import annotations

import json
import logging

from app.agent.runner import DecisionParseError, run_llm_tools
from app.db.session import get_sessionmaker
from app.insight import data, llm
from app.llm import ToolSpec

logger = logging.getLogger(__name__)

MAX_TOOL_CALLS = 8
MAX_HISTORY = 6                 # 带上最近几条对话
TIMEOUT_S = 240
SUBMIT = "submit_decision"      # run_llm_tools 的结束工具名

SYSTEM = """你是 StockArena 平台里的问答助手，帮用户看懂他正在实时跟踪的量化策略和组合（模拟账户）。

- 先用工具查数据再回答，不要凭印象回答账户里的事；数字直接引用工具结果。
  工具返回的收益、权重都是小数（0.012 = 1.2%）。
- 不知道账户编号时先调用 list_followed。问「为什么买 / 卖某只股票」时调用 explain_symbol，
  它会给出规则层面的原因（排名、因子、过滤条件、择时）。
- 这些是规则化的量化策略，买卖由规则自动决定，解释时说明是规则的结果，不要说成人的主观判断。
- 用简体中文、大白话，简洁；可以用 Markdown 列表或表格。
- 不给买卖建议，不预测后市，不评价股票值不值得买；用户问这些时说明你只能解释策略和数据。
- 查不到的数据直接说查不到，不要编造。
- 回答写好后调用 submit_decision 提交，参数 {"answer": "回答内容"}。"""


def _schema(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required}


_ACC = {"account_id": {"type": "integer", "description": "跟踪账户编号（list_followed 返回的 account_id）"}}

TOOLS = [
    ToolSpec("list_followed", "列出用户正在实时跟踪的策略和组合：名称、账户编号、可实盘状态、健康度。",
             _schema({}, [])),
    ToolSpec("get_performance", "账户最近 N 个交易日（不填为跟踪以来）的收益、同期基准收益、期间最大回撤。",
             _schema({**_ACC, "days": {"type": "integer", "description": "交易日数，如 5、20、60"}}, ["account_id"])),
    ToolSpec("get_holdings", "账户当前持仓：数量、成本、最新收盘价、市值占比、浮动盈亏、"
             "由哪些成员策略选中。",
             _schema(_ACC, ["account_id"])),
    ToolSpec("get_orders", "账户最近的订单：日期、方向、数量、状态、下单理由、未成交原因。",
             _schema({**_ACC, "limit": {"type": "integer", "description": "条数，默认 20"}}, ["account_id"])),
    ToolSpec("get_contributions", "最近 N 个交易日各持仓对账户净值的贡献（涨得最多和拖累最多的几只）。",
             _schema({**_ACC, "days": {"type": "integer"}}, ["account_id", "days"])),
    ToolSpec("explain_symbol", "某只股票为什么在 / 不在目标持仓里：最近一次决策时每个成员策略对它的判断（排名和因子，"
             "或没入选的原因），以及最近相关的订单。", _schema({**_ACC, "symbol": {
                 "type": "string", "description": "代码如 600036.SH，或股票名称"}}, ["account_id", "symbol"])),
    ToolSpec("get_members", "组合有哪些成员策略、各占多少资金；以及该账户的健康度检查明细。",
             _schema(_ACC, ["account_id"])),
    ToolSpec("get_notes", "最近的 AI 每周点评或偏差分析。", _schema({"kind": {
        "type": "string", "enum": ["weekly", "deviation"]}}, [])),
    ToolSpec(SUBMIT, "提交最终回答。",
             _schema({"answer": {"type": "string", "description": "给用户的回答（Markdown）"}}, ["answer"])),
]


def _resolve_symbol(s, text: str) -> str:
    from sqlalchemy import select

    from app.models import InstrumentRow

    t = text.strip()
    if "." in t:
        return t.upper()
    if t.isdigit() and len(t) == 6:
        from app.market.symbols import normalize_cn

        return normalize_cn(t)
    row = s.scalars(select(InstrumentRow).where(InstrumentRow.name == t)).first() or \
        s.scalars(select(InstrumentRow).where(InstrumentRow.name.like(f"%{t}%"))).first()
    if row is None:
        raise data.InsightError(f"找不到「{text}」，请给出股票代码")
    return row.symbol


def run_tool(uid: int, name: str, args: dict) -> object:
    """执行一个查询工具（同步，在线程里调用）。账户必须属于该用户。"""
    from sqlalchemy import select

    from app.models.insight import AiNoteRow

    with get_sessionmaker()() as s:
        if name == "list_followed":
            return data.followed(s, uid)
        if name == "get_notes":
            q = select(AiNoteRow).where(AiNoteRow.user_id == uid, AiNoteRow.status == "ok")
            if args.get("kind"):
                q = q.where(AiNoteRow.kind == args["kind"])
            return [{"kind": n.kind, "name": n.ref_name, "period_end": str(n.period_end), "content": n.content}
                    for n in s.scalars(q.order_by(AiNoteRow.id.desc()).limit(3))]
        acc = int(args.get("account_id") or 0)
        ref = data.owned_account(s, uid, acc)
        if name == "get_performance":
            return data.performance(s, acc, int(args["days"]) if args.get("days") else None)
        if name == "get_holdings":
            return data.holdings(s, acc)
        if name == "get_orders":
            return data.recent_orders(s, acc, min(int(args.get("limit") or 20), 50))
        if name == "get_contributions":
            from datetime import date

            perf = data.performance(s, acc, max(1, int(args.get("days") or 5)))
            if not perf.get("days"):
                return perf
            return {"period": perf, **data.contributions(s, acc, date.fromisoformat(perf["start"]),
                                                        date.fromisoformat(perf["end"]), top=8)}
        if name == "explain_symbol":
            return data.explain_symbol(s, acc, _resolve_symbol(s, str(args.get("symbol") or "")))
        if name == "get_members":
            from app.insight.notes import _ref_row

            health = (_ref_row(s, ref).readiness or {})
            return {"members": data.members(s, ref), "readiness": health.get("status"),
                    "health": health.get("health")}
    raise data.InsightError(f"没有这个工具：{name}")


def _parse(raw: str) -> str:
    text = (raw or "").strip()
    if text.startswith("{"):
        try:
            obj = json.loads(text)
            if isinstance(obj, dict) and obj.get("answer"):
                return str(obj["answer"]).strip()
        except ValueError:
            pass
    if not text:
        raise DecisionParseError("回答为空")
    return text


def _history_text(history: list[dict]) -> str:
    lines = []
    for h in history[-MAX_HISTORY:]:
        who = "用户" if h.get("role") == "user" else "助手"
        lines.append(f"{who}：{str(h.get('content') or '')[:1500]}")
    return "之前的对话：\n" + "\n".join(lines) + "\n\n" if lines else ""


async def ask(uid: int, question: str, history: list[dict] | None = None) -> dict:
    """回答一个问题。返回 {status, answer, error, trace, model, tokens_in, tokens_out, seconds}。"""
    import asyncio

    question = question.strip()
    if not question:
        raise data.InsightError("问题不能为空")
    with get_sessionmaker()() as s:
        client, model, params = llm.client_for(s, uid)

    async def execute(name: str, args: dict) -> str:
        try:
            result = await asyncio.to_thread(run_tool, uid, name, args or {})
        except data.InsightError as e:
            return f"查询失败：{e}"
        except Exception as e:  # noqa: BLE001
            logger.exception("问答助手工具 %s 失败", name)
            return f"查询失败：{type(e).__name__}"
        return json.dumps(result, ensure_ascii=False, default=str)[:12000]

    user = _history_text(history or []) + f"现在的问题：{question[:2000]}"
    out = await run_llm_tools(client, model=model, system=SYSTEM, user=user, params=params, timeout_s=TIMEOUT_S,
                              tools=TOOLS, execute=execute, parse=_parse, max_calls=MAX_TOOL_CALLS)
    trace = [{"tool": t["tool"], "args": t["args"]} for t in (out.trace or []) if t["tool"] != SUBMIT]
    return {"status": out.status, "answer": out.parsed if out.status == "ok" else "", "error": out.error,
            "trace": trace, "model": out.model or model, "tokens_in": out.tokens_in, "tokens_out": out.tokens_out,
            "seconds": round((out.latency_ms or 0) / 1000, 1)}
