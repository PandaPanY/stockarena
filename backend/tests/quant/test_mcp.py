"""MCP 服务：令牌认证、只读与修改权限、数据隔离与网页一致、不返回留出期、试算计入测试次数。"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.auth import tokens
from app.db.session import get_sessionmaker
from app.models.quant import QuantStrategyRow
from tests.quant.test_lab import BAD, GOOD, _create, lab  # noqa: F401
from tests.quant.test_multiuser import two  # noqa: F401


def _token(c: TestClient, can_write: bool = False) -> str:
    r = c.post("/api/auth/tokens", json={"name": "测试客户端", "can_write": can_write})
    assert r.status_code == 200, r.text
    assert r.json()["token"].startswith(tokens.PREFIX)
    return r.json()["token"]


def _rpc(c: TestClient, token: str | None, method: str, params: dict | None = None, mid: int = 1):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    msg = {"jsonrpc": "2.0", "id": mid, "method": method} | ({"params": params} if params is not None else {})
    return c.post("/api/mcp", json=msg, headers=headers)


def _call(c: TestClient, token: str, tool: str, **args) -> tuple[dict | str, bool]:
    r = _rpc(c, token, "tools/call", {"name": tool, "arguments": args})
    assert r.status_code == 200, r.text
    res = r.json()["result"]
    text = res["content"][0]["text"]
    return (text if res["isError"] else json.loads(text)), res["isError"]


def _no_holdout(o) -> bool:
    s = json.dumps(o, ensure_ascii=False).replace('"holdout_start"', "")      # 检验设置里的区间边界可以给
    return "holdout" not in s and "rank_corr" not in s and "walk_forward" not in s


def test_token_required_and_protocol(two):  # noqa: F811
    admin, bob, pub, priv = two
    anon = TestClient(admin.app)
    assert _rpc(anon, None, "initialize").status_code == 401
    assert _rpc(anon, "sa_not-a-real-token", "initialize").status_code == 401
    # 登录凭证（cookie）不能代替 API 令牌
    assert admin.post("/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 401

    tok = _token(bob)
    r = _rpc(anon, tok, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                        "clientInfo": {"name": "t", "version": "1"}})
    init = r.json()["result"]
    assert init["protocolVersion"] == "2025-03-26" and "tools" in init["capabilities"]
    assert "只读" in init["instructions"]
    note = anon.post("/api/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                     headers={"Authorization": f"Bearer {tok}"})
    assert note.status_code == 202
    assert _rpc(anon, tok, "ping").json()["result"] == {}
    assert _rpc(anon, tok, "no/such").json()["error"]["code"] == -32601
    assert anon.get("/api/mcp").status_code == 405
    bad = anon.post("/api/mcp", content=b"{oops", headers={"Authorization": f"Bearer {tok}"})
    assert bad.json()["error"]["code"] == -32700
    # 其他接口不接受 API 令牌
    assert anon.get("/api/quant/strategies", headers={"Authorization": f"Bearer {tok}"}).status_code == 401

    # 只读令牌只列出只读工具，调用修改工具被拒绝
    names = {t["name"] for t in _rpc(anon, tok, "tools/list").json()["result"]["tools"]}
    assert "get_strategy" in names and "create_strategy" not in names
    out, err = _call(anon, tok, "create_strategy", name="x", spec=GOOD)
    assert err and "只读" in out


def test_isolation_and_dev_only(two):  # noqa: F811
    admin, bob, pub, priv = two
    anon = TestClient(admin.app)
    tok = _token(bob)
    lst, err = _call(anon, tok, "list_strategies")
    ids = {x["id"] for x in lst["strategies"]}
    assert not err and pub in ids and priv not in ids                 # 与网页一致：看不到别人的私有策略
    assert _no_holdout(lst)

    d, err = _call(anon, tok, "get_strategy", strategy_id=pub)
    assert not err and d["champion"]["spec"] and d["champion"]["dev"]["sharpe"] is not None
    assert _no_holdout(d)
    out, err = _call(anon, tok, "get_strategy", strategy_id=priv)
    assert err
    tl, err = _call(anon, tok, "get_timeline", strategy_id=pub)
    assert not err and _no_holdout(tl)
    ref, err = _call(anon, tok, "get_spec_reference")
    assert not err and ref["factors"]

    # 撤销后立即失效；别人的令牌不能撤销
    tid = next(t["id"] for t in bob.get("/api/auth/tokens").json())
    assert admin.delete(f"/api/auth/tokens/{tid}").status_code == 404
    assert bob.delete(f"/api/auth/tokens/{tid}").status_code == 200
    assert _rpc(anon, tok, "ping").status_code == 401


def test_write_token_counts_previews(two):  # noqa: F811
    admin, bob, pub, priv = two
    anon = TestClient(admin.app)
    tok = _token(bob, can_write=True)
    for spec in (BAD, GOOD):
        p, err = _call(anon, tok, "preview_backtest", spec=spec)
        assert not err and _no_holdout(p) and "curves" not in p
    assert p["pending_trials"] == 2

    st, err = _call(anon, tok, "create_strategy", name="MCP 策略", spec=GOOD)
    assert not err and st["prior_trials_counted"] == 2
    with get_sessionmaker()() as s:
        row = s.get(QuantStrategyRow, st["id"])
        assert row.user_id != 1 and row.origin["kind"] == "new" and len(row.origin["trial_scores"]) == 2

    v, err = _call(anon, tok, "submit_version", strategy_id=st["id"], spec=BAD, title="换回动量")
    assert not err and v["number"] == 2 and v["decision"] and _no_holdout(v)
    # 不能改别人的策略
    out, err = _call(anon, tok, "submit_version", strategy_id=pub, spec=BAD)
    assert err
    # 新建后试算记录清零
    st2, _ = _call(anon, tok, "create_strategy", name="第二个", spec=BAD)
    assert st2["prior_trials_counted"] == 0


def test_tokens_limit_and_disabled(two, monkeypatch):  # noqa: F811
    admin, bob, pub, priv = two
    monkeypatch.setattr(tokens, "MAX_PER_USER", 2)
    tok = _token(bob)
    _token(bob)
    assert bob.post("/api/auth/tokens", json={"name": "第三个"}).status_code == 400
    listed = bob.get("/api/auth/tokens").json()
    assert len(listed) == 2 and all("token" not in t and t["prefix"] for t in listed)
    uid = next(u["id"] for u in admin.get("/api/auth/users").json() if u["name"] == "bob")
    assert admin.patch(f"/api/auth/users/{uid}", json={"disabled": True}).status_code == 200
    assert _rpc(TestClient(admin.app), tok, "ping").status_code == 401
