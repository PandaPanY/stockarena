"""A股 ETF 份额折算 / 拆分：由价格跳变识别、计入复权因子与除权事件，交易引擎按比例调整持仓（不访问网络）。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pandas as pd
import pytest
from sqlalchemy.orm import Session

from app.db.migrate import upgrade_to_head
from app.db.session import build_engine
from app.market.providers import akshare_cn
from app.market.providers.akshare_cn import AkshareCNProvider, _split_shares, fund_actions
from app.market.service import MarketDataService
from app.market.types import CorporateAction, Instrument
from app.trading.portfolio import AccountState
from app.trading.types import Position

NDX = Instrument("513100.SH", "CN", "纳指ETF", "etf", "fund", tick=Decimal("0.001"), t0=True,
                 price_limit_pct=Decimal("0.10"))
CSI1000 = Instrument("512100.SH", "CN", "中证1000ETF", "etf", "fund", tick=Decimal("0.001"),
                     price_limit_pct=Decimal("0.10"))


def _raw(closes: list[float], start: str = "2022-01-10") -> pd.DataFrame:
    dates = pd.bdate_range(start, periods=len(closes))
    return pd.DataFrame({"date": dates, "open": closes, "high": closes, "low": closes, "close": closes,
                         "volume": [1e6] * len(closes), "amount": [1e7] * len(closes)})


@pytest.mark.parametrize(("ratio", "expected"), [
    (Decimal("0.1955"), Decimal("4")),          # 1 拆 5（当天另有 -2% 涨跌）→ 取整
    (Decimal("0.5050"), Decimal("1")),          # 1 拆 2
    (Decimal("2.7627"), Decimal("-0.638035")),  # 约 2.76 份合并为 1 份：不接近整数，按推算值
    (Decimal("3.0300"), Decimal("-0.666667")),  # 3 合 1
])
def test_split_shares(ratio, expected):
    assert _split_shares(ratio) == expected


def test_fund_actions_detect_split_and_merge_dividend():
    raw = _raw([5.10, 5.192, 1.015, 1.02, 1.03])
    div_day = raw["date"][2].date()
    dividends = [CorporateAction(NDX.symbol, date(2021, 12, 1), Decimal("0.01"), source="sina.etf_dividend"),
                 CorporateAction(NDX.symbol, div_day, Decimal("0.002"), source="sina.etf_dividend")]
    acts = fund_actions(NDX, raw, dividends)
    assert [a.ex_date for a in acts] == [date(2021, 12, 1), div_day]
    split = acts[1]
    assert split.shares_per_share == Decimal("4") and split.cash_per_share == Decimal("0.002")
    assert split.source == "sina.etf_dividend+price_jump"
    # 普通涨跌（含涨停）不会被当成折算
    assert fund_actions(NDX, _raw([1.0, 1.1, 0.99, 1.08]), []) == []


class _FakeAk:
    def __init__(self, raw):
        self.raw = raw

    def fund_etf_hist_sina(self, symbol):
        return self.raw

    def fund_etf_dividend_sina(self, symbol):
        return pd.DataFrame({"日期": [], "累计分红": []})


def test_fund_bars_factor_and_prev_close_continuous(monkeypatch):
    raw = _raw([5.10, 5.192, 1.015, 1.02])
    monkeypatch.setattr(akshare_cn, "_ak", lambda: _FakeAk(raw))
    bars = AkshareCNProvider()._fund_bars(NDX, date(2022, 1, 1), date(2022, 12, 31))
    assert [b.adj_factor for b in bars][:2] == [Decimal("1"), Decimal("1")]
    assert bars[2].adj_factor == Decimal("5")
    assert bars[2].prev_close == Decimal("1.038")          # 折算后的参考价 5.192 / 5，用于涨跌停判断
    adj = [float(b.close * b.adj_factor) for b in bars]
    assert adj[2] / adj[1] == pytest.approx(1.015 * 5 / 5.192)   # 复权价连续，只剩当天真实涨跌
    acts = AkshareCNProvider().corporate_actions(NDX)
    assert len(acts) == 1 and acts[0].shares_per_share == Decimal("4")


def test_engine_adjusts_position_on_split_and_consolidation():
    st = AccountState(cash=Decimal("0"), positions={
        NDX.symbol: Position(NDX.symbol, 1000, Decimal("5.0")),
        CSI1000.symbol: Position(CSI1000.symbol, 1000, Decimal("1.0"))})
    st.apply_corporate_action(CorporateAction(NDX.symbol, date(2022, 1, 14), shares_per_share=Decimal("4")),
                              date(2022, 1, 14))
    assert st.positions[NDX.symbol].qty == 5000 and st.positions[NDX.symbol].avg_cost == Decimal("1")
    st.apply_corporate_action(CorporateAction(CSI1000.symbol, date(2022, 9, 5),
                                              shares_per_share=Decimal("-0.638040")), date(2022, 9, 5))
    pos = st.positions[CSI1000.symbol]
    assert pos.qty == 361 and pos.avg_cost * pos.qty == pytest.approx(Decimal("1000"), abs=Decimal("0.01"))


class _RepairProvider:
    market = "CN"

    def __init__(self, bars):
        self.bars = bars

    def daily_bars(self, inst, start, end):
        return [b for b in self.bars if start <= b.date <= end]


def test_refetch_bars_overwrites_cached_factors(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 't.db').as_posix()}"
    upgrade_to_head(url)
    engine = build_engine(url)
    raw = _raw([5.10, 5.192, 1.015, 1.02])
    monkeypatch.setattr(akshare_cn, "_ak", lambda: _FakeAk(raw))
    fixed = AkshareCNProvider()._fund_bars(NDX, date(2022, 1, 1), date(2022, 12, 31))
    with Session(engine) as s:
        svc = MarketDataService(s, _RepairProvider([
            *fixed[:2], *(b.__class__(**{**b.__dict__, "adj_factor": Decimal("1"), "prev_close": Decimal("5.192")
                                         if i == 0 else b.prev_close}) for i, b in enumerate(fixed[2:]))]))
        svc.upsert_instruments([NDX])
        svc.update_bars([NDX], until=date(2022, 12, 31), history_start=date(2022, 1, 1))
        assert svc.get_bars(NDX.symbol, date(2022, 1, 12), date(2022, 1, 12))[0].adj_factor == 1   # 旧口径
        svc.provider = _RepairProvider(fixed)
        result = svc.refetch_bars([NDX])
        assert result[NDX.symbol] == "ok: 4 根，修正 2 根"
        bar = svc.get_bars(NDX.symbol, date(2022, 1, 12), date(2022, 1, 12))[0]
        assert bar.adj_factor == 5 and bar.prev_close == Decimal("1.038")
    engine.dispose()
