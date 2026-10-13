"""AI 交易复核有效性评估（预注册 docs/prereg/2026-10-08-ai-review.md）：样本去重、超额收益、分日置换检验、判定表。"""

from __future__ import annotations

import numpy as np
import pytest

from app.quant import review_eval as ev
from app.quant.review_eval import Event
from tests.quant.test_backtest import make_panel


def _strata(seed: int, gap: float, days: int = 30):
    """每天 1 只否决 + 4 只放行；否决的股票之后平均落后 gap。"""
    rng = np.random.default_rng(seed)
    out = []
    for _ in range(days):
        x = rng.normal(0, 0.10, 5)
        x[0] -= gap
        out.append((x, np.array([True, False, False, False, False])))
    return out


def test_stratified_d_and_permutation_detect_large_effect_only():
    good = _strata(1, 0.10)
    d = ev.stratified_d(good)
    assert d > 0.05
    p_up, p_low = ev.permutation_test(good, n_perm=2000)
    assert p_up <= ev.ALPHA and p_low > 0.5
    # 没有效应：不显著
    p_up, _ = ev.permutation_test(_strata(2, 0.0), n_perm=2000)
    assert p_up > ev.ALPHA
    # 反方向（否决的反而更好）：下侧显著
    _, p_low = ev.permutation_test(_strata(3, -0.10), n_perm=2000)
    assert p_low <= ev.ALPHA
    # 权重：否决数 × 放行数 / 事件数；只有一种结论的日子不参与
    one = [(np.array([0.0, 0.1]), np.array([True, False])), (np.array([0.5, 0.5]), np.array([False, False]))]
    assert ev.stratified_d(one) == pytest.approx(0.1)
    assert ev.stratified_d([]) is None


def test_dedupe_repeated_vetoes_and_cross_account():
    panel = make_panel({"A": [10.0] * 60, "B": [10.0] * 60})
    d = panel.dates
    events = [
        Event(1, 31, "A", d[1], d[2], True),
        Event(1, 31, "A", d[6], d[7], True),     # 20 个交易日内再次否决：不计
        Event(1, 31, "A", d[30], d[31], True),   # 距上一次否决（第 6 日）超过 20 个交易日：计入
        Event(2, 32, "A", d[6], d[7], True),     # 另一个账户第一次否决：计入
        Event(1, 31, "B", d[1], d[2], False),
        Event(2, 32, "B", d[1], d[2], False),    # 同一股票同一天两个账户：只算一次
        Event(1, 31, "B", d[40], d[41], True),
        Event(2, 32, "B", d[40], d[41], False),  # 两个账户结论不同：不计入
    ]
    kept, conflicts = ev.dedupe(events, panel)
    assert [(e.account_id, e.symbol, panel.index[e.decided]) for e in kept] == [
        (31, "A", 1), (31, "B", 1), (32, "A", 6), (31, "A", 30)]
    assert conflicts == 1


def test_attach_excess_uses_open_to_close_and_complete_windows():
    T = 40
    a = [10.0] * T
    opens = {"A": [10.0] * T}
    opens["A"][5] = 8.0                         # 成交日开盘 8 元
    bench = [100.0] * 4 + [110.0] * (T - 4)    # 成交日前一天（第 4 日）收盘 110
    bench[24] = 121.0                           # 第 20 个交易日（第 24 日）收盘
    panel = make_panel({"A": a, "B": [5.0] * T}, opens=opens, bench=bench)
    d = panel.dates
    e1 = Event(1, 31, "A", d[4], d[5], True)
    e2 = Event(1, 31, "B", d[30], d[31], False)        # 窗口超过截止日：不计入
    e3 = Event(1, 31, "ZZZ", d[4], d[5], False)
    skipped = ev.attach_excess([e1, e2, e3], panel, cutoff=30)
    assert e1.excess == pytest.approx(10 / 8 - 1 - (121 / 110 - 1))
    assert e2.excess is None and skipped == {"incomplete": 1, "no_open": 0, "unknown_symbol": 1}


@pytest.mark.parametrize(("look", "n", "d", "p_up", "p_low", "delta", "want"), [
    (120, 25, 0.05, 0.01, 0.99, 0.003, "有效"),
    (120, 25, 0.05, 0.01, 0.99, -0.001, "未证明有效"),        # 否决对了，但账户没有赚到
    (120, 25, 0.02, 0.10, 0.90, 0.01, "未证明有效"),
    (120, 25, -0.05, 0.99, 0.01, 0.0, "有害"),
    (120, 25, 0.01, 0.30, 0.70, -0.025, "有害"),
    (120, 10, -0.08, None, None, -0.05, "跳过（否决事件不足）"),   # 第一次评估：样本不足不下结论
    (250, 10, 0.08, None, None, 0.01, "未证明有效（AI 很少否决，无法评估否决质量）"),
    (250, 10, None, None, None, -0.03, "有害"),
])
def test_verdict_table(look, n, d, p_up, p_low, delta, want):
    assert ev.verdict(look, n, d, p_up, p_low, delta) == want


def test_evaluate_reports_progress_only_before_first_look(monkeypatch):
    panel = make_panel({"A": [10.0] * 300}, start="2026-10-01")
    start = panel.dates[5]
    monkeypatch.setattr(ev, "collect", lambda s, pairs, st: [Event(1, 31, "A", start, panel.dates[6], True)])
    out = ev.evaluate(None, panel.dates[50], panel=panel, start=start)
    assert out["elapsed"] == 45 and out["next_look"] == 120 and "result" not in out
    assert out["looks"][0]["date"] == panel.dates[125].isoformat()


def test_evaluate_at_first_look(monkeypatch):
    rng = np.random.default_rng(7)
    T, n = 200, 20
    closes = {f"S{k}": list(10 * np.cumprod(1 + rng.normal(0, 0.01, T))) for k in range(n)}
    panel = make_panel(closes, start="2026-10-01")
    start = panel.dates[5]
    events = []
    for t in range(6, 130, 3):                  # 每 3 天一批，每批第一只被否决（之后走势与是否否决无关）
        for k in range(5):
            sym = f"S{(t + k) % n}"
            events.append(Event(1, 31, sym, panel.dates[t], panel.dates[t + 1], k == 0))
    monkeypatch.setattr(ev, "collect", lambda s, pairs, st: events)
    monkeypatch.setattr(ev, "account_delta", lambda s, pairs, end: {"delta": 0.001})
    monkeypatch.setattr(ev, "review_errors", lambda s, pairs, st: {})
    out = ev.evaluate(None, panel.dates[130], panel=panel, start=start, n_perm=500)
    r = out["result"]
    assert r["look"] == 120 and r["look_date"] == panel.dates[125].isoformat()
    # 第 120 个交易日（第 125 日）为截止：之前约 20 日内成交的事件窗口不完整，之后决策的事件不参与
    assert r["vetoes"] >= ev.MIN_VETOES and r["skipped"]["incomplete"] > 0
    assert r["verdict"] in ("未证明有效", "有效", "有害")
    assert 0 <= r["p_upper"] <= 1 and r["reference"]["veto_rate"] == pytest.approx(0.2)


def test_evaluation_only_applies_to_preregistered_portfolios(client, monkeypatch):
    """预注册写死了组合与账户编号；别的部署里编号相同的组合不显示评估。"""
    from app.db.session import get_sessionmaker
    from app.models.quant import QuantPortfolioRow

    with get_sessionmaker()() as s:
        pf = QuantPortfolioRow(name="别人的组合", members=[])
        s.add(pf)
        s.commit()
        pid = pf.id
    assert pid == 1                                          # 和预注册的组合 1 同编号
    assert client.get(f"/api/quant/portfolios/{pid}/review/evaluation").json() == {"applies": False}
    monkeypatch.setattr(ev, "evaluate", lambda s: {"stage": "stub"})   # 这里只测适用范围，不测计算
    with get_sessionmaker()() as s:                          # 账户编号也对得上时才适用
        pf = s.get(QuantPortfolioRow, pid)
        pf.follow_account_id, pf.settings = 17, {"review": {"enabled": True, "account_id": 31}}
        s.commit()
    assert client.get(f"/api/quant/portfolios/{pid}/review/evaluation").json()["applies"] is True
