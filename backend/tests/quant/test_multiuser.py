"""多用户数据隔离：公共策略只读、私有数据互不可见、模型与交易成本各自独立、管理员专属功能。

开启登录保护，管理员（admin）与普通用户（bob）各用一个客户端。
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.auth import core
from app.core.config import get_settings
from app.db.session import get_sessionmaker
from app.models import LLMProviderRow
from app.models.quant import QuantStrategyRow
from app.quant import costs
from tests.quant.test_lab import BAD, GOOD, _create, lab  # noqa: F401

PW = "test-pass-123"


@pytest.fixture
def stub_watch(monkeypatch):
    """自选股用到的证券信息与行情（不联网）。"""
    from app.market.types import Instrument
    from app.watch import service as watch

    monkeypatch.setattr(watch.sv, "resolve", lambda s, sym: Instrument(
        symbol=sym, market="CN", name="测试股份", type="stock", board="main", lot_size=100, tick=0.01))
    monkeypatch.setattr(watch.sv, "quotes", lambda syms: {x: {"name": "测试股份", "price": 10.0, "change_pct": 0.0}
                                                           for x in syms})
    monkeypatch.setattr(watch.sv, "relations", lambda s, sym, uid=1: {"picks": [], "holdings": [], "reviews": []})


@pytest.fixture
def two(lab, monkeypatch) -> Iterator[tuple[TestClient, TestClient, int, int]]:  # noqa: F811
    """(管理员客户端, 普通用户客户端, 管理员的公共策略 id, 管理员的私有策略 id)。"""
    monkeypatch.setenv("SA_AUTH_ENABLED", "true")
    get_settings.cache_clear()
    core.invalidate()
    core.limiter = core.LoginLimiter()
    core.register_limiter = core.LoginLimiter(max_failures=50, window=3600.0)
    costs._by_user.clear()
    from app.main import create_app

    pub, priv = _create(BAD), _create(GOOD)
    with get_sessionmaker()() as s:
        s.get(QuantStrategyRow, pub).is_public = True
        s.commit()
    app = create_app()
    with TestClient(app) as admin, TestClient(app) as bob:
        body = {"password": PW, "setup_code": core.ensure_setup_code()}
        assert admin.post("/api/auth/setup", json=body).status_code == 200
        assert admin.put("/api/auth/registration", json={"open": True}).status_code == 200
        assert bob.post("/api/auth/register", json={"username": "bob", "password": PW}).status_code == 200
        yield admin, bob, pub, priv
    core.invalidate()
    costs._by_user.clear()


def test_public_and_private_strategies(two):
    admin, bob, pub, priv = two
    lst = {s["id"]: s for s in bob.get("/api/quant/strategies").json()["strategies"]}
    assert set(lst) == {pub}                                     # 看得到公共策略，看不到管理员的私有策略
    assert lst[pub]["is_public"] and not lst[pub]["mine"] and lst[pub]["provider_id"] is None
    assert {s["id"] for s in admin.get("/api/quant/strategies").json()["strategies"]} == {pub, priv}

    # 公共策略：可以查看、看版本、试算、看信号所需的读取接口；不能修改、优化、跟踪
    assert bob.get(f"/api/quant/strategies/{pub}").status_code == 200
    assert bob.get(f"/api/quant/strategies/{priv}").status_code == 404
    v = bob.get(f"/api/quant/strategies/{pub}").json()["champion"]["id"]
    assert bob.get(f"/api/quant/versions/{v}").status_code == 200
    assert bob.post("/api/quant/preview", json={"spec": GOOD, "strategy_id": pub}).status_code == 200
    assert bob.patch(f"/api/quant/strategies/{pub}", json={"name": "改名"}).status_code == 404
    assert bob.post(f"/api/quant/strategies/{pub}/research", json={}).status_code == 404
    assert bob.post(f"/api/quant/strategies/{pub}/versions", json={"spec": GOOD}).status_code == 404
    assert bob.post(f"/api/quant/versions/{v}/promote").status_code == 404
    assert bob.post(f"/api/quant/strategies/{pub}/follow", json={}).status_code == 404
    assert bob.get(f"/api/quant/strategies/{pub}/tracking").status_code == 404
    assert bob.get(f"/api/quant/strategies/{pub}/readiness").status_code == 404
    assert bob.patch(f"/api/quant/strategies/{pub}", json={"is_public": False}).status_code == 404

    # 另存公共策略：成为自己的私有策略
    r = bob.post(f"/api/quant/strategies/{pub}/fork", json={"name": "我的副本"})
    assert r.status_code == 200, r.text
    mine = r.json()
    assert mine["mine"] and not mine["is_public"] and mine["origin"]["strategy_id"] == pub
    assert bob.patch(f"/api/quant/strategies/{mine['id']}", json={"name": "改名"}).status_code == 200
    assert admin.get(f"/api/quant/strategies/{mine['id']}").status_code == 404     # 管理员也看不到别人的私有策略
    # 普通用户不能把策略设为公共；不能另存别人的私有策略
    r = bob.patch(f"/api/quant/strategies/{mine['id']}", json={"is_public": True})
    assert r.status_code == 403
    assert bob.post(f"/api/quant/strategies/{priv}/fork", json={"name": "x"}).status_code == 404
    # 新建的策略属于自己
    r = bob.post("/api/quant/strategies", json={"name": "自建", "spec": GOOD, "settings": {"min_holdings": 0}})
    assert r.status_code == 200 and r.json()["mine"]
    with get_sessionmaker()() as s:
        assert s.get(QuantStrategyRow, r.json()["id"]).user_id == s.get(QuantStrategyRow, mine["id"]).user_id != 1


def test_models_are_not_shared(two):
    admin, bob, pub, priv = two
    admin_pid = admin.get("/api/providers").json()[0]["id"]
    assert bob.get("/api/providers").json() == []
    body = {"name": "x", "spec": GOOD, "provider_id": admin_pid, "settings": {"min_holdings": 0}}
    r = bob.post("/api/quant/strategies", json=body)
    assert r.status_code == 400 and "自己配置" in r.json()["detail"]
    r = bob.post(f"/api/quant/strategies/{pub}/fork", json={"name": "x", "provider_id": admin_pid})
    assert r.status_code == 400
    fid = bob.post(f"/api/quant/strategies/{pub}/fork", json={"name": "y"}).json()["id"]
    assert bob.patch(f"/api/quant/strategies/{fid}", json={"provider_id": admin_pid}).status_code == 400
    # 即使绕过接口把别人的模型写进策略，研究也不会使用
    with get_sessionmaker()() as s:
        s.get(QuantStrategyRow, fid).provider_id = admin_pid
        s.commit()
    r = bob.post(f"/api/quant/strategies/{fid}/research", json={"rounds": 1})
    if r.status_code == 200:
        import asyncio

        from app.quant import research

        assert asyncio.run(research.run_research(r.json()["id"])) == "未设置研究模型"
    # 自己配置的模型可以用
    with get_sessionmaker()() as s:
        p = LLMProviderRow(name="bob的模型", protocol="openai_compatible", base_url="http://127.0.0.1:1/v1",
                           default_model="m", price_in_per_m=Decimal("0"), price_out_per_m=Decimal("0"),
                           user_id=s.get(QuantStrategyRow, fid).user_id)
        s.add(p)
        s.commit()
        bob_pid = p.id
    assert bob.patch(f"/api/quant/strategies/{fid}", json={"provider_id": bob_pid}).status_code == 200


def test_portfolio_members_must_be_own(two):
    admin, bob, pub, priv = two
    fid = bob.post(f"/api/quant/strategies/{pub}/fork", json={"name": "y"}).json()["id"]
    gid = bob.post(f"/api/quant/strategies/{pub}/fork", json={"name": "z"}).json()["id"]
    members = [{"strategy_id": pub, "weight": 1}, {"strategy_id": fid, "weight": 1}]
    r = bob.post("/api/quant/portfolios", json={"name": "p", "members": members})
    assert r.status_code == 400 and "另存" in r.json()["detail"]                  # 公共策略要先另存
    members = [{"strategy_id": fid, "weight": 1}, {"strategy_id": gid, "weight": 1}]
    r = bob.post("/api/quant/portfolios", json={"name": "p", "members": members})
    assert r.status_code == 200
    pid = r.json()["id"]
    assert [p["id"] for p in bob.get("/api/quant/portfolios").json()] == [pid]
    assert admin.get("/api/quant/portfolios").json() == []
    assert admin.get(f"/api/quant/portfolios/{pid}").status_code == 404


def test_costs_are_per_user(two):
    admin, bob, pub, priv = two
    r = bob.put("/api/quant/costs", json={"commission_rate": 0.0001, "commission_min": 0, "dividend_tax": False})
    assert r.status_code == 200 and r.json()["costs"]["commission_rate"] == 0.0001
    assert admin.get("/api/quant/costs").json()["costs"] == costs.TradeCosts().to_dict()
    assert bob.get("/api/quant/costs").json()["costs"]["commission_min"] == 0
    # 同一份规则：bob（佣金万 1 免五、不扣红利税）的回测费用比管理员（默认万 2.5、最低 5 元）低
    a = admin.post("/api/quant/strategies", json={"name": "a", "spec": GOOD, "settings": {"min_holdings": 0}}).json()
    b = bob.post("/api/quant/strategies", json={"name": "b", "spec": GOOD, "settings": {"min_holdings": 0}}).json()
    da = admin.get(f"/api/quant/versions/{a['champion']['id']}").json()["dev_full"]
    db_ = bob.get(f"/api/quant/versions/{b['champion']['id']}").json()["dev_full"]
    assert db_["cost_ratio"] < da["cost_ratio"]


def test_admin_only_features(two):
    admin, bob, pub, priv = two
    for method, url in (("get", "/api/skills"), ("get", "/api/quant/data-quality"),
                        ("post", "/api/quant/data-quality/scan"), ("post", "/api/quant/data/prepare"),
                        ("post", "/api/quant/strategies/seed"), ("get", "/api/system/events"),
                        ("get", "/api/system/jobs"), ("get", "/api/auth/users")):
        kw = {"json": {}} if method == "post" else {}
        assert getattr(bob, method)(url, **kw).status_code == 403, url
    assert admin.get("/api/skills").status_code == 409          # 管理员可以访问，但技能功能默认关闭
    assert admin.get("/api/system/jobs").status_code == 200
    # 普通功能照常
    assert bob.get("/api/quant/factors").status_code == 200
    assert bob.get("/api/meta/universes").status_code == 200


def test_watch_and_home_isolated(two, stub_watch):  # noqa: F811
    admin, bob, pub, priv = two
    r = admin.post("/api/watch", json={"symbol": "600519.SH", "weight": 2})
    assert r.status_code == 200, r.text
    item = r.json()["id"]
    assert bob.get("/api/watch").json()["items"] == []
    assert bob.patch(f"/api/watch/{item}", json={"weight": 0}).status_code == 404
    assert bob.delete(f"/api/watch/{item}").status_code == 404
    assert bob.get("/api/watch/by-symbol/600519.SH").json() is None
    assert bob.post("/api/watch", json={"symbol": "600519.SH"}).status_code == 200    # 各自的自选股互不影响
    assert len(admin.get("/api/watch").json()["items"]) == 1
    assert admin.get("/api/watch/by-symbol/600519.SH").json()["weight"] == 2

    assert bob.put("/api/home/stars", json={"symbols": ["000001.SZ"]}).status_code == 200
    with get_sessionmaker()() as s:
        from app.stocks import home

        assert home.stars(s, 1) != ["000001.SZ"] and home.stars(s, 2) == ["000001.SZ"]


def test_notify_only_owner_channels(two):
    from app.arena.deps import secret_box
    from app.models.notify import NotifyChannelRow
    from app.notify import service as notify

    sent = []
    with get_sessionmaker()() as s:
        for uid in (1, 2):
            s.add(NotifyChannelRow(user_id=uid, name=f"u{uid}", kind="serverchan", enabled=True, events=["watch"],
                                   secret_enc=secret_box().encrypt(f"k{uid}")))
        s.commit()
    msg = notify.Message("watch", "k1", "t", "b", user_id=2)
    notify.deliver(msg, sender=lambda kind, secret, title, body: sent.append(secret))
    assert sent == ["k2"]
