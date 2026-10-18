"""升级情况：DSR 的稳健方差、未升级原因汇总、门槛过严时暂停每晚自动优化。"""

from __future__ import annotations

import numpy as np
from fastapi.testclient import TestClient

from app.db.session import get_sessionmaker
from app.models.quant import QuantStrategyRow, QuantVersionRow
from app.quant import service, tasks
from app.quant.evaluate import reject_codes, trial_sharpe_var
from tests.quant.test_lab import _create, lab  # noqa: F401


def test_trial_sharpe_var_ignores_near_cash_outliers():
    normal = [0.30, 0.42, 0.25, 0.38, 0.51, 0.33, 0.29, 0.45, 0.36, 0.40]
    assert abs(trial_sharpe_var(normal) - float(np.var(normal, ddof=1))) < 1e-12      # 正常时与样本方差相同
    spoiled = normal + [-4.05, -3.64, -3.64]                                           # 几乎空仓的候选
    assert float(np.var(spoiled, ddof=1)) > 1.0
    assert trial_sharpe_var(spoiled) < 0.2
    assert trial_sharpe_var([0.3]) is None
    assert trial_sharpe_var([0.3, 0.3, 0.3]) == 0.0


def test_reject_codes():
    d = ("月度超额 +1.2%/年 不显著（p=0.12，需 ≤ 0.0006，按累计 80 个候选校正）；"
         "7 年中只有 3 年跑赢冠军（至少需要 4 年），改进不够一致")
    assert reject_codes(d) == ["significance", "years"]
    assert reject_codes("开发期夏普 0.31 不高于冠军 0.40") == ["sharpe"]
    assert reject_codes("开发期最大回撤 31.0% 超过上限 25%") == ["drawdown"]
    assert reject_codes("") == []


def _add_rejected(sid: int, n: int, decision: str, p: float | None = None) -> None:
    with get_sessionmaker()() as s:
        st = s.get(QuantStrategyRow, sid)
        base = service.next_number(s, sid)
        for i in range(n):
            dev = {"sharpe": 0.1, "vs_champion": {"p": p}} if p is not None else {"sharpe": 0.1}
            s.add(QuantVersionRow(strategy_id=sid, number=base + i, parent_id=st.champion_id, source="ai",
                                  status="rejected", decision=decision, dev=dev, spec={}))
        s.commit()


def test_status_and_auto_pause(lab, client: TestClient):  # noqa: F811
    sid = _create()
    with get_sessionmaker()() as s:
        st = s.get(QuantStrategyRow, sid)
        st.auto_optimize = True
        out = service.upgrade_outlook(s, st)
        assert out["tests"] == 0 and not out["hopeless"]          # 0.05 / 3
        s.commit()
    _add_rejected(sid, 15, "开发期夏普 0.05 不高于冠军 0.20")
    _add_rejected(sid, 6, "月度超额 +2.0%/年 不显著（p=0.04，需 ≤ 0.002）", p=0.04)
    _add_rejected(sid, 2, "月度超额 不显著（p=0.3）；参数浮动 ±20% 后……不够稳健", p=0.3)

    with get_sessionmaker()() as s:
        st = s.get(QuantStrategyRow, sid)
        status = service.upgrade_status(s, st)
        assert status["candidates"] == 23 and status["tests"] == 23
        reasons = {r["code"]: r["count"] for r in status["reasons"]}
        assert reasons == {"sharpe": 15, "significance": 8, "robustness": 2}
        assert status["only_significance"] == 6 and status["p_below_05"] == 6 and status["best_p"] == 0.04
        assert status["hopeless"] and status["p_need"] < service.AUTO_PAUSE_P         # 0.05 / 26

        # 每晚：暂停并记录
        assert tasks.pause_if_hopeless(s, st)
        assert st.auto_optimize is False and st.auto_pause["tests"] == 23 and not st.auto_pause["resumed"]
        s.commit()

    detail = client.get(f"/api/quant/strategies/{sid}").json()
    assert detail["upgrade_status"]["auto_pause"]["tests"] == 23

    # 用户看过后重新打开：记为继续，之后每晚不再暂停
    r = client.patch(f"/api/quant/strategies/{sid}", json={"auto_optimize": True})
    assert r.status_code == 200 and r.json()["auto_optimize"] is True
    with get_sessionmaker()() as s:
        st = s.get(QuantStrategyRow, sid)
        assert st.auto_pause["resumed"] is True
        assert not tasks.pause_if_hopeless(s, st) and st.auto_optimize

        # 换了冠军：检验次数重新开始，暂停记录清空
        v = s.scalars(select_last(sid)).first()
        service.promote(s, st, v, "测试")
        assert st.auto_pause is None and not service.upgrade_outlook(s, st)["hopeless"]
        s.commit()


def select_last(sid: int):
    from sqlalchemy import select

    return select(QuantVersionRow).where(QuantVersionRow.strategy_id == sid).order_by(QuantVersionRow.id.desc())
