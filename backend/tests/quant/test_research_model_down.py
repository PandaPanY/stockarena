"""模型不可用时，AI 优化连续几轮连不上就停止并记为失败，不再显示成「完成 5 轮，升级 0 次」。"""

from __future__ import annotations

from app.db.session import get_sessionmaker
from app.llm import LLMError
from app.models.quant import QuantStrategyRow, ResearchRunRow
from app.quant import research
from tests.quant.test_lab import _create, lab  # noqa: F401


class DownClient:
    calls = 0

    async def chat(self, req):
        DownClient.calls += 1
        raise LLMError("无法连接模型服务：Connection error.")


async def test_run_stops_when_model_is_down(lab, monkeypatch):  # noqa: F811
    _llm, pid = lab
    monkeypatch.setattr(research, "create_client", lambda cfg: DownClient())
    sid = _create(provider_id=pid)
    with get_sessionmaker()() as s:
        run = research.start_run(s, s.get(QuantStrategyRow, sid), rounds=5)
        s.commit()
        rid = run.id
    out = await research.run_research(rid)
    assert "连续 2 轮失败" in out
    with get_sessionmaker()() as s:
        run = s.get(ResearchRunRow, rid)
        assert run.status == "failed" and run.rounds_done == 2 and "Connection error" in run.error
    assert DownClient.calls == 2
