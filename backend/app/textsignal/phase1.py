"""阶段 1：公告事件因子的一次性检验（判定标准先于结果写定（预注册文档已归档，未包含在本仓库））。

在服务器的 backend/ 目录下依次执行（可断点续跑）：

    uv run python -m app.textsignal.phase1 prepare     # 从阶段 0 下载的公告中取「可能有信息」的事件
    uv run python -m app.textsignal.phase1 anonymize   # 脱敏员（Qwen）：两步匿名化
    uv run python -m app.textsignal.phase1 score       # 打分员（Qwen）：只看匿名标题
    uv run python -m app.textsignal.phase1 audit       # 审查员（DeepSeek）：随机 400 条猜公司（判据 A）
    uv run python -m app.textsignal.phase1 dev         # 开发期 IC（判据 P1、P2）与事件研究（参考）
    uv run python -m app.textsignal.phase1 holdout     # 留出期（判据 P3），开发期通过后只运行一次

输出在 data/research/text_phase1/。
"""

from __future__ import annotations

import asyncio
import json
import math
import random
import sys
import time
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from app.core.config import get_settings
from app.textsignal import announcements as ann
from app.textsignal.phase0 import (
    PROVIDERS,
    SYS_ANON,
    SYS_CLASSIFY,
    SYS_IDENTITY,
    _hit,
    _int,
    _parse,
    _read_jsonl,
    _write_jsonl,
    finalize_anon,
)
from app.textsignal.phase0 import out_dir as phase0_dir

SEED = 20261006
BATCH = 25
CONCURRENCY = {"qwen": 4, "deepseek": 8}
WINDOW = 20                      # 因子：最近 20 个交易日的事件得分之和
HORIZON, STEP, MIN_STOCKS = 20, 20, 30
DEV = (date(2018, 1, 1), date(2024, 12, 31))
HOLDOUT_START = date(2025, 1, 1)
N_AUDIT = 400


def out_dir() -> Path:
    d = get_settings().data_dir / "research" / "text_phase1"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------- 1. 事件


def cmd_prepare() -> None:
    seen, events = set(), []
    for r in ann.read(phase0_dir() / "announcements.jsonl.gz"):
        key = (r["symbol"], r["date"], r["title"])
        if key in seen or not ann.is_material(r["title"]):
            continue
        seen.add(key)
        events.append({**r, "id": f"e{len(events)}", "anon": ann.anonymize(r["title"], r["name"], r["symbol"])})
    _write_jsonl(out_dir() / "events.jsonl", events)
    print(f"事件 {len(events)} 条，{len({e['symbol'] for e in events})} 只股票；按年："
          f"{dict(sorted(Counter(e['date'][:4] for e in events).items()))}")


def _events() -> list[dict]:
    return _read_jsonl(out_dir() / "events.jsonl")


# ---------------------------------------------------------------- 2. 模型调用（通用、可断点续跑）


async def _run(model_name: str, task: str, system: str, items: list[dict]) -> None:
    """items：[{id, text}]，按 BATCH 分批；结果追加到 raw_<task>.jsonl，已成功的批次跳过。"""
    from app.agent.runner import run_llm_json
    from app.arena.strategy import llm_config_for
    from app.db.session import get_sessionmaker
    from app.llm import calc_cost, create_client

    with get_sessionmaker()() as s:
        cfg, model, _, json_mode = llm_config_for(s, PROVIDERS[model_name], None)
    client = create_client(cfg)
    params: dict = {"temperature": 0}
    if model_name == "qwen":
        params["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
    path = out_dir() / f"raw_{task}.jsonl"
    done = {r["batch"] for r in _read_jsonl(path) if r.get("status") == "ok"}
    jobs = [(b, items[b:b + BATCH]) for b in range(0, len(items), BATCH) if b not in done]
    total, finished, t_start = len(jobs), 0, time.monotonic()
    print(f"{task}（{model_name}）：{total} 批待处理，已完成 {len(done)} 批", flush=True)
    sem, lock = asyncio.Semaphore(CONCURRENCY[model_name]), asyncio.Lock()

    async def one(b: int, chunk: list[dict]) -> None:
        nonlocal finished
        user = "\n".join(f"[{x['id']}] {x['text']}" for x in chunk)
        async with sem:
            t0 = time.monotonic()
            out = await run_llm_json(client, model=model, system=system, user=user, params=params,
                                     json_mode=json_mode, timeout_s=900, parse=_parse)
        rec = {"batch": b, "status": out.status, "error": out.error, "items": out.parsed or [], "n": len(chunk),
               "tokens_in": out.tokens_in, "tokens_out": out.tokens_out,
               "cost": float(calc_cost(cfg, out.tokens_in, out.tokens_out)), "seconds": round(time.monotonic() - t0, 1)}
        async with lock:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            finished += 1
            if finished % 20 == 0 or finished == total:
                left = (time.monotonic() - t_start) / finished * (total - finished) / 60
                print(f"  {task}: {finished}/{total} 批，预计还需 {left:.0f} 分钟", flush=True)

    await asyncio.gather(*(one(b, c) for b, c in jobs))


def _results(task: str) -> tuple[dict[str, dict], dict]:
    out: dict[str, dict] = {}
    usage = Counter()
    for r in _read_jsonl(out_dir() / f"raw_{task}.jsonl"):
        usage["calls"] += 1
        usage["seconds"] += r["seconds"]
        usage["cost"] += r["cost"]
        usage["tokens_out"] += r["tokens_out"]
        if r["status"] == "ok":
            usage["ok"] += 1
            for it in r["items"]:
                out[str(it["id"])] = it
    return out, dict(usage)


def cmd_anonymize() -> None:
    events = _events()
    asyncio.run(_run("qwen", "anonymize", SYS_ANON,
                     [{"id": e["id"], "text": f"公司简称：{e['name']}；标题：{e['title']}"} for e in events]))
    got, usage = _results("anonymize")
    src = Counter()
    for e in events:
        e["anon2"], e["anon2_src"] = finalize_anon(got.get(e["id"], {}).get("anon"), e)
        src[e["anon2_src"]] += 1
    _write_jsonl(out_dir() / "events.jsonl", events)
    print(f"两步匿名化：{dict(src)}；调用 {usage}")


def cmd_score() -> None:
    events = _events()
    if "anon2" not in events[0]:
        raise SystemExit("请先运行 anonymize")
    asyncio.run(_run("qwen", "score", SYS_CLASSIFY, [{"id": e["id"], "text": e["anon2"]} for e in events]))
    got, usage = _results("score")
    print(f"打分完成 {len(got)}/{len(events)} 条；调用 {usage}")


def cmd_audit() -> dict:
    events = _events()
    sample = random.Random(SEED).sample(events, N_AUDIT)
    asyncio.run(_run("deepseek", "audit", SYS_IDENTITY, [{"id": e["id"], "text": e["anon2"]} for e in sample]))
    got, usage = _results("audit")
    hit = sum(_hit(got.get(e["id"], {}).get("guesses"), e["name"]) for e in sample)
    single = sum(len(got.get(e["id"], {}).get("guesses") or []) == 1
                 and _hit(got.get(e["id"], {}).get("guesses"), e["name"]) for e in sample)
    years = Counter(e["date"][:4] for e in sample)
    year_hit = sum(str(got.get(e["id"], {}).get("year")) == e["date"][:4] for e in sample)
    res = {"n": len(sample), "answered": len(got), "hit": round(hit / len(sample), 4),
           "single_hit": round(single / len(sample), 4), "year_hit": round(year_hit / len(sample), 4),
           "year_base": round(max(years.values()) / len(sample), 4), "usage": usage,
           "pass": single / len(sample) <= 0.02 and hit / len(sample) <= 0.10,
           "hits": [{"name": e["name"], "anon": e["anon2"], "guesses": got.get(e["id"], {}).get("guesses")}
                    for e in sample if _hit(got.get(e["id"], {}).get("guesses"), e["name"])]}
    (out_dir() / "audit.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in res.items() if k != "hits"}, ensure_ascii=False, indent=1))
    return res


# ---------------------------------------------------------------- 3. 因子与检验


def _panel():
    from app.db.session import get_sessionmaker
    from app.quant.data import get_panel
    from app.quant.service import LabSettings

    with get_sessionmaker()() as s:
        return get_panel(s, "cn_csi300_pit_etf", LabSettings().train_start, date.today())


def build_factor(panel, events: list[dict], scores: dict[str, dict]) -> tuple[np.ndarray, list[dict]]:
    """事件得分按「公告日当天或之前最后一个交易日」记入，再取最近 WINDOW 个交易日之和。返回 (因子, 带下标的事件)。"""
    T, N = panel.shape
    daily = np.zeros((T, N))
    placed = []
    for e in events:
        j = panel.col.get(e["symbol"])
        if j is None:
            continue
        i = panel.locate(date.fromisoformat(e["date"]), "before")
        if i < 0:
            continue
        sc = scores.get(e["id"], {})
        d = _int(sc.get("direction"), -2, 2) or 0
        daily[i, j] += d
        placed.append({"i": i, "j": j, "direction": d, "type": str(sc.get("type") or "其他"),
                       "materiality": _int(sc.get("materiality"), 0, 3), "date": e["date"]})
    c = np.cumsum(daily, axis=0)
    factor = c - np.vstack([np.zeros((WINDOW, N)), c[:-WINDOW]])
    return factor, placed


def ic_test(panel, factor: np.ndarray, start: date, end: date) -> dict:
    """与因子研究页相同的选股范围与抽样；之后收益为第 i+1 日开盘 → 第 i+HORIZON 日收盘（可交易收益）。"""
    from app.quant.factor_research import _rank, _universe

    adj_open = panel.open * panel.factor
    i0, i1 = panel.locate(start, "after"), min(panel.locate(end, "before"), len(panel.dates) - 1 - HORIZON)
    ics, years = [], []
    for i in range(i0, i1 + 1, STEP):
        fwd = panel.adj_close[i + HORIZON] / adj_open[i + 1] - 1
        v = factor[i]
        m = _universe(panel, i) & panel.has_bar[i + 1] & np.isfinite(v) & np.isfinite(fwd)
        if m.sum() < MIN_STOCKS or np.ptp(v[m]) == 0:
            continue
        ic = np.corrcoef(_rank(v[m]), _rank(fwd[m]))[0, 1]
        if np.isfinite(ic):
            ics.append(float(ic))
            years.append(panel.dates[i].year)
    arr = np.array(ics)
    if len(arr) < 6:
        return {"periods": len(arr)}
    mean, std = float(arr.mean()), float(arr.std(ddof=1))
    per_year = pd.Series(ics).groupby(years).mean()
    return {"periods": len(arr), "ic": round(mean, 4), "t": round(mean / std * math.sqrt(len(arr)), 2) if std else None,
            "icir": round(mean / std * math.sqrt(252 / STEP), 2) if std else None,
            "positive": round(float((arr > 0).mean()), 3),
            "years": {int(y): round(float(x), 4) for y, x in per_year.items()}}


def event_study(panel, placed: list[dict], start: date, end: date) -> dict:
    """参考：按事件得分分组的「公告反应」与「之后漂移（可交易）」相对沪深300 的平均超额收益。"""
    adj, bench = panel.adj_close, panel.bench
    adj_open = panel.open * panel.factor
    T = len(panel.dates)
    rows: dict = {}
    for e in placed:
        d = date.fromisoformat(e["date"])
        if not (start <= d <= end):
            continue
        i, j = e["i"], e["j"]
        prev = panel.locate(d, "after") - 1          # 公告日之前最后一个交易日
        if prev < 0 or i + HORIZON >= T:
            continue
        react = adj[i, j] / adj[prev, j] - bench[i] / bench[prev] if i > prev else 0.0
        drift = adj[i + HORIZON, j] / adj_open[i + 1, j] - bench[i + HORIZON] / bench[i]
        if not (np.isfinite(react) and np.isfinite(drift)):
            continue
        rows.setdefault(e["direction"], []).append((react, drift))
    out = {}
    for k in sorted(rows):
        a = np.array(rows[k])
        se = a[:, 1].std(ddof=1) / math.sqrt(len(a)) if len(a) > 1 else None
        out[str(k)] = {"n": len(a), "reaction": round(float(a[:, 0].mean()), 4),
                       "drift": round(float(a[:, 1].mean()), 4), "drift_se": round(float(se), 4) if se else None}
    return out


def _load_factor():
    events = _events()
    scores, usage = _results("score")
    panel = _panel()
    factor, placed = build_factor(panel, events, scores)
    return panel, factor, placed, scores, usage, len(events)


def cmd_dev() -> dict:
    panel, factor, placed, scores, usage, n_events = _load_factor()
    res = ic_test(panel, factor, *DEV)
    years = res.get("years", {})
    res |= {"P1": bool(res.get("ic") and res["ic"] > 0 and (res.get("t") or 0) >= 2.0),
            "P2": sum(v > 0 for v in years.values()) >= 5 and len(years) >= 7,
            "events": n_events, "scored": len(scores), "placed": len(placed),
            "direction_dist": dict(Counter(e["direction"] for e in placed)),
            "event_study_dev": event_study(panel, placed, *DEV),
            "by_type_dev": _by_type(panel, placed, *DEV), "score_usage": usage,
            "computed_at": datetime.now(UTC).isoformat()}
    (out_dir() / "dev.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return res


def _by_type(panel, placed: list[dict], start: date, end: date) -> dict:
    """参考：只保留某一类事件时的开发期平均 IC（类型来自打分员）。"""
    out = {}
    for t, n in Counter(e["type"] for e in placed).most_common(8):
        sub = [e for e in placed if e["type"] == t]
        T, N = panel.shape
        daily = np.zeros((T, N))
        for e in sub:
            daily[e["i"], e["j"]] += e["direction"]
        c = np.cumsum(daily, axis=0)
        f = c - np.vstack([np.zeros((WINDOW, N)), c[:-WINDOW]])
        r = ic_test(panel, f, start, end)
        out[t] = {"events": n, "ic": r.get("ic"), "t": r.get("t")}
    return out


def cmd_holdout() -> dict:
    d = out_dir()
    dev = json.loads((d / "dev.json").read_text(encoding="utf-8")) if (d / "dev.json").exists() else None
    if not dev or not (dev.get("P1") and dev.get("P2")):
        raise SystemExit("开发期未通过 P1、P2（或尚未计算），按预注册不计算留出期")
    if (d / "holdout.json").exists():
        raise SystemExit("留出期已经计算过（只允许一次），结果见 holdout.json")
    panel, factor, placed, *_ = _load_factor()
    res = ic_test(panel, factor, HOLDOUT_START, panel.dates[-1])
    res["P3"] = bool(res.get("ic") and res["ic"] > 0 and res["ic"] >= 0.5 * dev["ic"])
    res["event_study_holdout"] = event_study(panel, placed, HOLDOUT_START, panel.dates[-1])
    res["computed_at"] = datetime.now(UTC).isoformat()
    (d / "holdout.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return res


def main(argv: list[str]) -> int:
    cmds = {"prepare": cmd_prepare, "anonymize": cmd_anonymize, "score": cmd_score, "audit": cmd_audit,
            "dev": cmd_dev, "holdout": cmd_holdout}
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd not in cmds:
        print(__doc__)
        return 1
    cmds[cmd]()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
