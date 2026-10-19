"""复核模型失效：失败的复核不算「放行」；更换模型后旧数据封存，评估从更换后的第一次决策起重新计数。"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.arena.lifecycle import ArenaError
from app.core.timeutils import utcnow
from app.db.session import get_sessionmaker
from app.models import AgentRow, DecisionRow, LLMProviderRow, RoundRow
from app.models.quant import QuantPortfolioRow
from app.quant import review
from app.quant import review_eval as ev
from tests.quant.test_backtest import make_panel
from tests.quant.test_review import _setup


def _decision(s, track_id: int, acc_id: int, d: date, items: list[dict], created=None) -> None:
    rnd = RoundRow(track_id=track_id, data_date=d, fill_date=d + timedelta(days=1), trigger="schedule", status="done")
    s.add(rnd)
    s.flush()
    s.add(DecisionRow(round_id=rnd.id, account_id=acc_id, status="baseline", created_at=created or utcnow(),
                      parsed={"review": {"items": items}}))
    s.flush()


def test_failed_reviews_are_not_approvals():
    assert review.is_failed({"decision": "approve", "reason": "未得到复核结果，按规则执行"})      # 旧记录
    assert review.is_failed({"decision": "approve", "failed": True})
    assert not review.is_failed({"decision": "approve", "reason": "没有重大风险"})


def test_change_model_seals_old_segment(arena):
    with get_sessionmaker()() as s:
        pf, track, rule_acc, provider = _setup(s)
        ai = review.enable(s, pf, provider.id)
        pairs = ((pf.id, rule_acc.id, ai.id),)
        long_ago = utcnow() - timedelta(days=30)
        _decision(s, track.id, ai.id, date(2026, 10, 14),
                  [{"symbol": "600000.SH", "decision": "veto", "risk": 3, "reason": "立案调查"},
                   {"symbol": "600036.SH", "decision": "approve", "risk": 0, "reason": "无重大风险"}], long_ago)
        _decision(s, track.id, ai.id, date(2026, 10, 19),
                  [{"symbol": "600519.SH", "decision": "approve", "risk": None,
                    "reason": "未得到复核结果，按规则执行", "failed": True}], long_ago)
        s.commit()

        # 失败的不计入
        assert ev.collect(s, pairs, date(2026, 10, 19)) == []
        out = ev.report(s, pairs=pairs)
        sealed = out["sealed"][0]
        assert (sealed["events"], sealed["vetoes"], sealed["failed"]) == (2, 1, 0)
        assert sealed["veto_list"][0]["reason"] == "立案调查" and "不下结论" in sealed["verdict"]
        assert out["current"]["waiting"] and out["current"]["missing"] == [pf.name]

        # 更换模型：需要写原因，不能与当前相同
        new = LLMProviderRow(name="新模型", protocol="openai_compatible", base_url="https://api.example.com/v1",
                             default_model="m2", user_id=pf.user_id)
        s.add(new)
        s.flush()
        with pytest.raises(ArenaError):
            review.change_model(s, pf, new.id, "m2", " ")
        with pytest.raises(ArenaError):
            review.change_model(s, pf, provider.id, None, "原模型失效")
        change = review.change_model(s, pf, new.id, "m2", "原模型失效、无法恢复")
        s.commit()
        cfg = review.settings(s.get(QuantPortfolioRow, pf.id))
        agent = s.get(AgentRow, cfg["agent_id"])
        assert agent.provider_id == new.id and agent.model == "m2" and "extra_body" not in agent.params
        assert agent.params.get("ai_review") and cfg["changes"][-1]["to"]["provider_id"] == new.id
        assert change["from"]["provider_id"] == provider.id
        assert ev.report(s, pairs=pairs)["current"]["message"].startswith("已更换复核模型")

        # 更换后的第一次决策成为新一段的第 0 个交易日；旧的一段不并入
        _decision(s, track.id, ai.id, date(2026, 10, 21),
                  [{"symbol": "600000.SH", "decision": "approve", "risk": 0, "reason": "无重大风险"}],
                  utcnow() + timedelta(seconds=5))
        s.commit()
        panel = make_panel({"600000.SH": [10.0] * 60}, start="2026-10-01")
        cur = ev.report(s, pairs=pairs, panel=panel)["current"]
        assert cur["start"] == "2026-10-21" and cur["events"] == 1 and cur["vetoes"] == 0
        assert cur["model"][pf.id]["model"] == "m2" and cur["next_look"] == 120
