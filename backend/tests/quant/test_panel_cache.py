"""行情矩阵缓存：同一个矩阵同时只加载一次（多个请求同时到达时，后到的等先到的加载完直接复用）。"""

from __future__ import annotations

import threading
import time
from datetime import date
from decimal import Decimal

import pytest

from app.market.types import Instrument
from app.quant import data

BENCH = Instrument("000300.SH", "CN", "沪深300", "index", "index", tick=Decimal("0.01"))


class _Session:
    def scalar(self, _stmt):
        return date(2026, 10, 16)


def test_concurrent_requests_load_panel_once(monkeypatch):
    calls = []

    def slow_load(s, universe_name, insts, bench, start, end):
        calls.append(universe_name)
        time.sleep(0.3)
        return object()

    monkeypatch.setattr(data, "universe_members", lambda name: ([], BENCH, "CN"))
    monkeypatch.setattr(data, "_load_panel", slow_load)
    data._panels.clear()
    results = []
    threads = [threading.Thread(target=lambda: results.append(
        data.get_panel(_Session(), "cn_test", date(2018, 1, 1), date(2026, 10, 16)))) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1 and len(results) == 5 and all(r is results[0] for r in results)
    # 不同的矩阵各自加载
    data.get_panel(_Session(), "cn_other", date(2018, 1, 1), date(2026, 10, 16))
    assert calls == ["cn_test", "cn_other"] and not data._loading
    data._panels.clear()


def test_failed_load_is_not_cached(monkeypatch):
    attempts = []

    def flaky(s, universe_name, insts, bench, start, end):
        attempts.append(1)
        if len(attempts) == 1:
            raise ValueError("缺少成分记录")
        return object()

    monkeypatch.setattr(data, "universe_members", lambda name: ([], BENCH, "CN"))
    monkeypatch.setattr(data, "_load_panel", flaky)
    data._panels.clear()
    with pytest.raises(ValueError):
        data.get_panel(_Session(), "cn_test", date(2018, 1, 1), date(2026, 10, 16))
    assert data.get_panel(_Session(), "cn_test", date(2018, 1, 1), date(2026, 10, 16)) is not None
    assert len(attempts) == 2 and not data._loading
    data._panels.clear()
