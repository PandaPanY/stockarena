"""消息推送：通道请求格式、接口（密钥只写不读）、每日调仓提醒与异常提醒（去重）。"""

from __future__ import annotations

import json
from datetime import date, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import select

from app.arena import cycle
from app.arena.deps import secret_box
from app.arena.lifecycle import create_track, enroll, start_track
from app.db.session import get_sessionmaker
from app.models import AgentRow, RoundRow, RuleSetRow
from app.models.notify import NotificationRow, NotifyChannelRow
from app.models.quant import QuantPortfolioRow
from app.notify import channels, service
from tests.arena.conftest import cst
from tests.quant.test_portfolio import SPEC_A, SPEC_B, _strategy

SCT = "SCT123456abcdefXYZ"
PP = "0123456789abcdef0123456789abcdef"
WECOM = "693a91f6-7000-4bc4-97a0-0ec200fa5aaa"


def _client(log: list, reply: dict):
    def handler(req: httpx.Request) -> httpx.Response:
        log.append(req)
        return httpx.Response(200, json=reply)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_validate_keys():
    assert channels.validate("serverchan", f" {SCT} ") == SCT
    assert channels.validate("wecom_bot", f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={WECOM}") == WECOM
    for kind, bad in (("serverchan", "abc"), ("pushplus", "xyz"), ("wecom_bot", "https://evil.example.com/hook"),
                      ("telegram", "x")):
        with pytest.raises(channels.NotifyError):
            channels.validate(kind, bad)


def test_send_request_formats():
    log: list = []
    channels.send("serverchan", SCT, "标题", "**内容**", _client(log, {"code": 0}))
    assert str(log[-1].url) == f"https://sctapi.ftqq.com/{SCT}.send" and b"desp=" in log[-1].content
    channels.send("pushplus", PP, "标题", "内容", _client(log, {"code": 200}))
    body = json.loads(log[-1].content)
    assert str(log[-1].url) == "https://www.pushplus.plus/send"
    assert body["template"] == "markdown" and body["token"] == PP
    channels.send("wecom_bot", WECOM, "标题", "长" * 5000, _client(log, {"errcode": 0}))
    body = json.loads(log[-1].content)
    assert log[-1].url.params["key"] == WECOM and body["msgtype"] == "markdown"
    assert len(body["markdown"]["content"].encode("utf-8")) <= channels.WECOM_LIMIT + 100   # 超长截断
    with pytest.raises(channels.NotifyError) as e:
        channels.send("serverchan", SCT, "t", "b", _client(log, {"code": 40001, "message": "bad key"}))
    assert SCT not in str(e.value) and "bad key" in str(e.value)                            # 错误信息不含密钥


def test_channel_api_never_returns_secret(settings, client):
    r = client.post("/api/notify/channels", json={"name": "我的微信", "kind": "serverchan", "secret": SCT})
    assert r.status_code == 200, r.text
    ch = r.json()
    assert SCT not in json.dumps(ch) and ch["secret_masked"].endswith(SCT[-4:])
    assert ch["events"] == ["orders", "errors", "health", "skills", "watch"]
    bad = {"name": "x", "kind": "serverchan", "secret": "bad"}
    assert client.post("/api/notify/channels", json=bad).status_code == 400
    r = client.patch(f"/api/notify/channels/{ch['id']}", json={"name": "改名", "kind": "serverchan", "enabled": False,
                                                                "events": ["orders"]})
    assert r.json()["name"] == "改名" and r.json()["enabled"] is False                     # 不填密钥表示不变
    r = client.patch(f"/api/notify/channels/{ch['id']}", json={"name": "改名", "kind": "pushplus"})
    assert r.status_code == 400                                                             # 换方式必须重填密钥
    assert SCT not in client.get("/api/notify/channels").text
    assert client.delete(f"/api/notify/channels/{ch['id']}").json()["ok"]
    assert client.get("/api/notify/channels").json() == []


async def test_daily_orders_notification_and_dedupe(arena):
    with get_sessionmaker()() as s:
        a, b = _strategy(s, "甲", SPEC_A), _strategy(s, "乙", SPEC_B)
        pf = QuantPortfolioRow(name="提醒组合", members=[{"strategy_id": a, "weight": 0.5},
                                                     {"strategy_id": b, "weight": 0.5}])
        s.add(pf)
        s.flush()
        agent = AgentRow(name="组合·提醒", kind="quant", quant_portfolio_id=pf.id)
        s.add(agent)
        rs = s.scalars(select(RuleSetRow).where(RuleSetRow.name == "cn_long_only")).one()
        track = create_track(s, 1, name="提醒跟踪", universe_name="cn_sse50_etf", rule_set_id=rs.id,
                             initial_cash=Decimal("1000000"), mode="replay", replay_start=date(2026, 9, 1))
        s.flush()
        acc = enroll(s, track, agent.id)
        start_track(s, track, cst(2026, 10, 2, 10))
        pf.follow_account_id = acc.id
        s.add(NotifyChannelRow(user_id=1, name="微信", kind="serverchan", secret_enc=secret_box().encrypt(SCT),
                               events=["orders", "errors"]))
        s.commit()
        track_id, acc_id = track.id, acc.id
    await cycle.run_replay(track_id, lambda m: None)
    sent = []

    def fake_send(kind, secret, title, body):
        sent.append((kind, secret, title, body))

    with get_sessionmaker()() as s:
        # 最近一次决策，模拟在它的执行日早上检查
        last = s.scalars(select(RoundRow).where(RoundRow.track_id == track_id)
                         .order_by(RoundRow.data_date.desc())).first()
    morning = datetime.combine(last.fill_date, time(8, 0), ZoneInfo("Asia/Shanghai"))
    out = service.daily_check(sender=fake_send, now=morning)
    assert sent, "回放最后一天的决策应有订单"
    if sent:
        kind, secret, title, body = sent[0]
        assert kind == "serverchan" and secret == SCT
        assert "【调仓提醒】提醒组合" in title and f"{last.fill_date:%m-%d} 开盘" in body and " 股" in body
        assert out[0]["status"] == "ok"
        n = len(sent)
        service.daily_check(sender=fake_send, now=morning)
        assert len(sent) == n                      # 同一天只推送一次
    # 执行日已过：旧决策不再提醒
    later = datetime.combine(date(2026, 12, 1), time(8, 0), ZoneInfo("Asia/Shanghai"))
    before = len(sent)
    service.daily_check(sender=fake_send, now=later)
    assert len(sent) == before
    # 晚间最后一次检查：没有最近一个交易日的决策 → 异常提醒（只发一次）
    evening = datetime.combine(last.fill_date, time(21, 0), ZoneInfo("Asia/Shanghai"))
    service.daily_check(final=True, sender=fake_send, now=evening)
    alerts = [x for x in sent if "【异常提醒】" in x[2]]
    assert len(alerts) == 1 and "未完成决策" in alerts[0][2]
    service.daily_check(final=True, sender=fake_send, now=evening)
    assert len([x for x in sent if "【异常提醒】" in x[2]]) == 1
    with get_sessionmaker()() as s:
        assert {r.status for r in s.scalars(select(NotificationRow))} == {"ok"}
        assert s.scalars(select(NotifyChannelRow)).one().last_status == "ok"
    assert acc_id


def test_readiness_messages():
    today = date(2026, 12, 1)
    ok_health = {"status": "待观察", "items": [], "health": {"status": "正常", "checks": []}}
    assert service.readiness_messages("portfolio", "组合", 1, ok_health, ok_health, today) == []

    bad = {"status": "未通过", "message": "不满足", "items": [],
           "health": {"status": "提醒", "message": "明显差于回测历史",
                      "checks": [{"item": "跟踪期最大回撤", "detail": "回测历史最大回撤 10%", "level": "提醒"}]}}
    msgs = service.readiness_messages("portfolio", "组合", 1, ok_health, bad, today)
    assert [m.event for m in msgs] == ["health"]
    assert msgs[0].key == "health:portfolio:1:2026-W49" and "最大回撤" in msgs[0].body
    # 同一周内重复检查：去重键相同，不会重复推送
    assert service.readiness_messages("portfolio", "组合", 1, bad, bad, date(2026, 12, 3))[0].key == msgs[0].key

    ready = {"status": "可实盘", "message": "所有条件均满足",
             "items": [{"name": "实时跟踪", "detail": "已跟踪 60 天", "level": "pass"}]}
    msgs = service.readiness_messages("strategy", "价值", 8, ok_health, ready, today)
    assert len(msgs) == 1 and msgs[0].title.startswith("【可实盘】") and "✅" in msgs[0].body
    assert service.readiness_messages("strategy", "价值", 8, ready, ready, today) == []
    back = service.readiness_messages("strategy", "价值", 8, ready, {"status": "未通过", "items": []}, today)
    assert back[0].title == "【状态变化】价值：可实盘 → 未通过"
