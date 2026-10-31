"""研究报告《让 AI 改了几百个策略版本之后》的数据：AI 优化到底有没有用。

命令（服务器上运行，结果写入 data/research/ai_versions.json）：
  uv run python -m app.quant.ai_report [截止日期 YYYY-MM-DD]

数据库里保存的版本指标是各自生成时计算的，留出期的截止日不同，也不一定带月度收益。这里把每条策略的
每个版本按同一个截止日重新回测一次（策略自己的股票池、检验区间、所有者的交易成本），再计算：

- 漏斗：AI 提出的版本中多少通过检验、升级为冠军；未通过的原因；
- 开发期夏普与留出期夏普的秩相关（开发期越好的版本，留出期是否也越好）；
- 相对父版本（提出时的冠军）的改进：开发期提升与留出期提升是否同向；
- 每次升级在留出期是否也更好；
- 回测过拟合概率 PBO（开发期月度收益的 CSCV）与滚动选择检验（walk-forward）；
- 研究的轮数、token 与花费。

不含研究模型对比实验建的策略（名称以「模型对比」开头）和只有一个版本的策略。
"""

from __future__ import annotations

import json
import logging
import sys
from collections import Counter
from datetime import date

import numpy as np
import pandas as pd
from sqlalchemy import select

logger = logging.getLogger(__name__)

EXCLUDE_PREFIX = "模型对比"
KEYS = ("annualized_return", "sharpe", "max_drawdown")


def _spearman(a: list[float], b: list[float]) -> float | None:
    if len(a) < 5:
        return None
    x = pd.Series(a).rank().corr(pd.Series(b).rank())
    return None if pd.isna(x) else round(float(x), 3)


def _pearson(a: list[float], b: list[float]) -> float | None:
    if len(a) < 5 or np.std(a) == 0 or np.std(b) == 0:
        return None
    return round(float(np.corrcoef(a, b)[0, 1]), 3)


def evaluate_versions(st, versions, panel, cfg, end: date) -> dict[int, dict]:
    """{版本 id: {"dev": 报告, "holdout": 报告}}：同一截止日重新回测；规格已不合法的版本跳过。"""
    from app.quant.backtest import run_backtest
    from app.quant.evaluate import window_report
    from app.quant.spec import parse_spec

    out = {}
    for v in versions:
        try:
            spec = parse_spec(v.spec)
            res = run_backtest(spec, panel, cfg.train_start, end, cfg.initial_cash)
        except (ValueError, KeyError) as e:
            logger.warning("%s v%d 无法回测：%s", st.name, v.number, e)
            continue
        out[v.id] = {"dev": window_report(res, cfg.train_start, cfg.valid_end, monthly=True),
                     "holdout": window_report(res, cfg.holdout_start, end, monthly=True)}
    return out


def strategy_stats(s, st, end: date) -> dict | None:
    from app.models.quant import QuantVersionRow, ResearchRunRow
    from app.quant import costs
    from app.quant.evaluate import pbo_cscv, reject_codes, walk_forward
    from app.quant.service import panel_for, settings_of

    versions = list(s.scalars(select(QuantVersionRow).where(QuantVersionRow.strategy_id == st.id)
                              .order_by(QuantVersionRow.number)))
    if len(versions) < 2:
        return None
    cfg = settings_of(st)
    with costs.use(st.user_id):
        reps = evaluate_versions(st, versions, panel_for(s, st), cfg, end)
    sh = {vid: (r["dev"].get("sharpe"), r["holdout"].get("sharpe")) for vid, r in reps.items()}
    ok = {vid: p for vid, p in sh.items() if p[0] is not None and p[1] is not None}
    ai = [v for v in versions if v.source == "ai"]
    promoted = [v for v in ai if v.status in ("champion", "retired")]
    first = versions[0]
    champ = next((v for v in versions if v.id == st.champion_id), versions[0])

    # 相对父版本（提出时的冠军）的提升
    deltas = []
    for v in ai:
        if v.id in ok and v.parent_id in ok:
            deltas.append((ok[v.id][0] - ok[v.parent_id][0], ok[v.id][1] - ok[v.parent_id][1]))
    better_dev = [d for d in deltas if d[0] > 0]
    ups = [{"number": v.number, "dev_delta": round(ok[v.id][0] - ok[v.parent_id][0], 3),
            "holdout_delta": round(ok[v.id][1] - ok[v.parent_id][1], 3)}
           for v in promoted if v.id in ok and v.parent_id in ok]

    monthly = {v.number: dict((m[0], m[1]) for m in reps[v.id]["dev"].get("monthly") or [])
               for v in versions if v.id in reps}
    dev_df = pd.DataFrame({k: x for k, x in monthly.items() if x}).dropna()
    full = {v.number: {**monthly.get(v.number, {}),
                       **dict((m[0], m[1]) for m in reps[v.id]["holdout"].get("monthly") or [])}
            for v in versions if v.id in reps}
    runs = list(s.scalars(select(ResearchRunRow).where(ResearchRunRow.strategy_id == st.id)))
    log = [e for r in runs for e in r.log or []]
    reasons = Counter(code for v in ai if v.status == "rejected" for code in reject_codes(v.decision))

    def pick(vid):
        r = reps.get(vid)
        return {w: {k: r[w].get(k) for k in KEYS} for w in ("dev", "holdout")} if r else None

    return {
        "id": st.id, "name": st.name, "style": st.style, "status": st.status, "universe": st.universe_name,
        "windows": {"dev": [cfg.train_start.isoformat(), cfg.valid_end.isoformat()],
                    "holdout": [cfg.holdout_start.isoformat(), end.isoformat()]},
        "versions": len(versions), "evaluated": len(reps), "ai_versions": len(ai), "promotions": len(promoted),
        "initial": pick(first.id), "champion": pick(champ.id), "champion_number": champ.number,
        "rank_corr": _spearman([p[0] for p in ok.values()], [p[1] for p in ok.values()]), "rank_n": len(ok),
        "delta_n": len(deltas), "delta_corr": _pearson([d[0] for d in deltas], [d[1] for d in deltas]),
        "better_dev": len(better_dev), "better_dev_and_holdout": sum(1 for d in better_dev if d[1] > 0),
        "upgrades": ups,
        "pbo": pbo_cscv(dev_df.to_numpy()) if dev_df.shape[1] >= 4 else None, "pbo_months": int(dev_df.shape[0]),
        "walk_forward": walk_forward({k: x for k, x in full.items() if x}, first.number,
                                     holdout_year=cfg.holdout_start.year),
        "reject_reasons": dict(reasons.most_common()),
        "runs": len(runs), "rounds": len(log), "failed_rounds": sum(1 for e in log if e.get("status") != "ok"),
        "tokens_in": sum(e.get("tokens_in") or 0 for e in log),
        "tokens_out": sum(e.get("tokens_out") or 0 for e in log),
        "cost": round(sum(e.get("cost") or 0 for e in log), 2),
        "deltas": [[round(a, 4), round(b, 4)] for a, b in deltas],
        "pairs": [[round(a, 4), round(b, 4)] for a, b in ok.values()],
    }


def pooled(rows: list[dict]) -> dict:
    """合并各策略：版本数、升级、开发期提升与留出期提升的同向比例、秩相关的平均。"""
    deltas = [d for r in rows for d in r["deltas"]]
    better = [d for d in deltas if d[0] > 0]
    ups = [u for r in rows for u in r["upgrades"]]
    rc = [(r["rank_corr"], r["rank_n"]) for r in rows if r["rank_corr"] is not None]
    pbos = [r["pbo"] for r in rows if r["pbo"] is not None]
    return {
        "strategies": len(rows), "versions": sum(r["versions"] for r in rows),
        "ai_versions": sum(r["ai_versions"] for r in rows), "promotions": sum(r["promotions"] for r in rows),
        "rounds": sum(r["rounds"] for r in rows), "failed_rounds": sum(r["failed_rounds"] for r in rows),
        "tokens_in": sum(r["tokens_in"] for r in rows), "tokens_out": sum(r["tokens_out"] for r in rows),
        "cost": round(sum(r["cost"] for r in rows), 2),
        "delta_n": len(deltas), "delta_corr": _pearson([d[0] for d in deltas], [d[1] for d in deltas]),
        "better_dev": len(better), "better_dev_and_holdout": sum(1 for d in better if d[1] > 0),
        "upgrades": len(ups), "upgrades_holdout_better": sum(1 for u in ups if u["holdout_delta"] > 0),
        "rank_corr_weighted": round(sum(c * n for c, n in rc) / sum(n for _, n in rc), 3) if rc else None,
        "pbo_median": round(float(np.median(pbos)), 3) if pbos else None,
        "reject_reasons": dict(sum((Counter(r["reject_reasons"]) for r in rows), Counter()).most_common()),
    }


def run(end: date | None = None) -> dict:
    from app.db.session import get_sessionmaker
    from app.models.quant import QuantStrategyRow

    with get_sessionmaker()() as s:
        sts = [st for st in s.scalars(select(QuantStrategyRow).order_by(QuantStrategyRow.id))
               if not st.name.startswith(EXCLUDE_PREFIX)]
        end = end or date.today()
        rows = []
        for st in sts:
            r = strategy_stats(s, st, end)
            if r:
                rows.append(r)
                logger.info("完成：%s（%d 个版本）", st.name, r["versions"])
    return {"end": end.isoformat(), "strategies": rows, "pooled": pooled(rows)}


def main(argv: list[str]) -> int:
    from app.core.config import get_settings

    logging.basicConfig(level=logging.INFO)
    out = run(date.fromisoformat(argv[0]) if argv else None)
    path = get_settings().data_dir / "research" / "ai_versions.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out["pooled"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
