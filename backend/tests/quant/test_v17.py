"""v1.7：回测—跟踪对账、健康度、资金容量（PRD 3.9 G4 / G6 / G7 / G9）。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.arena import cycle
from app.db.session import get_sessionmaker
from app.quant import capacity, live, reconcile
from app.quant.service import LabSettings
from app.quant.spec import parse_spec
from tests.quant.test_lab import GOOD, PANEL, SETTINGS
from tests.quant.test_live import _strategy


async def test_replay_check_reconciles_with_backtest(arena):
    with get_sessionmaker()() as s:
        st = _strategy(s)
        s.commit()
        acc = live.replay_check(s, 1, strategy=st, start=date(2026, 8, 3), initial_cash=Decimal("1000000"))
        from app.models import TrackRow

        track = s.get(TrackRow, acc.track_id)
        assert track.mode == "replay" and track.name.startswith(live.REPLAY_PREFIX)
        s.commit()
        acc_id, track_id = acc.id, acc.track_id
    assert "回放完成" in await cycle.run_replay(track_id, lambda m: None)
    with get_sessionmaker()() as s:
        r = reconcile.reconcile(s, acc_id)
        assert r["days"] >= 20 and r["status"] in ("一致", "有偏差"), r
        assert r["matched_fills"] > 0 and abs(r["price_dev_bps"]) < 50
        assert len(r["curve"]["live"]) == len(r["curve"]["backtest"]) and r["holdings_overlap"] > 0.5
        h = reconcile.health(s, acc_id, r)
        assert h["status"] in ("正常", "关注", "提醒", "数据不足")


def test_capacity_levels_and_minimum_capital():
    cfg = LabSettings.from_dict(SETTINGS)
    out = capacity.capacity([(parse_spec(GOOD), 1.0)], PANEL, cfg)
    caps = [r["capital"] for r in out["levels"]]
    assert caps == list(capacity.LEVELS)
    devs = {r["capital"]: r["deviation"] for r in out["levels"]}
    assert devs[1e9] > devs[1e6]                     # 10 亿时受成交额限制，持仓偏离目标更多
    assert min(r["extra_deviation"] for r in out["levels"]) == 0
    assert out["min_capital"] is not None and (out["capacity"] is None or out["capacity"] >= 1e6)


def test_reconcile_requires_quant_account(arena):
    with get_sessionmaker()() as s:
        assert reconcile.reconcile(s, 999999)["status"] == "不适用"
