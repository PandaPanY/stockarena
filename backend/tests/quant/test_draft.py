"""一句话创建策略：解析与校验、做不到的条件要列出来、模型属于自己；新建前的试算计入测试次数。"""

from __future__ import annotations

import json

import pytest

from app.agent.parse import DecisionParseError
from app.db.session import get_sessionmaker
from app.llm import ChatResponse
from app.models.quant import QuantStrategyRow
from app.quant import draft, service
from tests.quant.test_lab import SETTINGS, lab  # noqa: F401

SPEC = {"universe": {"types": ["stock"]}, "factors": [{"name": "ep", "direction": "high", "weight": 1}],
        "selection": {"top_n": 20}, "rebalance": {"every_days": 20}}


class FakeDrafter:
    def __init__(self, replies: list[str]):
        self.replies, self.requests = list(replies), []

    async def chat(self, req):
        self.requests.append(req)
        return ChatResponse(content=self.replies.pop(0), tokens_in=3000, tokens_out=400, model="fake")


def _reply(spec=SPEC, unsupported=("分析师评级：因子库里没有评级数据，未实现",)) -> str:
    raw = unsupported if isinstance(unsupported, str) else list(unsupported)
    return json.dumps({"name": "低估值", "description": "买便宜的股票", "spec": spec,
                       "unsupported": raw, "notes": "持有 20 只"}, ensure_ascii=False)


def test_parse_draft_validates_spec_and_keeps_unsupported():
    d = draft.parse_draft(_reply())
    assert d.name == "低估值" and d.spec.selection.top_n == 20
    assert d.unsupported == ["分析师评级：因子库里没有评级数据，未实现"]
    with pytest.raises(DecisionParseError, match="规格不合法"):
        draft.parse_draft(_reply(spec={"factors": [{"name": "不存在的因子"}]}))
    with pytest.raises(DecisionParseError, match="缺少 spec"):
        draft.parse_draft(json.dumps({"name": "x"}))
    assert draft.parse_draft(_reply(unsupported="做空")).unsupported == ["做空"]


def test_prompt_lists_spec_doc_factors_and_base_spec():
    p = draft.build_prompt("低估值高股息", SPEC)
    assert "低估值高股息" in p and "## 策略规格" in p and "dividend_yield" in p and "## 当前规则" in p
    assert "unsupported" in draft.SYSTEM and "不能悄悄丢掉" in draft.SYSTEM


def test_draft_endpoint_retries_invalid_spec_once(lab, client, monkeypatch):  # noqa: F811
    _llm, pid = lab
    fake = FakeDrafter([_reply(spec={"factors": [{"name": "bogus"}]}), _reply()])
    monkeypatch.setattr(draft, "create_client", lambda cfg: fake)
    body = {"text": "买低估值的大盘股，回避分析师下调评级的", "provider_id": pid}
    r = client.post("/api/quant/strategies/draft", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["spec"]["factors"][0]["name"] == "ep" and out["unsupported"] and out["rules"]
    assert len(fake.requests) == 2                       # 第一次规格不合法，带着错误重试
    assert client.post("/api/quant/strategies/draft", json={"text": "x", "provider_id": 99999}).status_code == 400


def test_trials_before_create_count_as_tests(lab):  # noqa: F811
    with get_sessionmaker()() as s:
        st = service.create_strategy(s, name="试算过的", style="custom", description="", spec=SPEC, settings=SETTINGS,
                                     trial_scores=[0.3, None, 0.5])
        s.commit()
        sid, champ = st.id, st.champion_id
    with get_sessionmaker()() as s:
        st = s.get(QuantStrategyRow, sid)
        assert st.origin["kind"] == "new" and st.origin["prior_tests"] == 3
        assert service.prior_tests(s, st, champ) == 3            # 初始版本的升级检验门槛按 3 次收紧
        scores = service.trial_scores(s, st)
        assert 0.3 in scores and 0.5 in scores                       # 试算的夏普也算作尝试（DSR）
