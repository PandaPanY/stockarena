"""AI 交易复核的有效性评估——严格按预注册 docs/prereg/2026-10-08-ai-review.md 计算，不得反过来修改预注册。

- 样本：AI 复核账户每次决策中的每只候选股票（复核事件）；同一账户 20 个交易日内重复否决同一只股票只算第一次；
  同一股票、同一决策日在两个账户中只算一次（结论不同的不计入，单独报告）。
- 之后的表现：成交日开盘 → 第 20 个交易日收盘（复权），减沪深300 同期（前一日收盘 → 第 20 个交易日收盘）。
- P1 否决质量：同一决策日内「放行平均超额 − 否决平均超额」的加权平均 D（权重 否决数×放行数/事件数），
  分日置换检验（每天内打乱标签，10,000 次，种子 20261008）。
- P2 账户结果：两个 AI 账户合计 − 两个规则账户合计的累计收益 ΔR。
- 只在第 120 个交易日（否决事件 ≥ 20 时）与第 250 个交易日评估，各取 p ≤ 0.025。

命令行：`uv run python -m app.quant.review_eval [--as-of YYYY-MM-DD]`（输出 JSON）。
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DecisionRow, NavSnapshotRow, RoundRow
from app.quant.panel import Panel

START = date(2026, 10, 8)                  # 第 0 个交易日（AI 复核账户第一次决策）
PAIRS = ((1, 17, 31), (2, 23, 32))         # （组合，规则账户，AI 复核账户）
LOOKS = (120, 250)
MIN_VETOES = 20
HORIZON = 20
DEDUP_DAYS = 20
ALPHA = 0.025
HARM_DELTA = -0.02
N_PERM = 10_000
SEED = 20261008


@dataclass
class Event:
    portfolio_id: int
    account_id: int
    symbol: str
    decided: date              # 决策日（收盘后）
    fill: date | None          # 成交日（下一交易日开盘）
    veto: bool
    risk: int | None = None
    excess: float | None = None


# ---------------------------------------------------------------- 样本


def collect(s: Session, pairs=PAIRS, start: date = START) -> list[Event]:
    """读取 AI 复核账户的全部复核事件（决策日在 start 及以后）。"""
    out: list[Event] = []
    for pid, _rule_id, ai_id in pairs:
        rows = s.execute(select(DecisionRow, RoundRow).join(RoundRow, DecisionRow.round_id == RoundRow.id)
                         .where(DecisionRow.account_id == ai_id, RoundRow.data_date >= start)
                         .order_by(RoundRow.data_date)).all()
        for dec, rnd in rows:
            for it in ((dec.parsed or {}).get("review") or {}).get("items", []):
                risk = it.get("risk")
                out.append(Event(pid, ai_id, str(it["symbol"]), rnd.data_date, rnd.fill_date,
                                 it.get("decision") == "veto", risk if isinstance(risk, int) else None))
    return out


def review_errors(s: Session, pairs=PAIRS, start: date = START) -> dict:
    """复核失败（按规则执行）的批次数与决策数，参考用。"""
    n_dec = n_err = 0
    for _pid, _rule_id, ai_id in pairs:
        for dec, _rnd in s.execute(select(DecisionRow, RoundRow).join(RoundRow, DecisionRow.round_id == RoundRow.id)
                                   .where(DecisionRow.account_id == ai_id, RoundRow.data_date >= start)).all():
            rv = (dec.parsed or {}).get("review") or {}
            if rv.get("candidates"):
                n_dec += 1
                n_err += len(rv.get("errors") or [])
    return {"decisions_with_candidates": n_dec, "error_batches": n_err}


def dedupe(events: list[Event], panel: Panel) -> tuple[list[Event], int]:
    """去重：①同一账户 20 个交易日内重复否决同一只股票只算第一次；②同一股票同一决策日跨账户只算一次。
    返回（保留的事件，跨账户结论不同而不计入的组数）。"""
    kept: list[Event] = []
    last_veto: dict[tuple[int, str], int] = {}
    for e in sorted(events, key=lambda x: (x.decided, x.account_id, x.symbol)):
        i = panel.locate(e.decided, "before")
        if e.veto:
            key = (e.account_id, e.symbol)
            prev = last_veto.get(key)
            last_veto[key] = i
            if prev is not None and i - prev <= DEDUP_DAYS:
                continue
        kept.append(e)
    groups: dict[tuple[str, date], list[Event]] = defaultdict(list)
    for e in kept:
        groups[(e.symbol, e.decided)].append(e)
    out, conflicts = [], 0
    for g in groups.values():
        if len({e.veto for e in g}) > 1:
            conflicts += 1
            continue
        out.append(g[0])
    return sorted(out, key=lambda x: (x.decided, x.symbol)), conflicts


def attach_excess(events: list[Event], panel: Panel, cutoff: int, horizon: int = HORIZON) -> dict:
    """计算每个事件之后 horizon 个交易日的超额收益；窗口须在 cutoff（含）之前结束。返回不计入的原因计数。"""
    skipped = {"incomplete": 0, "no_open": 0, "unknown_symbol": 0}
    adj_open = panel.open * panel.factor
    for e in events:
        e.excess = None
        j = panel.col.get(e.symbol)
        if j is None:
            skipped["unknown_symbol"] += 1
            continue
        if e.fill is None:
            skipped["incomplete"] += 1
            continue
        i = panel.locate(e.fill, "after")
        k = i + horizon - 1
        if i >= len(panel.dates) or k > cutoff or i < 1:
            skipped["incomplete"] += 1
            continue
        o = adj_open[i, j]
        if not np.isfinite(o) or o <= 0:
            skipped["no_open"] += 1
            continue
        ret = panel.adj_close[k, j] / o - 1
        bench = panel.bench[k] / panel.bench[i - 1] - 1
        if np.isfinite(ret) and np.isfinite(bench):
            e.excess = float(ret - bench)
        else:
            skipped["no_open"] += 1
    return skipped


# ---------------------------------------------------------------- 统计量


def _strata(events: list[Event]) -> list[tuple[np.ndarray, np.ndarray]]:
    """按决策日分组：（超额收益，是否否决），只保留同一天既有否决又有放行的日子。"""
    by_day: dict[date, list[Event]] = defaultdict(list)
    for e in events:
        if e.excess is not None:
            by_day[e.decided].append(e)
    out = []
    for g in by_day.values():
        x = np.array([e.excess for e in g], dtype=float)
        v = np.array([e.veto for e in g], dtype=bool)
        if 0 < v.sum() < len(v):
            out.append((x, v))
    return out


def stratified_d(strata: list[tuple[np.ndarray, np.ndarray]]) -> float | None:
    """D = Σ w_t (放行均值 − 否决均值) / Σ w_t，w_t = 否决数 × 放行数 / 事件数。"""
    num = den = 0.0
    for x, v in strata:
        nv, n = int(v.sum()), len(v)
        if nv in (0, n):           # 只有一种结论的日子没有同日对比
            continue
        w = nv * (n - nv) / n
        num += w * (x[~v].mean() - x[v].mean())
        den += w
    return num / den if den else None


def permutation_test(strata, n_perm: int = N_PERM, seed: int = SEED) -> tuple[float | None, float | None]:
    """分日置换检验：每天内打乱否决 / 放行标签（每天否决数不变）。返回（上侧 p：D_perm ≥ D，下侧 p：D_perm ≤ D）。"""
    d = stratified_d(strata)
    if d is None:
        return None, None
    rng = np.random.default_rng(seed)
    ge = le = 0
    for _ in range(n_perm):
        dp = stratified_d([(x, rng.permutation(v)) for x, v in strata])
        ge += dp >= d - 1e-12
        le += dp <= d + 1e-12
    return ge / n_perm, le / n_perm


def account_delta(s: Session, pairs=PAIRS, end: date | None = None) -> dict:
    """ΔR：AI 账户合计 − 规则账户合计的累计收益（从 AI 账户首次有净值的那天到 end）。"""
    def navs(acc_id: int) -> dict[date, float]:
        q = select(NavSnapshotRow.date, NavSnapshotRow.nav).where(NavSnapshotRow.account_id == acc_id)
        if end is not None:
            q = q.where(NavSnapshotRow.date <= end)
        return {d: float(n) for d, n in s.execute(q)}

    rule = [navs(r) for _p, r, _a in pairs]
    ai = [navs(a) for _p, _r, a in pairs]
    firsts = [min(x) for x in ai if x]
    if len(firsts) < len(pairs):
        return {"delta": None}
    common = sorted(set.intersection(*(set(x) for x in rule + ai)))
    common = [d for d in common if d >= max(firsts)]
    if len(common) < 2:
        return {"delta": None}
    d0, d1 = common[0], common[-1]
    ai_r = sum(x[d1] for x in ai) / sum(x[d0] for x in ai) - 1
    rule_r = sum(x[d1] for x in rule) / sum(x[d0] for x in rule) - 1
    each = [{"portfolio_id": p, "ai": a[d1] / a[d0] - 1, "rule": r[d1] / r[d0] - 1}
            for (p, _ri, _ai), r, a in zip(pairs, rule, ai, strict=True)]
    return {"delta": ai_r - rule_r, "ai": ai_r, "rule": rule_r, "start": d0.isoformat(), "end": d1.isoformat(),
            "each": each}


def verdict(look: int, n_veto: int, d: float | None, p_up: float | None, p_low: float | None,
            delta: float | None) -> str:
    """按预注册的判定表：第一次评估否决事件不足则跳过（不下任何结论）；其余情况「有害」优先。"""
    if look == LOOKS[0] and n_veto < MIN_VETOES:
        return "跳过（否决事件不足）"
    harmful = (d is not None and d < 0 and p_low is not None and p_low <= ALPHA) or \
        (delta is not None and delta <= HARM_DELTA)
    if harmful:
        return "有害"
    if n_veto < MIN_VETOES:
        return "未证明有效（AI 很少否决，无法评估否决质量）"
    if d is not None and d > 0 and p_up is not None and p_up <= ALPHA and delta is not None and delta >= 0:
        return "有效"
    return "未证明有效"


# ---------------------------------------------------------------- 汇总


def _reference(events: list[Event]) -> dict:
    """参考信息（不参与判定）：否决率、各风险等级之后的平均超额、各组合的 D。"""
    done = [e for e in events if e.excess is not None]
    by_risk: dict[str, list[float]] = defaultdict(list)
    for e in done:
        by_risk[str(e.risk) if e.risk is not None else "未知"].append(e.excess)
    per_pf = {}
    for pid in sorted({e.portfolio_id for e in events}):
        sub = [e for e in done if e.portfolio_id == pid]
        per_pf[pid] = {"events": len(sub), "vetoes": sum(e.veto for e in sub), "d": stratified_d(_strata(sub))}
    return {
        "veto_rate": (sum(e.veto for e in events) / len(events)) if events else None,
        "by_risk": {k: {"n": len(v), "mean_excess": float(np.mean(v))} for k, v in sorted(by_risk.items())},
        "per_portfolio": per_pf,
    }


def _look_date(panel: Panel, i0: int, k: int) -> dict:
    """第 k 个交易日的日期；还没到时估算：每年约 250 个交易日、261 个工作日，按比例折成工作日
    （节假日集中的时段如春节前后会有几天误差）。"""
    if i0 + k < len(panel.dates):
        return {"day": k, "date": panel.dates[i0 + k].isoformat(), "estimated": False}
    rest = i0 + k - (len(panel.dates) - 1)
    d = np.busday_offset(np.datetime64(panel.dates[-1]), round(rest * 261 / 250), roll="forward")
    return {"day": k, "date": str(d), "estimated": True}


def evaluate(s: Session, as_of: date | None = None, *, pairs=PAIRS, start: date = START,
             panel: Panel | None = None, n_perm: int = N_PERM) -> dict:
    """评估进度；到达评估时点时按预注册计算判定。评估时点之前不计算 P1 / P2（只给进度）。"""
    if panel is None:
        from app.models.quant import QuantPortfolioRow
        from app.quant.live import live_panel
        from app.quant.portfolio import members_of

        members = members_of(s, s.get(QuantPortfolioRow, pairs[0][0]))
        panel, _ = live_panel(s, members[0].strategy.universe_name, as_of or date.today())
    as_of = as_of or panel.dates[-1]
    i0 = panel.locate(start, "after")
    last = min(panel.locate(as_of, "before"), len(panel.dates) - 1)
    elapsed = max(last - i0, 0)
    events_raw = collect(s, pairs, start)
    out: dict = {
        "start": start.isoformat(), "as_of": panel.dates[last].isoformat(), "elapsed": elapsed,
        "looks": [_look_date(panel, i0, k) for k in LOOKS],
        "events": len(events_raw), "vetoes": sum(e.veto for e in events_raw),
    }
    reached = [k for k in LOOKS if elapsed >= k]
    if not reached:
        out["next_look"] = next(k for k in LOOKS if elapsed < k)
        return out
    look = reached[-1]
    cutoff = i0 + look
    events, conflicts = dedupe([e for e in events_raw if e.decided <= panel.dates[cutoff]], panel)
    skipped = attach_excess(events, panel, cutoff)
    done = [e for e in events if e.excess is not None]
    n_veto = sum(e.veto for e in done)
    strata = _strata(events)
    d = stratified_d(strata)
    p_up, p_low = permutation_test(strata, n_perm) if n_veto >= MIN_VETOES or look == LOOKS[-1] else (None, None)
    acct = account_delta(s, pairs, panel.dates[cutoff])
    out["result"] = {
        "look": look, "look_date": panel.dates[cutoff].isoformat(),
        "events": len(done), "vetoes": n_veto, "approves": len(done) - n_veto,
        "cross_account_conflicts": conflicts, "skipped": skipped, "strata": len(strata),
        "d": d, "p_upper": p_up, "p_lower": p_low, "account": acct,
        "verdict": verdict(look, n_veto, d, p_up, p_low, acct.get("delta")),
        "reference": {**_reference(events), **review_errors(s, pairs, start)},
    }
    return out


def main() -> None:
    from app.db.session import get_sessionmaker

    ap = argparse.ArgumentParser(description="AI 交易复核有效性评估（按预注册）")
    ap.add_argument("--as-of", type=date.fromisoformat, default=None)
    args = ap.parse_args()
    with get_sessionmaker()() as s:
        print(json.dumps(evaluate(s, args.as_of), ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
