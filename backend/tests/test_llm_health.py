"""模型失效：连续失败提醒与恢复提醒、用到某个模型的地方、一次换成其他模型。"""

from __future__ import annotations

import pytest

from app.db.session import get_sessionmaker
from app.llm import ChatRequest, LLMError, ProviderConfig, create_client, health
from app.models import LLMProviderRow


@pytest.fixture
def sent(monkeypatch):
    out = []
    from app.notify import service as notify

    monkeypatch.setattr(notify, "deliver", lambda msg: out.append(msg))
    health._last_ok_write.clear()
    return out


def _provider(name="本地模型") -> int:
    with get_sessionmaker()() as s:
        p = LLMProviderRow(name=name, protocol="openai_compatible", base_url="http://127.0.0.1:1/v1",
                           default_model="m", user_id=1)
        s.add(p)
        s.commit()
        return p.id


def test_alert_after_consecutive_failures_and_recovery(db, sent):
    pid = _provider()
    for _ in range(health.FAIL_ALERT - 1):
        health.record(pid, False, "无法连接模型服务：Connection error.")
    assert not sent
    health.record(pid, False, "无法连接模型服务：Connection error.")
    assert len(sent) == 1 and sent[0].event == "errors" and "模型不可用" in sent[0].title
    assert "Connection error" in sent[0].body
    health.record(pid, False, "还是连不上")                     # 同一次故障只提醒一次
    assert len(sent) == 1
    with get_sessionmaker()() as s:
        st = health.get(s, pid)
        assert st["fails"] == health.FAIL_ALERT + 1 and st["alerted"] and st["down_since"]
    health.record(pid, True)
    assert len(sent) == 2 and "恢复" in sent[1].title
    with get_sessionmaker()() as s:
        st = health.get(s, pid)
        assert st["fails"] == 0 and not st["alerted"] and "down_since" not in st


async def test_tracked_client_records_failures(db, sent, monkeypatch):
    pid = _provider()
    client = create_client(ProviderConfig("openai_compatible", "http://127.0.0.1:1/v1", None, provider_id=pid))
    assert isinstance(client, health.TrackedClient)
    assert not isinstance(create_client(ProviderConfig("openai_compatible", "http://x/v1", None)),
                          health.TrackedClient)

    async def broken(req):
        raise LLMError("无法连接模型服务：Connection error.")

    monkeypatch.setattr(client._inner, "chat", broken)
    with pytest.raises(LLMError):
        await client.chat(ChatRequest(model="m", system="", messages=[]))
    with get_sessionmaker()() as s:
        assert health.get(s, pid)["fails"] == 1


def test_usages_replace_and_delete_guard(lab, client, sent):  # noqa: F811
    from tests.quant.test_lab import _create

    old, new = _provider("失效的模型"), _provider("新模型")
    sid = _create(provider_id=old)
    u = client.get(f"/api/providers/{old}/usages").json()
    assert [x["id"] for x in u["strategies"]] == [sid] and u["health"]["fails"] == 0
    r = client.delete(f"/api/providers/{old}")
    assert r.status_code == 400 and "用到的地方" in r.json()["detail"]

    r = client.post(f"/api/providers/{old}/replace", json={"target_provider_id": new, "model": "new-model"})
    assert r.status_code == 200 and r.json()["replaced"]["strategies"] == 1
    assert r.json()["remaining"]["strategies"] == []
    detail = client.get(f"/api/quant/strategies/{sid}").json()
    assert detail["provider_id"] == new and detail["model"] == "new-model"
    assert client.delete(f"/api/providers/{old}").status_code == 200
    assert client.post(f"/api/providers/{new}/replace", json={"target_provider_id": 99999}).status_code == 400


from tests.quant.test_lab import lab  # noqa: E402, F401
