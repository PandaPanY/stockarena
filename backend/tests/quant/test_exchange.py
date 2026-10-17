"""策略导入导出：导出冠军规则与测试历史，导入时重新回测并继承测试次数（不能靠导出再导入洗掉）。"""

from __future__ import annotations

import yaml

from app.db.session import get_sessionmaker
from app.models.quant import QuantStrategyRow
from app.quant import exchange, research, service
from tests.quant.test_lab import _create, lab  # noqa: F401


async def _researched(pid) -> int:
    sid = _create(provider_id=pid)
    with get_sessionmaker()() as s:
        run = research.start_run(s, s.get(QuantStrategyRow, sid))
        s.commit()
        rid = run.id
    await research.run_research(rid)
    return sid


async def test_export_import_roundtrip_keeps_test_history(lab, client):  # noqa: F811
    _llm, pid = lab
    sid = await _researched(pid)
    r = client.get(f"/api/quant/strategies/{sid}/export")
    assert r.status_code == 200 and ".stockarena.yaml" in r.headers["content-disposition"]
    text = r.text
    assert text.startswith("# StockArena 策略文件")
    data = yaml.safe_load(text)
    assert data["format"] == exchange.FORMAT and data["provenance"]["version"] == 2
    trials = data["provenance"]["tests"]["trials"]
    assert trials >= 2 and len(data["provenance"]["tests"]["trial_scores"]) == trials

    out = client.post("/api/quant/strategies/import", json={"text": text, "name": "导入的测试策略"}).json()
    assert out["name"] == "导入的测试策略" and out["mine"] is True
    with get_sessionmaker()() as s:
        st = s.get(QuantStrategyRow, out["id"])
        assert st.origin["kind"] == "import" and st.origin["version_number"] == 2
        # 继承：DSR 的尝试次数 = 文件中的尝试 + 导入后的初始版本
        assert len(service.trial_scores(s, st)) == trials + 1
        assert service.prior_tests(s, st, st.champion_id) == data["provenance"]["tests"]["prior_tests"]


def test_import_rejects_bad_files(lab, client):  # noqa: F811
    bad = client.post("/api/quant/strategies/import", json={"text": "format: other\nspec: {}\n"})
    assert bad.status_code == 400 and "不是 StockArena 策略文件" in bad.json()["detail"]
    broken = client.post("/api/quant/strategies/import",
                         json={"text": f"format: {exchange.FORMAT}\nspec:\n  factors:\n    - name: magic\n"})
    assert broken.status_code == 400 and "规则不合法" in broken.json()["detail"]


def test_import_warns_without_history_and_unknown_universe(lab, client):  # noqa: F811
    from tests.quant.test_lab import GOOD, SETTINGS

    text = exchange.dumps({"format": exchange.FORMAT, "name": "手写策略", "universe": "no_such_pool",
                           "settings": SETTINGS, "spec": GOOD})
    out = client.post("/api/quant/strategies/import", json={"text": text}).json()
    assert any("没有股票池" in w for w in out["warnings"]) and any("没有测试历史" in w for w in out["warnings"])


def test_community_files_parse():
    """community/strategies 里的示例文件必须能通过导入校验，并带测试历史。"""
    from pathlib import Path

    files = sorted((Path(__file__).parents[3] / "community" / "strategies").glob("*.stockarena.yaml"))
    assert files
    for f in files:
        data = exchange.parse(f.read_text(encoding="utf-8"))
        scores, prior = exchange._trials(data)
        assert scores, f.name
