"""策略工坊流程（PRD 3.8）：创建策略 → AI 研究轮 → 检验与升级 → 接口。行情使用合成数据，AI 为伪造。"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import numpy as np
import pytest
from sqlalchemy import select

from app.db.migrate import upgrade_to_head
from app.db.session import get_sessionmaker
from app.llm import ChatResponse
from app.market.types import Instrument
from app.models import LLMProviderRow
from app.models.quant import QuantStrategyRow, QuantVersionRow, ResearchRunRow
from app.quant import research, service
from tests.quant.test_backtest import make_panel

SETTINGS = {"train_start": "2019-01-01", "train_end": "2020-06-30", "valid_start": "2020-07-01",
            "valid_end": "2021-06-30", "holdout_start": "2021-07-01", "rounds": 1, "candidates": 3,
            "min_holdings": 0}


def synthetic_panel():
    T = 1000
    rng = np.random.default_rng(42)

    def walk(drift, vol=0.006):
        return list(10 * np.cumprod(1 + rng.normal(drift, vol, T)))

    closes = {"UP1.SH": walk(0.002), "UP2.SH": walk(0.0018), "FLAT1.SH": walk(0.0), "FLAT2.SH": walk(0.0),
              "DOWN1.SH": walk(-0.0018), "DOWN2.SH": walk(-0.002), "511010.SH": walk(0.0001, 0.001)}
    return make_panel(closes, start="2018-01-01", types={"511010.SH": "etf"},
                      bench=list(1000 * np.cumprod(1 + rng.normal(0.0002, 0.01, T))))


PANEL = synthetic_panel()

# 冠军故意选错（买跌得最多的），候选用动量（买涨得最多的）
BAD = {"universe": {"types": ["stock"], "min_listed_days": 0, "min_amount": 0},
       "factors": [{"name": "momentum", "params": {"window": 60}, "direction": "low"}],
       "selection": {"top_n": 2}, "weighting": {"max_weight": 0.5}, "rebalance": {"every_days": 20}}
GOOD = {**BAD, "factors": [{"name": "momentum", "params": {"window": 60}, "direction": "high"}]}


class FakeResearcher:
    def __init__(self):
        self.prompts: list[str] = []

    async def chat(self, req):
        self.prompts.append(req.messages[-1]["content"])
        content = json.dumps({"analysis": "冠军买入了持续下跌的股票，应改为顺势", "candidates": [
            {"title": "改为顺势动量", "rationale": "动量方向改为越高越好", "spec": GOOD},
            {"title": "重复冠军", "rationale": "不变", "spec": BAD},
            {"title": "无效候选", "rationale": "用了不存在的因子", "spec": {**BAD, "factors": [{"name": "magic"}]}},
        ], "lessons": ["动量方向应为越高越好", "", "合成数据中下跌股持续下跌"]}, ensure_ascii=False)
        return ChatResponse(content=content, tokens_in=5000, tokens_out=800, model="fake")


@pytest.fixture
def lab(settings, monkeypatch):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    upgrade_to_head(settings.resolved_database_url)
    insts = [Instrument(symbol=s, market="CN", name=s, type="etf" if s == "511010.SH" else "stock",
                        board="fund" if s == "511010.SH" else "main") for s in PANEL.symbols]
    bench = Instrument(symbol="000300.SH", market="CN", name="沪深300", type="index", board="index")
    members = lambda name: (insts, bench, "CN")  # noqa: E731
    monkeypatch.setattr(service, "get_panel", lambda *a, **k: PANEL)
    for mod in (service, research):
        monkeypatch.setattr(mod, "panel_for", lambda s, st: PANEL)
        monkeypatch.setattr(mod, "universe_members", members)
    llm = FakeResearcher()
    monkeypatch.setattr(research, "create_client", lambda cfg: llm)
    with get_sessionmaker()() as s:
        prov = LLMProviderRow(name="本地", protocol="openai_compatible", base_url="http://127.0.0.1:1/v1",
                              default_model="fake", price_in_per_m=Decimal("0"), price_out_per_m=Decimal("0"))
        s.add(prov)
        s.commit()
        return llm, prov.id


def _create(spec=BAD, provider_id=None) -> int:
    with get_sessionmaker()() as s:
        st = service.create_strategy(s, name="测试策略", style="momentum", description="顺势而为", spec=spec,
                                     provider_id=provider_id, settings=SETTINGS)
        s.commit()
        return st.id


def test_create_strategy_evaluates_initial_champion(lab):
    sid = _create()
    with get_sessionmaker()() as s:
        st = s.get(QuantStrategyRow, sid)
        v = s.get(QuantVersionRow, st.champion_id)
        assert v.number == 1 and v.status == "champion" and v.source == "initial"
        assert v.train["start"] >= "2019-01-01" and v.valid["end"] <= "2021-06-30"
        assert v.holdout["days"] > 50 and v.robustness["samples"] > 0
        assert v.curves["nav"][0] == 1.0
        assert v.score == v.dev["sharpe"] and v.dev["monthly"] and v.dev["diagnostics"]["worst"]


def test_settings_validation(lab):
    with pytest.raises(service.LabError, match="区间顺序"):
        service.LabSettings.from_dict({**SETTINGS, "valid_start": "2019-06-01"})


async def test_research_round_promotes_better_candidate(lab):
    llm, pid = lab
    sid = _create(provider_id=pid)
    with get_sessionmaker()() as s:
        run = research.start_run(s, s.get(QuantStrategyRow, sid))
        s.commit()
        rid = run.id
        with pytest.raises(service.LabError, match="已有正在进行"):
            research.start_run(s, s.get(QuantStrategyRow, sid))
    msg = await research.run_research(rid)
    assert msg == "完成 1 轮，升级 1 次"
    prompt = llm.prompts[0]
    assert "冠军的开发期报告" in prompt and "分年" in prompt and "momentum" in prompt
    assert "升级条件" in prompt and "2021-12" not in prompt          # 留出区间不出现在提示中
    assert "## 研究笔记" in prompt and "（暂无）" in prompt.split("## 研究笔记")[1].split("##")[0]
    assert "冠军开发期的交易诊断" in prompt and "胜率" in prompt.split("交易诊断")[1]

    with get_sessionmaker()() as s:
        st = s.get(QuantStrategyRow, sid)
        champ = s.get(QuantVersionRow, st.champion_id)
        assert champ.number == 2 and champ.source == "ai" and champ.title == "改为顺势动量"
        assert champ.dev["sharpe"] > 0 and "达标" in champ.decision and "年跑赢冠军" in champ.decision
        assert champ.dev["vs_champion"]["p"] < 0.1 and champ.score == champ.dev["sharpe"]
        old = s.scalars(select(QuantVersionRow).where(QuantVersionRow.number == 1)).one()
        assert old.status == "retired"
        run = s.get(ResearchRunRow, rid)
        entry = run.log[0]
        assert run.status == "done" and run.promoted == 1 and entry["promoted"] == 2
        assert sorted(c["status"] for c in entry["candidates"]) == ["champion", "duplicate"]
        assert "部分候选无效" in entry["analysis"] and "magic" in entry["analysis"]
        assert [n["text"] for n in st.research_notes] == ["动量方向应为越高越好", "合成数据中下跌股持续下跌"]
        assert entry["lessons"] == ["动量方向应为越高越好", "合成数据中下跌股持续下跌"]


def test_parse_research_rejects_all_invalid():
    from app.agent.parse import DecisionParseError

    with pytest.raises(DecisionParseError, match="未知因子"):
        research.parse_research(json.dumps({"candidates": [{"spec": {"factors": [{"name": "x"}]}}]}))


def test_lab_api(lab, client):
    _, pid = lab
    sid = _create(provider_id=pid)
    data = client.get("/api/quant/strategies").json()
    assert data["strategies"][0]["champion"]["number"] == 1
    detail = client.get(f"/api/quant/strategies/{sid}").json()
    assert detail["champion_detail"]["rules"] and detail["version_list"][0]["status"] == "champion"
    assert any(f["name"] == "ep" for f in client.get("/api/quant/factors").json())
    assert {x["style"] for x in client.get("/api/quant/seeds").json()} >= {"value", "momentum", "rotation"}

    r = client.patch(f"/api/quant/strategies/{sid}", json={"settings": {"max_drawdown": 0.3}, "auto_optimize": True})
    assert r.status_code == 200 and r.json()["settings"]["max_drawdown"] == 0.3
    r = client.patch(f"/api/quant/strategies/{sid}", json={"research_notes": ["手动经验", "  "]})
    assert r.status_code == 200
    notes = client.get(f"/api/quant/strategies/{sid}").json()["research_notes"]
    assert [(n["text"], n["source"]) for n in notes] == [("手动经验", "manual")]
    r = client.patch(f"/api/quant/strategies/{sid}", json={"settings": {"train_start": "2019-02-01"}})
    assert r.status_code == 400 and "不能修改" in r.json()["detail"]

    r = client.post(f"/api/quant/strategies/{sid}/versions", json={"spec": GOOD, "title": "手动顺势"})
    assert r.status_code == 200 and r.json()["source"] == "manual" and r.json()["status"] == "candidate"
    vid = r.json()["id"]
    assert client.post(f"/api/quant/strategies/{sid}/versions",
                       json={"spec": {"factors": []}}).status_code == 400
    assert client.post(f"/api/quant/versions/{vid}/promote").json()["status"] == "champion"
    assert client.get(f"/api/quant/strategies/{sid}").json()["champion"]["id"] == vid


def test_seed_file_specs_are_valid():
    from app.quant.spec import parse_spec

    seeds = service.load_seeds()
    assert len(seeds) == 7
    for seed in seeds:
        parse_spec(seed["spec"])
    assert date.fromisoformat(service.LabSettings().to_dict()["train_start"]) == date(2018, 1, 1)


def test_parse_research_repairs_missing_closing_brackets():
    spec = json.dumps(BAD, ensure_ascii=False)
    text = '{"analysis": "诊断", "candidates": [{"title": "a", "rationale": "r", "spec": ' + spec  # 少了 }]}
    out = research.parse_research(text)
    assert out.candidates[0]["title"] == "a" and out.analysis == "诊断"


def test_parse_research_repairs_missing_candidate_brace():
    spec = json.dumps(BAD, ensure_ascii=False)
    # 实测：候选对象少了 }，直接写了 ]（还多一个 ]）
    text = '{"analysis": "诊断", "candidates": [{"title": "a", "rationale": "r", "spec": ' + spec + ']}]'
    out = research.parse_research(text)
    assert out.candidates[0]["title"] == "a" and len(out.candidates) == 1
