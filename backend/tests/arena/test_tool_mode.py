"""工具模式（v1.0 子阶段 B）：工具调用循环、协议转换、完整决策轮次与防前视。"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from app.agent.parse import parse_decision
from app.agent.runner import run_llm_tools
from app.arena import cycle, deps
from app.arena.lifecycle import create_track, enroll, start_track
from app.db.session import get_sessionmaker
from app.llm import ChatResponse, ToolCall, ToolSpec
from app.llm.anthropic_client import _to_anthropic
from app.llm.openai_compat import _to_openai
from app.market import fundamentals as fmod
from app.models import AgentRow, DecisionRow, OrderRow, RoundRow, RuleSetRow
from tests.arena.conftest import cst
from tests.arena.test_extras import FakeFundamentals

TARGETS = {"analysis": "买入茅台", "targets": [{"symbol": "600519", "weight": 0.2, "reason": "龙头"}]}


class ToolLLM:
    """按脚本依次返回：工具调用（name, args）或文本。记录每次收到的请求。"""

    def __init__(self, script: list):
        self.script = script
        self.requests = []

    async def chat(self, req):
        self.requests.append(req)
        step = self.script[min(len(self.requests) - 1, len(self.script) - 1)]
        if isinstance(step, str):
            return ChatResponse(content=step, tokens_in=100, tokens_out=10)
        calls = [ToolCall(f"c{len(self.requests)}_{i}", name, args) for i, (name, args) in enumerate(step)]
        return ChatResponse(content="", tool_calls=calls, tokens_in=100, tokens_out=10)


async def _run(llm, max_calls=3, results=None):
    seen = []

    async def execute(name, args):
        seen.append((name, args))
        return (results or {}).get(name, f"{name} 的结果")

    out = await run_llm_tools(llm, model="m", system="s", user="u", params={}, timeout_s=60,
                              tools=[ToolSpec("get_quote", "", {}), ToolSpec("submit_decision", "", {})],
                              execute=execute, parse=lambda t: parse_decision(t, "targets"), max_calls=max_calls)
    return out, seen


async def test_tool_loop_corrects_bad_submit():
    llm = ToolLLM([[("get_quote", {"symbols": ["600519"]})],
                   [("submit_decision", {"analysis": "x"})],                       # 缺少 targets
                   [("submit_decision", TARGETS)]])
    out, seen = await _run(llm)
    assert out.status == "ok" and out.parsed.targets == {"600519.SH": Decimal("0.2")}
    assert seen == [("get_quote", {"symbols": ["600519"]})]
    assert [t["tool"] for t in out.trace] == ["get_quote", "submit_decision", "submit_decision"]
    assert "提交失败" in out.trace[1]["result"]
    assert (out.attempts, out.tokens_in) == (3, 300)
    # 第三次请求中包含前面的工具结果与错误提示
    tool_msgs = [m for m in llm.requests[2].messages if m["role"] == "tool"]
    assert len(tool_msgs) == 2 and "缺少 targets" in tool_msgs[1]["content"]


async def test_tool_limit_forces_submit():
    llm = ToolLLM([[("get_quote", {"symbols": [str(i)]})] for i in range(5)] + [[("submit_decision", TARGETS)]])
    out, seen = await _run(llm, max_calls=2)
    assert out.status == "ok" and len(seen) == 2
    # 达到上限后只提供 submit_decision，并提示立即提交
    assert [t.name for t in llm.requests[2].tools] == ["submit_decision"]
    assert any("上限" in str(m.get("content")) for m in llm.requests[2].messages)


async def test_text_json_fallback_and_nudge():
    out, _ = await _run(ToolLLM([json.dumps(TARGETS)]))
    assert out.status == "ok"
    out, _ = await _run(ToolLLM(["我再想想", "还是不确定"]))
    assert out.status == "invalid" and "submit_decision" in out.error


def test_message_conversion():
    call = ToolCall("t1", "get_quote", {"symbols": ["A"]})
    msgs = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "", "tool_calls": [call, call]},
            {"role": "tool", "tool_call_id": "t1", "name": "get_quote", "content": "r1"},
            {"role": "tool", "tool_call_id": "t1", "name": "get_quote", "content": "r2"}]
    a = _to_anthropic(msgs)
    assert [m["role"] for m in a] == ["user", "assistant", "user"]
    assert [b["type"] for b in a[2]["content"]] == ["tool_result", "tool_result"]
    assert a[1]["content"][0] == {"type": "tool_use", "id": "t1", "name": "get_quote", "input": {"symbols": ["A"]}}
    o = _to_openai(msgs[1])
    assert o["tool_calls"][0]["function"] == {"name": "get_quote", "arguments": '{"symbols": ["A"]}'}
    assert _to_openai(msgs[2]) == {"role": "tool", "tool_call_id": "t1", "content": "r1"}


async def test_tool_mode_round_end_to_end(arena, monkeypatch):
    fund = FakeFundamentals()
    monkeypatch.setattr(deps, "fundamentals_provider", lambda: fund)
    fmod.FundamentalsService._fetched.clear()
    llm = ToolLLM([[("get_indicators", {"symbol": "600519"}), ("get_bars", {"symbol": "600519", "days": 5})],
                   [("get_fundamentals", {"symbol": "600519.SH"}), ("get_news", {"symbol": "600519"}),
                    ("get_positions", {}), ("get_quote", {"symbols": ["999999"]})],
                   [("submit_decision", TARGETS)]])
    monkeypatch.setattr(cycle, "create_client", lambda cfg: llm)
    with get_sessionmaker()() as s:
        rs = s.scalars(select(RuleSetRow).where(RuleSetRow.name == "cn_long_only")).one()
        t = create_track(s, 1, name="工具", universe_name="cn_sse50_etf", rule_set_id=rs.id,
                         initial_cash=Decimal("1000000"), decision_mode="tools", max_tool_calls=8,
                         mode="replay", replay_start=date(2026, 9, 1), end_date=date(2026, 9, 2))
        acc = enroll(s, t, s.scalars(select(AgentRow).where(AgentRow.kind == "llm")).one().id)
        start_track(s, t, cst(2026, 10, 2, 10))
        s.commit()
        track_id, acc_id = t.id, acc.id
    await cycle.run_track_cycle(track_id, trigger="replay", now=cst(2026, 9, 1, 16))

    req = llm.requests[0]
    assert {t.name for t in req.tools} >= {"get_bars", "submit_decision"}
    assert "submit_decision" in req.system and "MA5" not in req.messages[0]["content"]   # 只给股票池列表
    with get_sessionmaker()() as s:
        dec = s.scalars(select(DecisionRow).join(RoundRow, DecisionRow.round_id == RoundRow.id)
                        .where(RoundRow.track_id == track_id)).one()
        assert dec.status == "ok" and dec.tool_trace[-1]["tool"] == "submit_decision"
        trace = {t["tool"]: t["result"] for t in dec.tool_trace}
        assert trace["get_bars"].splitlines()[-1].startswith("2026-09-01")               # 不晚于决策日期
        assert "2026-06-30" in trace["get_fundamentals"] and "2026-09-30" not in trace["get_fundamentals"]
        assert "历史回放中不提供新闻" in trace["get_news"]
        assert "当前空仓" in trace["get_positions"] and "无法识别的代码" in trace["get_quote"]
        assert "RSI14" in trace["get_indicators"]
        order = s.scalars(select(OrderRow).where(OrderRow.account_id == acc_id)).one()
        assert order.symbol == "600519.SH" and order.side == "buy"
