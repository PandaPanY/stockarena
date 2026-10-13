"""全时点股票池：成分快照 → 区间、最新快照之后以当前名单为准、Baostock 数据转换、回测中退市清算、股票池包含历史成分。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import numpy as np
import pytest

from app.market.providers import baostock_cn as bsq
from app.quant import pit
from tests.quant.test_backtest import make_panel, spec


def test_intervals_from_snapshots():
    d = date
    snaps = [(d(2018, 1, 2), {"A": "甲", "B": "乙"}),
             (d(2018, 6, 11), {"A": "甲", "C": "丙"}),          # B 被剔除，C 纳入
             (d(2018, 12, 17), {"A": "甲", "B": "乙", "C": "丙"}),  # B 重新纳入
             (d(2019, 1, 28), {"A": "甲", "B": "乙", "C": "丙"})]  # 最新快照：之后以当前名单为准
    iv = pit.intervals(snaps, current={"A", "D"})
    assert iv["A"] == [(d(2018, 1, 2), None)]                                    # 一直在：前后合并
    assert iv["B"] == [(d(2018, 1, 2), d(2018, 6, 11)), (d(2018, 12, 17), d(2019, 1, 28))]
    assert iv["C"] == [(d(2018, 6, 11), d(2019, 1, 28))]                         # 不在当前名单：截止到最新快照
    assert iv["D"] == [(d(2019, 1, 28), None)]                                   # 当前名单里新进来的
    assert pit.intervals([], {"A"}) == {}


def test_symbol_codes():
    assert bsq.to_symbol("sh.600000") == "600000.SH" and bsq.to_code("000001.SZ") == "sz.000001"


class _RS:
    def __init__(self, rows):
        self.rows, self.k, self.error_code, self.error_msg = rows, -1, "0", ""

    def next(self):
        self.k += 1
        return self.k < len(self.rows)

    def get_row_data(self):
        return self.rows[self.k]


def test_baostock_daily_bars_and_factors():
    fake = SimpleNamespace(
        query_history_k_data_plus=lambda *a, **k: _RS([
            ["2018-12-03", "2.0", "2.1", "1.9", "2.0", "2.0", "1000", "2000", "1"],
            ["2018-12-04", "2.0", "2.0", "2.0", "2.0", "2.0", "0", "0", "0"],          # 停牌：不返回
            ["2018-12-05", "1.0", "1.1", "0.9", "1.0", "1.0", "500", "500", "1"]]),
        query_adjust_factor=lambda **k: _RS([["sz.000979", "2018-12-05", "0.5", "2.0", "0.5"],
                                             ["sz.000979", "2015-05-29", "0.7", "1.5", "0.7"]]))
    bars = bsq.daily_bars(fake, "000979.SZ", date(2018, 12, 1), date(2018, 12, 31))
    assert [(b.date.day, b.close, b.adj_factor) for b in bars] == [
        (3, Decimal("2.0"), Decimal("1.5")), (5, Decimal("1.0"), Decimal("2.0"))]   # 后复权价连续：2×1.5 = 1×2 + 1


def test_baostock_send_msg_fails_fast_on_closed_connection(monkeypatch):
    import baostock.common.context as context

    class ClosedSocket:                      # 服务器已断开：recv 一直返回空（原版在这里死循环）
        def settimeout(self, t): ...
        def sendall(self, b): ...
        def recv(self, n):
            return b""

    monkeypatch.setattr(context, "default_socket", ClosedSocket(), raising=False)
    before = bsq._failed
    assert bsq._send_msg("x") is None and bsq._failed == before + 1


def test_baostock_relogin_and_retry_after_comm_failure(monkeypatch):
    calls = {"query": 0, "login": 0}

    def query(**k):
        calls["query"] += 1
        if calls["query"] == 1:                # 第一次：翻页时通信失败，结果被截断
            bsq._failed += 1
            return _RS([["2018-01-02", "sh.600000", "浦发银行"]])
        return _RS([["2018-01-02", "sh.600000", "浦发银行"], ["2018-01-02", "sz.000001", "平安银行"]])

    monkeypatch.setattr(bsq, "_relogin", lambda bs: calls.__setitem__("login", calls["login"] + 1))
    fake = SimpleNamespace(query_hs300_stocks=query)
    eff, members = bsq.snapshot(fake, "hs300", date(2018, 1, 31))
    assert set(members) == {"600000.SH", "000001.SZ"} and calls == {"query": 2, "login": 1}

    def bad(**k):                              # 数据本身的错误：不重试
        rs = _RS([])
        rs.error_code, rs.error_msg = "10004011", "代码错误"
        return rs

    with pytest.raises(bsq.BaostockError, match="代码错误"):
        bsq.stock_basic(SimpleNamespace(query_stock_basic=bad), "600000.SH")
    assert calls["login"] == 1


def test_backtest_liquidates_delisted_holding():
    from app.quant.backtest import run_backtest
    from app.quant.spec import parse_spec

    closes = {"A.SH": [10.0 + 0.1 * i for i in range(40)], "B.SH": [10.0] * 40}
    closes["A.SH"][25:] = [float("nan")] * 15          # 第 25 日起 A 没有行情（退市）
    panel = make_panel(closes)
    panel.delisted = np.array([25, len(panel.dates)])
    sp = parse_spec(spec(selection={"top_n": 1}, rebalance={"every_days": 10, "min_trade_pct": 0}))
    res = run_backtest(sp, panel, panel.dates[0], panel.dates[-1])
    sells = [t for t in res.trades if t.side == "sell" and t.symbol == "A.SH" and t.note.startswith("退市")]
    assert len(sells) == 1 and sells[0].date == panel.dates[25]
    assert sells[0].price == pytest.approx(closes["A.SH"][24])                 # 按最后交易日收盘价
    assert not [t for t in res.trades if t.symbol == "A.SH" and t.date > panel.dates[25]]
    assert res.final_weights.get("A.SH", 0) == 0
    assert np.isfinite(res.nav[-1]) and res.nav[-1] > 0


def test_universe_members_includes_history(settings, monkeypatch):
    from app.db.migrate import upgrade_to_head
    from app.db.session import get_engine, get_sessionmaker
    from app.models.market import IndexMemberRow
    from app.quant import data

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    upgrade_to_head(settings.resolved_database_url)
    get_engine()
    with get_sessionmaker()() as s:
        for sym, name, out in (("600001.SH", "邯郸钢铁", date(2019, 1, 1)), ("600000.SH", "浦发银行", None)):
            s.add(IndexMemberRow(index_code="000300h", symbol=sym, name=name, in_date=date(2016, 1, 1), out_date=out))
        s.commit()
    monkeypatch.setattr(pit, "_load_cache", lambda: {"basic": {"600001.SH": {"delisted": "2019-06-30"}}})
    from app.market.types import UniverseSpec

    fake_spec = UniverseSpec(name="h", title="h", market="CN", benchmark="000300.SH", point_in_time="000300",
                             history=True)

    class FakeResolver:
        specs = {"h": fake_spec}

        def resolve(self, name):
            from app.market.universe import UniverseResolver

            return [UniverseResolver._cn_instrument({"symbol": "600000.SH", "name": "浦发银行"})]

    monkeypatch.setattr(data, "universe_resolver", lambda: FakeResolver())
    insts, bench, _ = data.universe_members("h")
    by = {i.symbol: i for i in insts}
    assert set(by) == {"600000.SH", "600001.SH"} and by["600001.SH"].delisted == date(2019, 6, 30)
    assert by["600000.SH"].delisted is None and bench.symbol == "000300.SH"
    assert data.membership_code("h") == "000300h"


def test_switch_active_to_history_universe(settings):
    from app.db.migrate import upgrade_to_head
    from app.db.session import get_engine, get_sessionmaker
    from app.models.quant import QuantStrategyRow
    from app.quant.pit_compare import _pair

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    upgrade_to_head(settings.resolved_database_url)
    get_engine()
    with get_sessionmaker()() as s:
        for name, uni, status in (("甲", "cn_csi300_pit_etf", "active"), ("乙", "cn_csi800_pit_etf", "active"),
                                  ("丙", "cn_csi300_pit_etf", "archived"), ("丁", "cn_sse50_etf", "active")):
            s.add(QuantStrategyRow(name=name, style="x", universe_name=uni, benchmark="000300.SH", settings={},
                                   status=status))
        s.flush()
        assert len(pit.switch_active(s)) == 2
        unis = {st.name: st.universe_name for st in s.query(QuantStrategyRow)}
    assert unis == {"甲": "cn_csi300_hist", "乙": "cn_csi800_hist", "丙": "cn_csi300_pit_etf", "丁": "cn_sse50_etf"}
    # 切换之后对比脚本仍能找到半时点 / 全时点两个股票池
    assert _pair("cn_csi300_hist") == _pair("cn_csi300_pit_etf") == (("half", "cn_csi300_pit_etf"),
                                                                       ("full", "cn_csi300_hist"))
