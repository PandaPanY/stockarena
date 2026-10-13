"""文本信号阶段 0：公告匿名化、例行 / 有信息粗分、统计工具。"""

from __future__ import annotations

import pytest

from app.textsignal.announcements import anonymize, is_material
from app.textsignal.phase0 import _hit, _kappa, _parse, _spearman


@pytest.mark.parametrize(("title", "name", "expected"), [
    ("招商银行：2018年年度报告", "招商银行", "某年年度报告"),
    ("五粮液关于2019年度业绩预增的公告", "五粮液", "本公司关于某年度业绩预增的公告"),
    ("招商银行股份有限公司2019年度报告", "招商银行", "本公司某年度报告"),
    ("关于控股股东四川省宜宾五粮液集团有限公司增持公司股份的公告", "五粮液", "关于控股股东某公司增持公司股份的公告"),
    ("关于收购北京星辰科技有限公司100%股权的公告", "x", "关于收购某公司100%股权的公告"),
    ("独立董事提名人声明与承诺（鲁篱）", "五粮液", "独立董事提名人声明与承诺（某人）"),
    ("第六届董事会2024年第1次会议决议公告", "x", "第某届董事会某年第1次会议决议公告"),
    ("*ST康美关于收到600518相关立案调查通知书的公告", "*ST康美", "本公司关于收到******相关立案调查通知书的公告"),
    ("中国国际航空股份有限公司关于控股股东自愿承诺不减持公司股份的公告", "中国国航",
     "本公司关于控股股东自愿承诺不减持公司股份的公告"),
    ("关于对富士康工业互联网股份有限公司问询函的回复", "工业富联", "关于对某公司问询函的回复"),
    ("2022年度向特定对象发行A股股票募集说明书（申报稿）", "x", "某年度向特定对象发行A股股票募集说明书（申报稿）"),
    ("普华永道中天会计师事务所（特殊普通合伙）专项意见", "x", "普华永道中天会计师事务所（特殊普通合伙）专项意见"),
])
def test_anonymize(title, name, expected):
    assert anonymize(title, name, "000858.SZ") == expected


def test_is_material():
    assert is_material("关于2023年半年度业绩预增的公告")
    assert is_material("关于回购股份的董事会决议公告")        # 强关键词优先于例行格式
    assert is_material("关于签订重大合同的公告")
    assert not is_material("公司章程（2022年4月）")
    assert not is_material("第六届董事会第1次会议决议公告")
    assert not is_material("关于召开2023年度股东大会的通知")


def test_stat_helpers():
    assert _kappa(["a", "b", "a", "b"], ["a", "b", "a", "b"]) == 1.0
    assert _kappa(["a", "a", "b", "b"], ["a", "b", "a", "b"]) == 0.0
    assert _spearman([1, 2, 3] * 5, [1, 2, 3] * 5) == pytest.approx(1.0)
    assert _hit(["招行", "招商银行"], "招商银行")
    assert _hit(["五粮液"], "五 粮 液")
    assert not _hit(["茅台"], "五粮液")
    assert not _hit([], "五粮液")
    assert _parse('说明 {"items": [{"id": "c1", "type": "回购"}]} 结束') == [{"id": "c1", "type": "回购"}]


def test_phase1_factor_window_and_dates():
    import numpy as np

    from app.textsignal.phase1 import WINDOW, build_factor
    from tests.quant.test_backtest import make_panel

    p = make_panel({"A": [10.0] * 40, "B": [10.0] * 40}, start="2026-01-05")   # 2026-01-05 是周一
    events = [{"id": "e1", "symbol": "A", "date": "2026-01-10"},     # 周六 → 记在周五（第 4 个交易日）
              {"id": "e2", "symbol": "A", "date": "2026-01-12"},     # 周一当天
              {"id": "e3", "symbol": "C", "date": "2026-01-12"}]     # 不在行情矩阵中
    scores = {"e1": {"direction": 2, "type": "回购"}, "e2": {"direction": -1}, "e3": {"direction": 2}}
    f, placed = build_factor(p, events, scores)
    assert [e["i"] for e in placed] == [4, 5]
    assert f[3, 0] == 0 and f[4, 0] == 2 and f[5, 0] == 1
    assert f[4 + WINDOW - 1, 0] == 1 and f[4 + WINDOW, 0] == -1 and f[5 + WINDOW, 0] == 0   # 20 个交易日后移出
    assert np.all(f[:, 1] == 0)


def test_phase1_ic_uses_tradable_returns():
    import numpy as np

    from app.textsignal.phase1 import ic_test
    from tests.quant.test_backtest import make_panel

    rng = np.random.default_rng(1)
    n, T = 40, 300
    drift = np.linspace(-0.002, 0.002, n)                  # 因子越高，之后涨得越多
    closes = {f"S{k}": list(10 * np.cumprod(1 + drift[k] + rng.normal(0, 0.001, T))) for k in range(n)}
    p = make_panel(closes, start="2020-01-01")
    factor = np.tile(drift, (T, 1))
    good = ic_test(p, factor, p.dates[150], p.dates[-1])
    bad = ic_test(p, -factor, p.dates[150], p.dates[-1])
    assert good["ic"] > 0.8 and good["t"] > 2 and bad["ic"] < -0.8
