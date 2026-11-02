"""AI 洞察：跟踪账户的数据整理、每周点评与偏差分析（保存、去重、推送）、问答助手（工具调用、账户隔离）、接口。"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.arena import cycle
from app.arena.lifecycle import create_track, enroll, start_track
from app.db.session import get_sessionmaker
from app.insight import assistant, data, llm, notes
from app.llm.base import ChatResponse, ToolCall
from app.models import AgentRow, LLMProviderRow, RuleSetRow
from app.models.insight import AiNoteRow
from app.models.quant import QuantPortfolioRow
from tests.arena.conftest import cst
from tests.quant.test_portfolio import SPEC_A, SPEC_B, _strategy


class ScriptedLLM:
    """按顺序返回预设的回复；记录收到的请求。"""

    def __init__(self, replies: list[ChatResponse]):
        self.replies, self.requests = list(replies), []

    async def chat(self, req):
        self.requests.append(req)
        return self.replies.pop(0) if self.replies else ChatResponse(content="（没有更多回复）")


@pytest.fixture
async def tracked(arena):  # noqa: F811
    """一个跟踪中的多风格组合（回放到 2026-10 初，有决策、成交和净值）+ 一个模型供应商。"""
    with get_sessionmaker()() as s:
        a, b = _strategy(s, "甲", SPEC_A), _strategy(s, "乙", SPEC_B)
        pf = QuantPortfolioRow(name="洞察组合", members=[{"strategy_id": a, "weight": 0.5},
                                                     {"strategy_id": b, "weight": 0.5}])
        s.add(pf)
        s.flush()
        agent = AgentRow(name="组合·洞察", kind="quant", quant_portfolio_id=pf.id)
        s.add(agent)
        rs = s.scalars(select(RuleSetRow).where(RuleSetRow.name == "cn_long_only")).one()
        track = create_track(s, 1, name="洞察跟踪", universe_name="cn_sse50_etf", rule_set_id=rs.id,
                             initial_cash=Decimal("1000000"), mode="replay", replay_start=date(2026, 9, 1))
        s.flush()
        acc = enroll(s, track, agent.id)
        start_track(s, track, cst(2026, 10, 2, 10))
        pf.follow_account_id = acc.id
        prov = LLMProviderRow(name="测试模型", protocol="openai_compatible", base_url="https://api.example.com/v1",
                              default_model="m", user_id=1)
        s.add(prov)
        s.commit()
        track_id, acc_id, pf_id, pid = track.id, acc.id, pf.id, prov.id
    await cycle.run_replay(track_id, lambda m: None)
    with get_sessionmaker()() as s:
        llm.set_settings(s, 1, provider_id=pid)
        s.commit()
    return {"account_id": acc_id, "portfolio_id": pf_id, "provider_id": pid}


def _use(monkeypatch, fake):
    monkeypatch.setattr(llm, "client_for", lambda s, uid=1: (fake, "m", {}))


async def test_data_for_tracked_portfolio(tracked):
    acc = tracked["account_id"]
    with get_sessionmaker()() as s:
        refs = data.followed(s, 1)
        assert [(r["kind"], r["name"], r["account_id"]) for r in refs] == [("portfolio", "洞察组合", acc)]
        assert data.followed(s, 2) == []
        with pytest.raises(data.InsightError):
            data.owned_account(s, 2, acc)                                   # 别人的账户查不到
        perf = data.performance(s, acc, 5)
        assert perf["days"] == 5 and perf["return"] is not None and perf["benchmark_return"] is not None
        whole = data.performance(s, acc)
        assert whole["start"] == whole["tracking_start"] and whole["days"] > 5
        hold = data.holdings(s, acc)
        assert hold["count"] > 0 and any(p["chosen_by"] for p in hold["positions"])     # 待卖出的没有成员选中
        assert abs(sum(p["weight"] for p in hold["positions"]) + hold["cash_weight"] - 1) < 0.02
        start, end = date.fromisoformat(whole["start"]), date.fromisoformat(whole["end"])
        tr = data.trades(s, acc, start - timedelta(days=1), end)
        assert tr["count"] > 0 and tr["buy_amount"] > 0
        contrib = data.contributions(s, acc, start, end)
        assert contrib["top"] or contrib["bottom"]
        # 各标的贡献之和 ≈ 期间收益（不含分红与手续费，允许少量误差）
        full = data.contributions(s, acc, start, end, top=100)
        total = sum(x["contribution"] for x in full["top"] + full["bottom"])
        assert abs(total - whole["return"]) < 0.01
        sym = hold["positions"][0]["symbol"]
        ex = data.explain_symbol(s, acc, sym)
        assert ex["held_qty"] > 0 and any(m["verdict"] == "入选" and "名" in m["detail"] for m in ex["members"])


async def test_weekly_note_saved_pushed_once(tracked, monkeypatch):
    fake = ScriptedLLM([ChatResponse(content="**本周表现**：小幅上涨。", tokens_in=100, tokens_out=20)])
    _use(monkeypatch, fake)
    pushed = []
    from app.notify import service as notify

    monkeypatch.setattr(notify, "deliver", lambda msg: pushed.append(msg))
    out = await notes.run_weekly(1)
    assert out["done"] == ["洞察组合"] and not out["errors"]
    assert "请为「洞察组合」写本周点评" in fake.requests[0].messages[0]["content"]
    assert "不给买卖建议" in fake.requests[0].system
    with get_sessionmaker()() as s:
        n = s.scalars(select(AiNoteRow)).one()
        assert n.kind == "weekly" and n.status == "ok" and n.content.startswith("**本周表现**")
        assert n.material["period"]["days"] == 5 and n.material["members"]
    assert pushed and pushed[0].event == "insight" and pushed[0].title.startswith("【每周点评】洞察组合")
    assert (await notes.run_weekly(1))["skipped"] == ["洞察组合"]       # 同一周期只写一次


async def test_deviation_only_when_health_alert(tracked, monkeypatch):
    fake = ScriptedLLM([ChatResponse(content="**可能的原因**：同期基准也在跌。")])
    _use(monkeypatch, fake)
    from app.notify import service as notify

    pushed = []
    monkeypatch.setattr(notify, "deliver", lambda msg: pushed.append(msg))
    assert (await notes.run_deviation(1))["done"] == []                  # 健康度正常：不分析
    with get_sessionmaker()() as s:
        pf = s.get(QuantPortfolioRow, tracked["portfolio_id"])
        pf.readiness = {"status": "待观察", "health": {"status": "提醒", "message": "明显偏弱", "checks": []}}
        s.commit()
    assert (await notes.run_deviation(1))["done"] == ["洞察组合"]
    assert "健康度为「提醒」" in fake.requests[0].messages[0]["content"]
    assert pushed[0].event == "health" and "【AI 分析】" in pushed[0].title
    assert (await notes.run_deviation(1))["done"] == []                  # 一周内不重复


async def test_model_failure_saved_not_pushed(tracked, monkeypatch):
    from app.llm.base import LLMError
    from app.notify import service as notify

    class Down:
        async def chat(self, req):
            raise LLMError("无法连接模型服务")

    _use(monkeypatch, Down())
    pushed = []
    monkeypatch.setattr(notify, "deliver", lambda msg: pushed.append(msg))
    await notes.run_weekly(1)
    with get_sessionmaker()() as s:
        n = s.scalars(select(AiNoteRow)).one()
        assert n.status == "error" and "无法连接" in n.error
    assert not pushed


async def test_assistant_uses_tools_and_isolation(tracked, monkeypatch):
    acc = tracked["account_id"]
    fake = ScriptedLLM([
        ChatResponse(content="", tool_calls=[ToolCall("1", "list_followed", {})]),
        ChatResponse(content="", tool_calls=[ToolCall("2", "get_holdings", {"account_id": acc}),
                                             ToolCall("3", "get_holdings", {"account_id": 99999})]),
        ChatResponse(content="", tool_calls=[ToolCall("4", "submit_decision", {"answer": "持有 3 只股票。"})]),
    ])
    _use(monkeypatch, fake)
    out = await assistant.ask(1, "我的组合现在持有什么？", [{"role": "user", "content": "你好"},
                                                         {"role": "assistant", "content": "你好！"}])
    assert out["status"] == "ok" and out["answer"] == "持有 3 只股票。"
    assert [t["tool"] for t in out["trace"]] == ["list_followed", "get_holdings", "get_holdings"]
    assert "之前的对话" in fake.requests[0].messages[0]["content"]
    tool_results = [m["content"] for m in fake.requests[2].messages if m["role"] == "tool"]
    assert '"洞察组合"' in tool_results[0] and '"positions"' in tool_results[1]
    assert "不是你正在跟踪" in tool_results[2]                                # 别人的账户查不到

    fake = ScriptedLLM([ChatResponse(content="我只能解释策略和数据，不能给买卖建议。")])       # 不调用工具直接回答
    _use(monkeypatch, fake)
    out = await assistant.ask(1, "明天该买什么？")
    assert out["status"] == "ok" and "不能给买卖建议" in out["answer"]
    with pytest.raises(data.InsightError):
        await assistant.ask(1, "   ")


def test_settings_and_api(client, monkeypatch):
    with get_sessionmaker()() as s:
        assert llm.settings(s, 1)["provider_id"] is None
        with pytest.raises(data.InsightError):
            llm.client_for(s, 1)
    body = client.get("/api/insight").json()
    assert body["followed"] == [] and body["settings"]["weekly"] is True
    r = client.post("/api/insight/notes/run", json={"kind": "weekly"})
    assert r.status_code == 400 and "模型" in r.json()["detail"]
    r = client.put("/api/insight/settings", json={"weekly": False, "provider_id": 12345})
    assert r.status_code == 400
    r = client.put("/api/insight/settings", json={"weekly": False})
    assert r.status_code == 200 and r.json()["weekly"] is False
    r = client.post("/api/insight/ask", json={"question": "我的持仓？"})
    assert r.status_code == 400 and "模型" in r.json()["detail"]


from tests.arena.conftest import arena  # noqa: E402, F401
