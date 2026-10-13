"""v1.8：实盘成交录入 / 导入、「实盘 vs 模拟」报告、「可实盘」检查（PRD 3.9 G8）。"""

from __future__ import annotations

import base64
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from app.arena import cycle
from app.db.session import get_sessionmaker
from app.models import FillRow, NavSnapshotRow
from app.models.quant import QuantStrategyRow, RealAccountRow, RealFillRow
from app.quant import live, readiness, real
from tests.quant.test_lab import _create, lab  # noqa: F401
from tests.quant.test_live import _strategy

BROKER_CSV = """资金账号：********
导出时间：2026-10-12 15:30:00
成交日期,成交时间,证券代码,证券名称,买卖标志,成交数量,成交均价,成交金额,佣金,印花税,过户费
20261009,09:31:02,600519,贵州茅台,证券买入,100,1500.00,150000.00,37.50,0.00,1.50
20261009,09:35:10,510300,沪深300ETF,买入,2000,4.000,8000.00,5.00,0.00,0.00
20261010,10:01:00,000001,平安银行,证券卖出,300,12.50,3750.00,5.00,1.88,0.04
20261010,15:00:00,600519,贵州茅台,红利入账,0,0,1200.00,0,0,0
"""


def test_normalize_symbol():
    assert real.normalize_symbol("600519") == "600519.SH"
    assert real.normalize_symbol("sz000001") == "000001.SZ"
    assert real.normalize_symbol("510300.SH") == "510300.SH"
    assert real.normalize_symbol("159915") == "159915.SZ"
    assert real.normalize_symbol("=\"000001\"") == "000001.SZ"
    assert real.normalize_symbol("abc") is None


def test_parse_broker_csv_gbk_with_preface():
    text = real.decode(BROKER_CSV.encode("gbk"))
    out = real.parse_fills(text)
    rows = out["rows"]
    assert [(r.symbol, r.side, r.qty) for r in rows] == [
        ("600519.SH", "buy", 100), ("510300.SH", "buy", 2000), ("000001.SZ", "sell", 300)]
    assert rows[0].trade_date == date(2026, 10, 9) and rows[0].price == 1500.0
    assert abs(rows[0].fee - 39.0) < 1e-9 and abs(rows[2].fee - 6.92) < 1e-9     # 佣金 + 印花税 + 过户费
    assert len(out["skipped"]) == 1 and "不是买卖记录" in out["skipped"][0]["reason"]


def test_parse_reports_missing_columns():
    try:
        real.parse_fills("日期,代码,名称\n20261009,600519,茅台\n")
    except real.RealError as e:
        assert "买卖方向" in str(e) and "成交数量" in str(e)
    else:
        raise AssertionError("应提示缺少列")


async def test_real_report_against_replay(arena):
    """把模拟账户的成交以略差的价格「实盘执行」一遍：报告应配对全部成交、得到正的执行价差，收益差等于价差损耗。"""
    with get_sessionmaker()() as s:
        st = _strategy(s)
        s.commit()
        acc = live.replay_check(s, 1, strategy=st, start=date(2026, 8, 3), initial_cash=Decimal("1000000"))
        s.commit()
        acc_id, track_id, sid = acc.id, acc.track_id, st.id
    await cycle.run_replay(track_id, lambda m: None)
    with get_sessionmaker()() as s:
        s.get(QuantStrategyRow, sid).follow_account_id = acc_id
        first = s.scalars(select(NavSnapshotRow.date).where(NavSnapshotRow.account_id == acc_id)
                          .order_by(NavSnapshotRow.date)).first()
        ra = RealAccountRow(user_id=1, name="实盘", kind="strategy", ref_id=sid, initial_cash=1_000_000,
                            start_date=first)
        s.add(ra)
        s.flush()
        sims = list(s.scalars(select(FillRow).where(FillRow.account_id == acc_id)))
        assert sims
        for f in sims:
            side = "buy" if f.side == "buy" else "sell"
            px = float(f.price) * (1.001 if side == "buy" else 0.999)       # 实盘成交价差 10 个基点
            s.add(RealFillRow(real_account_id=ra.id, trade_date=f.trade_date, symbol=f.symbol, side=side,
                              qty=f.qty, price=px, fee=float(f.total_fee)))
        s.commit()
        rep = real.report(s, ra)
    assert rep["status"] in ("一致", "有偏差"), rep
    ex = rep["execution"]
    assert ex["matched"] == len(sims) and ex["missed_count"] == 0 and ex["extra_count"] == 0
    assert 9 < ex["avg_dev_bps"] < 11 and ex["price_cost"] > 0 and ex["avg_lag"] == 0
    # 收益差 ≈ 执行价差造成的金额 / 本金（净值重建与交易引擎逐日一致，差异只来自成交价）
    assert rep["real_return"] < rep["sim_return"] and abs(rep["gap"] + ex["price_cost"] / 1_000_000) < 0.002
    h = rep["holdings"]
    assert h["overlap"] > 0.97 * min(h["real_exposure"], h["sim_exposure"])     # 重合度上限是股票仓位
    assert len(rep["curve"]["real"]) == len(rep["curve"]["sim"])


def test_real_account_api(lab, client):  # noqa: F811
    sid = _create()
    r = client.post("/api/quant/real-accounts", json={"name": "我的账户", "kind": "strategy", "ref_id": sid,
                                                      "initial_cash": 1_000_000, "start_date": "2026-10-08"})
    assert r.status_code == 200, r.text
    rid = r.json()["id"]
    bad = {"name": "x", "kind": "strategy", "ref_id": 9999, "initial_cash": 1, "start_date": "2026-10-08"}
    assert client.post("/api/quant/real-accounts", json=bad).status_code == 404
    fill = {"trade_date": "2026-10-09", "symbol": "600519", "side": "buy", "qty": 100, "price": 1500, "fee": 5}
    assert client.post(f"/api/quant/real-accounts/{rid}/fills", json=fill).json()["added"] == 1
    assert client.post(f"/api/quant/real-accounts/{rid}/fills", json=fill).status_code == 400      # 重复
    content = base64.b64encode(BROKER_CSV.encode("gbk")).decode()
    prev = client.post(f"/api/quant/real-accounts/{rid}/import", json={"content_base64": content}).json()
    assert len(prev["rows"]) == 3 and "added" not in prev
    done = client.post(f"/api/quant/real-accounts/{rid}/import",
                       json={"content_base64": content, "commit": True}).json()
    assert done["added"] == 2 and done["duplicates"] == 1          # 茅台那笔与手工录入的重复
    fills = client.get(f"/api/quant/real-accounts/{rid}/fills").json()
    assert len(fills) == 3
    assert client.delete(f"/api/quant/real-accounts/{rid}/fills/{fills[0]['id']}").json()["ok"]
    acc = client.get("/api/quant/real-accounts").json()[0]
    assert acc["fills"] == 2 and acc["ref_name"] == "测试策略"
    assert client.delete(f"/api/quant/real-accounts/{rid}").json()["ok"]
    assert client.get("/api/quant/real-accounts").json() == []


def test_readiness_without_tracking(lab, monkeypatch):  # noqa: F811
    from app.quant import data

    monkeypatch.setattr(data, "pit_ready", lambda s, u: True)      # 视为历史成分已准备好
    sid = _create()
    with get_sessionmaker()() as s:
        out = readiness.refresh(s, "strategy", sid)
        s.commit()
        keys = [it["key"] for it in out["items"]]
        assert keys[:5] == ["dev", "holdout", "yearly", "tracking", "capacity"]
        tracking = out["items"][3]
        assert tracking["level"] == "pending" and "尚未开始" in tracking["detail"]
        assert out["status"] in ("未通过", "待观察")
        assert s.get(QuantStrategyRow, sid).readiness["status"] == out["status"]


def test_readiness_fails_on_quick_universe(lab):  # noqa: F811
    """快速体验股票池（当前成分回看历史）有幸存者偏差，不能通过可实盘检查。"""
    from app.quant.service import QUICK_UNIVERSE

    sid = _create()
    with get_sessionmaker()() as s:
        s.get(QuantStrategyRow, sid).universe_name = QUICK_UNIVERSE
        s.commit()
        out = readiness.check(s, "strategy", sid)
        first = out["items"][0]
        assert first["key"] == "universe" and first["level"] == "fail" and "幸存者偏差" in first["detail"]
        assert out["status"] == "未通过"


def test_readiness_fails_when_pit_history_missing(lab):  # noqa: F811
    """全时点股票池的历史成分没准备好（Baostock 失败）时，等于只用今天的成分回看历史，判为未通过。"""
    sid = _create()
    with get_sessionmaker()() as s:
        out = readiness.check(s, "strategy", sid)
        first = out["items"][0]
        assert first["key"] == "universe" and first["level"] == "fail" and "历史成分还没准备好" in first["detail"]
