"""另存为新策略：独立的版本历史、继承检验次数（不能靠另存清零）、半时点股票池改为全时点、接口。"""

from __future__ import annotations

from app.db.session import get_sessionmaker
from app.models.quant import QuantStrategyRow, QuantVersionRow
from app.quant import research, service
from app.quant.research import ParsedResearch
from app.quant.spec import parse_spec
from tests.quant.test_lab import BAD, GOOD, _create, lab  # noqa: F401


def _manual(sid: int, window: int) -> int:
    with get_sessionmaker()() as s:
        st = s.get(QuantStrategyRow, sid)
        spec = {**GOOD, "factors": [{"name": "momentum", "params": {"window": window}}]}
        v = research.evaluate_manual(s, st, spec, f"窗口 {window}", "")
        s.commit()
        return v.id


def _fork(sid: int, version_id: int | None = None, **kw) -> int:
    with get_sessionmaker()() as s:
        src = s.get(QuantStrategyRow, sid)
        v = s.get(QuantVersionRow, version_id or src.champion_id)
        st = service.fork_strategy(s, src, v, name=kw.pop("name", "副本"), **kw)
        s.commit()
        return st.id


def test_fork_is_independent_and_inherits_tests(lab):  # noqa: F811
    sid = _create(BAD)
    with get_sessionmaker()() as s:
        s.get(QuantStrategyRow, sid).research_notes = [{"text": "经验", "source": "ai"}]
        s.commit()
    for w in (20, 40, 80):                     # 3 个候选挑战过冠军
        _manual(sid, w)
    fid = _fork(sid, name="价值·副本")
    with get_sessionmaker()() as s:
        src, st = s.get(QuantStrategyRow, sid), s.get(QuantStrategyRow, fid)
        src_spec = s.get(QuantVersionRow, src.champion_id).spec
        versions = s.query(QuantVersionRow).filter_by(strategy_id=fid).all()
        assert len(versions) == 1 and versions[0].status == "champion" and versions[0].spec == src_spec
        assert versions[0].title == "另存自「测试策略」v1" and versions[0].dev
        assert st.settings == src.settings and st.style == src.style and st.description == src.description
        assert [n["text"] for n in st.research_notes] == ["经验"] and not st.auto_optimize
        o = st.origin
        assert (o["strategy_id"], o["version_number"], o["prior_tests"], o["base_version_id"]) == (
            sid, 1, 3, st.champion_id)
        assert service.prior_tests(s, st, st.champion_id) == 3
        # 按尝试次数折算的夏普：来源的 4 次尝试 + 新策略自己的 1 次
        assert len(service.trial_scores(s, st)) == 5
        assert service.research_quality(s, st)["tests_since_champion"] == 3

    # 新策略里 AI 再试一个候选：门槛按 3 + 1 = 4 次检验收紧
    out = research._round_evaluate(fid, 0, ParsedResearch("", [
        {"title": "t", "rationale": "r", "spec": parse_spec({**BAD, "selection": {"top_n": 3}})}]))
    with get_sessionmaker()() as s:
        v = s.get(QuantVersionRow, out["candidates"][0]["version_id"])
        assert v.dev["vs_champion"]["n_tests"] == 4
        # 来源策略不受影响
        assert s.query(QuantVersionRow).filter_by(strategy_id=sid).count() == 4

    # 另存的另存：沿来源逐级追溯
    with get_sessionmaker()() as s:
        base = s.get(QuantStrategyRow, fid).origin["base_version_id"]
    gid = _fork(fid, base, name="副本的副本", copy_notes=False)
    with get_sessionmaker()() as s:
        st = s.get(QuantStrategyRow, gid)
        assert st.origin["prior_tests"] == 4 and st.research_notes == []
        assert len(service.trial_scores(s, st)) == 4 + 2 + 1


def test_fork_non_champion_version_and_half_pit_universe(lab):  # noqa: F811
    sid = _create(BAD)
    vid = _manual(sid, 40)                     # 未升级的候选也可以另存
    with get_sessionmaker()() as s:
        s.get(QuantStrategyRow, sid).universe_name = "cn_csi300_pit_etf"
        s.commit()
    fid = _fork(sid, vid)
    with get_sessionmaker()() as s:
        st = s.get(QuantStrategyRow, fid)
        assert st.universe_name == "cn_csi300_hist"          # 半时点只保留作对比，另存时改用全时点
        assert st.origin["version_status"] == "candidate" and st.origin["prior_tests"] == 0
        assert s.get(QuantVersionRow, st.champion_id).spec["factors"][0]["params"]["window"] == 40


def test_fork_api(lab, client):  # noqa: F811
    _, pid = lab
    sid = _create(BAD)
    vid = _manual(sid, 40)
    r = client.post(f"/api/quant/strategies/{sid}/fork",
                    json={"name": "顺势版", "version_id": vid, "provider_id": pid, "model": "fake",
                          "auto_optimize": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "顺势版" and body["auto_optimize"] and body["provider_id"] == pid
    assert body["origin"]["version_number"] == 2 and body["champion"]["number"] == 1
    assert client.get(f"/api/quant/strategies/{body['id']}").json()["origin"]["strategy_name"] == "测试策略"
    # 默认另存当前冠军
    assert client.post(f"/api/quant/strategies/{sid}/fork", json={"name": "冠军副本"}).json()[
        "origin"]["version_number"] == 1
    r = client.post(f"/api/quant/strategies/{sid}/fork", json={"name": "x", "auto_optimize": True})
    assert r.status_code == 400 and "研究模型" in r.json()["detail"]
    assert client.post("/api/quant/strategies/9999/fork", json={"name": "x"}).status_code == 404
    other = _create(GOOD)
    other_v = client.get(f"/api/quant/strategies/{other}").json()["champion"]["id"]
    r = client.post(f"/api/quant/strategies/{sid}/fork", json={"name": "x", "version_id": other_v})
    assert r.status_code == 400 and "不属于" in r.json()["detail"]


def test_preview_dev_only_and_create_api(lab, client):  # noqa: F811
    _, pid = lab
    r = client.post("/api/quant/preview", json={"spec": GOOD, "universe_name": "cn_csi300_hist"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["window"]["end"] == "2024-12-31" and "holdout" not in body       # 只有开发期
    assert body["dev"]["sharpe"] is not None and body["rules"] and body["curves"]["nav"]
    assert any("至少持有" in w for w in body["warnings"])                         # 默认最少持仓 20，GOOD 只持 2 只
    assert client.post("/api/quant/preview", json={"spec": {"factors": []}}).status_code == 400
    # 沿用策略的检验区间
    sid = _create(BAD)
    body = client.post("/api/quant/preview", json={"spec": GOOD, "strategy_id": sid}).json()
    assert body["window"]["end"] == "2021-06-30" and not any("至少持有" in w for w in body["warnings"])

    new = {"name": "我的动量", "style": "custom", "description": "自定义", "spec": GOOD,
           "settings": {"min_holdings": 0}}
    r = client.post("/api/quant/strategies", json=new | {"auto_optimize": True})
    assert r.status_code == 400 and "研究模型" in r.json()["detail"]
    r = client.post("/api/quant/strategies", json=new | {"auto_optimize": True, "provider_id": pid, "model": "fake"})
    assert r.status_code == 200, r.text
    assert r.json()["auto_optimize"] and r.json()["champion"]["number"] == 1 and r.json()["origin"] is None
