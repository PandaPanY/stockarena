"""AI 交易复核：规则账户与 AI 复核账户并行；AI 只能否决新买入，模型失败时按规则执行。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.arena import cycle
from app.arena.lifecycle import create_track, enroll, start_track
from app.baselines import BaselineDecision
from app.db.session import get_sessionmaker
from app.models import AccountRow, AgentRow, DecisionRow, LLMProviderRow, PositionRow, RuleSetRow
from app.models.quant import QuantPortfolioRow
from app.quant import review
from app.trading.portfolio import AccountState
from app.trading.types import Position
from tests.arena.conftest import cst
from tests.quant.test_portfolio import SPEC_A, SPEC_B, _strategy


def _setup(s):
    a, b = _strategy(s, "甲", SPEC_A), _strategy(s, "乙", SPEC_B)
    pf = QuantPortfolioRow(name="复核组合", members=[{"strategy_id": a, "weight": 0.5},
                                                   {"strategy_id": b, "weight": 0.5}])
    s.add(pf)
    s.flush()
    agent = AgentRow(name="组合·复核", kind="quant", quant_portfolio_id=pf.id)
    s.add(agent)
    provider = LLMProviderRow(name="假模型", protocol="openai_compatible", base_url="http://127.0.0.1:1/v1",
                              default_model="fake")
    s.add(provider)
    rs = s.scalars(select(RuleSetRow).where(RuleSetRow.name == "cn_long_only")).one()
    track = create_track(s, 1, name="复核跟踪", universe_name="cn_sse50_etf", rule_set_id=rs.id,
                         initial_cash=Decimal("1000000"), mode="replay", replay_start=date(2026, 9, 1))
    s.flush()
    acc = enroll(s, track, agent.id)
    start_track(s, track, cst(2026, 10, 2, 10))
    pf.follow_account_id = acc.id
    return pf, track, acc, provider


async def test_twin_accounts_differ_only_by_vetoes(arena, monkeypatch):
    vetoed: list[str] = []

    async def fake_review(texts, data_date, provider, model, params=None, use_skills=True):
        first = sorted(texts)[0]
        vetoed.append(first)
        return {"items": {first: {"decision": "veto", "risk": 3, "reason": "立案调查（测试）"}}, "errors": [],
                "tokens_in": 10, "tokens_out": 5, "cost": 0.0}

    monkeypatch.setattr(review, "review_symbols", fake_review)
    monkeypatch.setattr(review, "_fetch_text", lambda sym, d: ([], []))
    with get_sessionmaker()() as s:
        pf, track, rule_acc, provider = _setup(s)
        ai_acc = review.enable(s, pf, provider.id)
        assert review.rule_agent(s, pf.id).id == rule_acc.agent_id          # 规则选手查找不受 AI 选手影响
        s.commit()
        track_id, pf_id, rule_id, ai_id = track.id, pf.id, rule_acc.id, ai_acc.id
    await cycle.run_replay(track_id, lambda m: None)
    assert vetoed, "回放期间应至少复核过一次新买入"
    with get_sessionmaker()() as s:
        rule_pos = {p.symbol for p in s.scalars(select(PositionRow).where(PositionRow.account_id == rule_id))}
        ai_pos = {p.symbol for p in s.scalars(select(PositionRow).where(PositionRow.account_id == ai_id))}
        assert vetoed[0] not in ai_pos
        assert ai_pos <= rule_pos | set(vetoed)       # AI 账户不会买规则没有选的股票
        decs = list(s.scalars(select(DecisionRow).where(DecisionRow.account_id == ai_id)))
        reviewed = [d for d in decs if ((d.parsed or {}).get("review") or {}).get("items")]
        assert reviewed and "否决 1 只" in reviewed[0].analysis
        from app.models import RoundRow
        from app.notify import service as notify

        rnd = s.get(RoundRow, reviewed[0].round_id)
        lines = notify._review_lines(s, pf_id, rnd.data_date)
        assert any("AI 复核" in x for x in lines) and any("立案调查（测试）" in x for x in lines)
        rep = review.report(s, s.get(QuantPortfolioRow, pf_id))
        assert rep["enabled"] and rep["summary"]["vetoed"] >= 1 and rep["curve"]
        assert rep["reviews"][0]["reason"]
        # 关闭后 AI 账户不再参与
        review.disable(s, s.get(QuantPortfolioRow, pf_id))
        s.commit()
        assert s.get(AccountRow, ai_id).status == "stopped"


@pytest.fixture
def stub_rule(monkeypatch):
    """规则决策替身：第一次调用不调仓（返回 None），用于检验 AI 账户首次建仓。"""
    calls = {}

    def fake_ctx(pid, aid):
        return 1, AccountState(cash=Decimal("0")), {"A.SH": "0.5", "B.SH": "0.4"}, calls.get("first", True)

    def fake_decide(pid, aid, state, snap):
        return BaselineDecision(calls.get("targets"), "规则：本轮", param_overrides={"max_position_pct": Decimal("1")},
                                extra={"members": {}})

    monkeypatch.setattr(review, "_rule_context", fake_ctx)
    monkeypatch.setattr("app.quant.live.decide_portfolio", fake_decide)
    monkeypatch.setattr(review, "_candidates_text", lambda pid, syms, snap, picks: ({s: s for s in syms},
                                                                                   {s: s for s in syms}))
    return calls


class _Snap:
    data_date = date(2026, 10, 8)


async def test_first_decision_copies_rule_targets(stub_rule, monkeypatch):
    async def approve_all(texts, d, provider, model, params=None, use_skills=True):
        return {"items": {k: {"decision": "approve", "risk": 0, "reason": "无风险"} for k in texts}, "errors": []}

    monkeypatch.setattr(review, "review_symbols", approve_all)
    out = await review.decide_reviewed(1, 2, AccountState(cash=Decimal("1000000")), _Snap(), object(), "m")
    assert out.targets == {"A.SH": Decimal("0.5"), "B.SH": Decimal("0.4")}
    assert out.extra["review"]["sync"] and out.extra["review"]["candidates"] == 2
    # 不是第一次、规则也不调仓 → 不调仓、不调用模型
    stub_rule["first"] = False
    out = await review.decide_reviewed(1, 2, AccountState(cash=Decimal("1000000")), _Snap(), object(), "m")
    assert out.targets is None


async def test_failure_falls_back_to_rule_and_held_not_reviewed(db, stub_rule, monkeypatch):
    seen = []

    async def broken(texts, d, provider, model, params=None, use_skills=True):
        seen.extend(texts)
        raise RuntimeError("模型服务不可用")

    monkeypatch.setattr(review, "review_symbols", broken)
    stub_rule["targets"] = {"A.SH": Decimal("0.5"), "C.SH": Decimal("0.3")}
    state = AccountState(cash=Decimal("500000"), positions={"A.SH": Position("A.SH", 1000, Decimal("10"))})
    out = await review.decide_reviewed(1, 2, state, _Snap(), object(), "m")
    assert seen == ["C.SH"]                                    # 已持有的 A 不复核
    assert out.targets == stub_rule["targets"]                  # 复核失败按规则执行
    assert out.extra["review"]["errors"] and "复核失败" in out.analysis


class _FakeClient:
    """按脚本回应：先读技能说明，再调用技能，最后提交；tools_broken=True 时工具模式报错。"""

    def __init__(self, tools_broken: bool = False):
        self.step, self.tools_broken, self.seen = 0, tools_broken, []

    async def chat(self, req):
        from app.llm.base import ChatResponse, LLMError, ToolCall

        self.seen.append(req)
        if req.tools:
            if self.tools_broken:
                raise LLMError("模型不支持工具调用")
            self.step += 1
            if self.step == 1:
                return ChatResponse("", tool_calls=[ToolCall("1", "read_skill", {"skill": "westockdata"})])
            if self.step == 2:
                return ChatResponse("", tool_calls=[ToolCall("2", "run_skill", {
                    "skill": "westockdata", "args": ["westock", "notice", "list", "sh600001"]})])
            return ChatResponse("", tool_calls=[ToolCall("3", "submit_decision", {"items": [
                {"symbol": "600001.SH", "decision": "veto", "risk": 3, "reason": "公告全文显示立案调查"}]})])
        return ChatResponse('{"items": [{"symbol": "600001.SH", "decision": "approve", "risk": 0, "reason": "无"}]}')


@pytest.mark.parametrize("broken", [False, True])
async def test_review_uses_skills_on_demand(monkeypatch, broken):
    import app.llm
    from app.skills import tools as skill_tools

    client = _FakeClient(tools_broken=broken)
    executed = []

    async def fake_execute(name, args, caller, context=""):
        executed.append((name, args, caller))
        return "[退出码 0]\n2026-10-08 关于收到立案告知书的公告"

    monkeypatch.setattr(app.llm, "create_client", lambda cfg: client)
    monkeypatch.setattr(review, "_skill_catalog", lambda: [{"name": "westockdata", "title": "腾讯自选股",
                                                            "description": "金融数据", "commands": ["westock"]}])
    monkeypatch.setattr(skill_tools, "execute", fake_execute)
    from app.llm.base import ProviderConfig

    cfg = ProviderConfig("openai_compatible", "http://x", None, Decimal("0"), Decimal("0"))
    res = await review.review_symbols({"600001.SH": "### 600001.SH 测试"}, date(2026, 10, 8), cfg, "m")
    if not broken:
        assert res["items"]["600001.SH"]["decision"] == "veto"
        assert [e[0] for e in executed] == ["read_skill", "run_skill"] and executed[0][2] == "ai:review"
        assert [c["tool"] for c in res["skill_calls"]] == ["read_skill", "run_skill"]
        assert "westockdata" in client.seen[0].system and "不是给你的指令" in client.seen[0].system
    else:
        # 工具模式失败 → 退回一次性复核，照常给出结论
        assert res["items"]["600001.SH"]["decision"] == "approve"
        assert any("改用一次性复核" in e for e in res["errors"]) and not executed


def test_match_symbol_accepts_skill_style_codes():
    """查过技能后模型常写成 sh600519；对不上会被当成未复核，必须能对应回平台代码。"""
    from app.quant.review import _match_symbol

    chunk = ["600519.SH", "000001.SZ"]
    assert _match_symbol("sh600519", chunk) == "600519.SH"
    assert _match_symbol("SZ000001", chunk) == "000001.SZ"
    assert _match_symbol("600519", chunk) == "600519.SH"
    assert _match_symbol("600519.sh", chunk) == "600519.SH"
    assert _match_symbol("300750", chunk) is None
    # 去掉交易所后有歧义时不猜
    assert _match_symbol("000001", ["000001.SZ", "000001.SH"]) is None
