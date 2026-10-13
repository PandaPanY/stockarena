"""自选股监控：增删改与权重校验、解析 AI 结果、只把新资料交给 AI、事件入库与推送、失败时下次重试。"""

from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace

import pytest

from app.watch import service as watch


@pytest.fixture
def db(settings, monkeypatch):
    from app.db.migrate import upgrade_to_head
    from app.db.session import get_engine, get_sessionmaker
    from app.market.types import Instrument

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    upgrade_to_head(settings.resolved_database_url)
    get_engine()
    monkeypatch.setattr(watch.sv, "resolve", lambda s, sym: Instrument(
        symbol=sym, market=watch.sv.market_of(sym), name="测试股份", type="stock", board="main", lot_size=100,
        tick=0.01))
    monkeypatch.setattr(watch.sv, "quotes", lambda syms: {x: {"name": "测试股份", "price": 10.0, "prev_close": 10.0,
                                                               "change_pct": 0.0, "time": "20261008150000"}
                                                           for x in syms})
    with get_sessionmaker()() as s:
        yield s


def test_add_update_and_limits(db):
    row = watch.add(db, "600519.sh", 1.5, "长期关注")
    assert row.symbol == "600519.SH" and row.weight == 1.5 and row.market == "CN"
    with pytest.raises(watch.WatchError, match="已在"):
        watch.add(db, "600519.SH")
    with pytest.raises(watch.WatchError, match="权重"):
        watch.add(db, "000001.SZ", 5)
    with pytest.raises(watch.WatchError, match="指数"):
        watch.add(db, "000300.SH")
    watch.update(db, row, weight=0, monitor=False)
    assert row.weight == 0 and not row.monitor
    assert watch.weights(db) == {"600519.SH": 0.0}


def test_parse_events():
    raw = ('好的：{"events": [{"level": 5, "direction": "BAD", "title": "立案调查", "summary": "证监会立案",'
           ' "sources": [1, "2", "x"]}, {"level": 1, "title": ""}, {"level": "x", "direction": "?", "title": "回购"}]}')
    evs = watch._parse(raw)
    assert [(e["level"], e["direction"], e["title"], e["sources"]) for e in evs] == [
        (3, "bad", "立案调查", [1, 2]), (1, "neutral", "回购", [])]
    from app.agent.parse import DecisionParseError

    with pytest.raises(DecisionParseError):
        watch._parse("没有 JSON")


def test_needs_ai_and_material_text(db):
    row = watch.add(db, "600519.SH")
    assert not watch.needs_ai({"materials": [], "move": {"day": 0.02}})
    assert watch.needs_ai({"materials": [], "move": {"day": -0.06}})            # 当天大跌也要看看原因
    info = {"materials": [{"kind": "公告", "date": "2026-10-08", "title": "关于股东减持的公告", "url": "u1"}],
            "move": {"day": -0.031, "time": "20261008150000", "five_day": 0.02}}
    text = watch.material_text(row, info)
    assert "[1] 公告 2026-10-08 关于股东减持的公告" in text and "今日涨跌 -3.10%" in text


def test_check_saves_events_marks_seen_and_notifies(db, monkeypatch):
    row = watch.add(db, "600519.SH")
    db.commit()
    mats = [{"kind": "公告", "date": "2026-10-08", "title": "业绩预告：净利润下降 60%", "url": "u1", "key": "u1"},
            {"kind": "新闻", "date": "2026-10-08", "title": "无关新闻", "url": "u2", "key": "u2"}]
    seen_by_ai: list[str] = []

    def fake_collect(r, today):
        seen = set((r.state or {}).get("seen", []))
        new = [m for m in mats if m["key"] not in seen]
        return {"materials": new, "all_keys": [m["key"] for m in mats], "errors": [], "move": {"day": 0.0}}

    async def fake_analyze(text, client, model, params, label, use_skills=True):
        seen_by_ai.append(text)
        return [{"level": 2, "direction": "bad", "title": "业绩大幅下滑", "summary": "预计净利润下降 60%",
                 "impact": "影响较大", "sources": [1]}], {"tokens_in": 10, "tokens_out": 5, "skill_calls": []}

    sent = []
    monkeypatch.setattr(watch, "collect", fake_collect)
    monkeypatch.setattr(watch, "analyze", fake_analyze)
    monkeypatch.setattr(watch, "_llm", lambda s, uid=1: (None, "qwen", {}, None))
    monkeypatch.setattr("app.notify.service.deliver", lambda msg: sent.append(msg) or [{"status": "ok"}])

    res = asyncio.run(watch.check())
    assert res == {"checked": 1, "skipped": 0, "events": 1, "errors": []}
    from app.models.watch import WatchEventRow, WatchItemRow

    db.expire_all()
    ev = db.query(WatchEventRow).one()
    assert ev.title == "业绩大幅下滑" and ev.sources[0]["url"] == "u1" and ev.notified
    assert sent and sent[0].event == "watch" and "业绩大幅下滑" in sent[0].title
    assert set(db.get(WatchItemRow, row.id).state["seen"]) == {"u1", "u2"}
    # 第二次：没有新资料、走势平稳 → 不调用 AI
    res = asyncio.run(watch.check())
    assert res["checked"] == 0 and res["skipped"] == 1 and len(seen_by_ai) == 1


def test_check_failure_keeps_materials_unseen(db, monkeypatch):
    row = watch.add(db, "600519.SH")
    db.commit()
    monkeypatch.setattr(watch, "collect", lambda r, today: {
        "materials": [{"kind": "新闻", "date": "2026-10-08", "title": "t", "url": "u", "key": "u"}],
        "all_keys": ["u"], "errors": [], "move": {}})

    async def failing(*a, **k):
        return [], {"tokens_in": 0, "tokens_out": 0, "skill_calls": [], "error": "超时"}

    monkeypatch.setattr(watch, "analyze", failing)
    monkeypatch.setattr(watch, "_llm", lambda s, uid=1: (None, "qwen", {}, None))
    res = asyncio.run(watch.check())
    assert res["errors"] == ["测试股份：超时"]
    from app.models.watch import WatchItemRow

    db.expire_all()
    r = db.get(WatchItemRow, row.id)
    assert "seen" not in (r.state or {}) and r.last_result["error"] == "超时"     # 下次重试


def test_settings_default_provider_and_validation(db):
    cfg = watch.settings(db)
    assert cfg["enabled"] and cfg["push_level"] == 2
    assert watch.set_settings(db, push_level=3)["push_level"] == 3
    with pytest.raises(watch.WatchError):
        watch.set_settings(db, push_level=5)


def test_notify_message_text():
    e = SimpleNamespace(id=1, level=3, direction="bad", symbol="600519.SH", title="立案调查", summary="证监会立案",
                        impact="可能被处罚",
                        sources=[{"kind": "公告", "date": "2026-10-08", "title": "关于收到立案告知书"}],
                        date=date(2026, 10, 8))
    msg = watch.notify_message(e, "贵州茅台")
    assert msg.event == "watch" and msg.key == "watch-event:1"
    assert "重大·利空" in msg.body and "不构成投资建议" in msg.body
