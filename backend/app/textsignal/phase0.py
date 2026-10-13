"""阶段 0：公告文本信号的可行性检验（判定标准先于结果写定（预注册文档已归档，未包含在本仓库））。

在服务器的 backend/ 目录下依次执行（模型密钥由程序从数据库读取，不经过命令行）：

    uv run python -m app.textsignal.phase0 download     # 下载沪深300 成分股 2018-01-01 起的公告标题
    uv run python -m app.textsignal.phase0 sample       # 抽样并计算公告后 20 个交易日的超额收益
    uv run python -m app.textsignal.phase0 run          # 两个模型分别完成三项任务（可断点续跑）
    uv run python -m app.textsignal.phase0 report       # 统计并生成报告

输出在 data/research/text_phase0/。
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import sys
import time
from collections import Counter
from datetime import date
from pathlib import Path

import numpy as np

from app.core.config import get_settings
from app.textsignal import announcements as ann

logger = logging.getLogger(__name__)

SEED = 20261006
START, END = date(2018, 1, 1), date(2026, 9, 30)
OLD = ("2018-01-01", "2024-12-31")            # 旧样本：模型训练数据大概率覆盖
RECENT = ("2026-06-01", "2026-08-31")         # 新样本：尽量在模型知识截止之后（需要之后 20 个交易日的行情）
HORIZON = 20
N_CLASSIFY, N_IDENTITY, N_BUNDLE, N_MEMORY = 400, 200, 40, 300
BATCH = 20
PROVIDERS = {"qwen": 1, "deepseek": 2}
CONCURRENCY = {"qwen": 4, "deepseek": 8}

TYPES = ["业绩", "股东增减持", "回购", "再融资", "并购重组", "重大合同或中标", "监管处罚或问询", "诉讼仲裁",
         "股权激励或员工持股", "分红送转", "人事变动", "经营数据", "产品或资质获批", "担保质押冻结",
         "风险提示或退市", "其他"]


def out_dir() -> Path:
    d = get_settings().data_dir / "research" / "text_phase0"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


# ---------------------------------------------------------------- 1. 下载


def cmd_download() -> None:
    from app.core.network import apply_direct_hosts
    from app.quant.data import universe_members

    apply_direct_hosts([*get_settings().direct_hosts, *ann.CNINFO_HOSTS])
    insts, _, _ = universe_members("cn_csi300")
    symbols = [i.symbol for i in insts if i.type == "stock"]
    res = ann.download(symbols, START, END, out_dir() / "announcements.jsonl.gz")
    print(f"下载完成：{res}")


# ---------------------------------------------------------------- 2. 抽样与公告后收益


def _excess_returns(items: list[dict]) -> None:
    """公告日前一交易日收盘 → 之后第 20 个交易日收盘，个股（复权）减沪深300。原地写入 excess。"""
    from app.db.session import get_sessionmaker
    from app.quant.data import get_panel

    with get_sessionmaker()() as s:
        panel = get_panel(s, "cn_csi300_pit_etf", date(2018, 1, 1), date.today())
    adj, bench = panel.adj_close, panel.bench
    for it in items:
        j = panel.col.get(it["symbol"])
        it["excess"] = None
        if j is None:
            continue
        i0 = panel.locate(date.fromisoformat(it["date"]), "after") - 1     # 公告日之前最后一个交易日
        i1 = i0 + HORIZON
        if i0 < 0 or i1 >= len(panel.dates):
            continue
        a0, a1 = adj[i0, j], adj[i1, j]
        if not (np.isfinite(a0) and np.isfinite(a1)) or a0 <= 0:
            continue
        it["excess"] = round(float(a1 / a0 - bench[i1] / bench[i0]), 5)


def cmd_sample() -> None:
    rows = list(ann.read(out_dir() / "announcements.jsonl.gz"))
    seen, uniq = set(), []
    for r in rows:
        key = (r["symbol"], r["date"], r["title"])
        if key not in seen:
            seen.add(key)
            uniq.append(r)
    for r in uniq:
        r["material"] = ann.is_material(r["title"])
        r["anon"] = ann.anonymize(r["title"], r["name"], r["symbol"])
    mat = [r for r in uniq if r["material"]]
    by_year = Counter(r["date"][:4] for r in uniq)
    mat_year = Counter(r["date"][:4] for r in mat)
    rng = random.Random(SEED)

    def pick(pool: list[dict], n: int) -> list[dict]:
        return rng.sample(pool, min(n, len(pool)))

    def tag(items: list[dict], prefix: str) -> list[dict]:
        return [{**it, "id": f"{prefix}{k}"} for k, it in enumerate(items)]

    classify = tag(pick(mat, N_CLASSIFY), "c")
    identity = tag(pick(mat, N_IDENTITY), "i")
    groups: dict[tuple, list[dict]] = {}
    for r in mat:
        groups.setdefault((r["symbol"], r["date"][:7]), []).append(r)
    multi = [g for g in groups.values() if len(g) >= 3]
    bundles = [{"id": f"b{k}", "symbol": g[0]["symbol"], "name": g[0]["name"], "month": g[0]["date"][:7],
                "titles": [x["anon"] for x in g[:8]]} for k, g in enumerate(pick(multi, N_BUNDLE))]
    old_pool = [r for r in mat if OLD[0] <= r["date"] <= OLD[1]]
    new_pool = [r for r in mat if RECENT[0] <= r["date"] <= RECENT[1]]
    old = tag(pick(old_pool, N_MEMORY * 2), "o")
    new = tag(pick(new_pool, N_MEMORY * 2), "n")
    _excess_returns(old + new)
    old = [x for x in old if x["excess"] is not None][:N_MEMORY]
    new = [x for x in new if x["excess"] is not None][:N_MEMORY]
    d = out_dir()
    _write_jsonl(d / "s_classify.jsonl", classify)
    _write_jsonl(d / "s_identity.jsonl", identity)
    _write_jsonl(d / "s_bundle.jsonl", bundles)
    _write_jsonl(d / "s_memory_old.jsonl", old)
    _write_jsonl(d / "s_memory_new.jsonl", new)
    stats = {"total": len(uniq), "symbols": len({r["symbol"] for r in uniq}), "material": len(mat),
             "by_year": dict(sorted(by_year.items())), "material_by_year": dict(sorted(mat_year.items())),
             "samples": {"classify": len(classify), "identity": len(identity), "bundle": len(bundles),
                         "memory_old": len(old), "memory_new": len(new), "new_pool": len(new_pool)},
             "anon_examples": [{"title": r["title"], "anon": r["anon"]} for r in pick(mat, 30)]}
    (d / "stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in stats.items() if k != "anon_examples"}, ensure_ascii=False, indent=1))


# ---------------------------------------------------------------- 3. 模型任务


_ANON_NOTE = "公司名、年份、人名等已隐去（「本公司」「某公司」「某年」「某人」）。"

SYS_CLASSIFY = f"""你是 A股上市公司公告分析员。下面是若干条公告标题，{_ANON_NOTE}
对每条判断：
- type：事件类型，只能从以下选一个：{"、".join(TYPES)}
- direction：对该公司股价短期（约一个月）影响的方向与强度，整数 -2～2（-2 明显利空，0 无明显影响，2 明显利好）
- materiality：信息重要程度，整数 0～3（0 例行公告，3 重大事件）
- confidence：你对 direction 判断的把握，0～1
只根据标题判断，不要猜测是哪家公司。只输出 JSON：
{{"items": [{{"id": "...", "type": "...", "direction": 0, "materiality": 0, "confidence": 0.5}}]}}"""

SYS_IDENTITY = f"""下面每条是一家 A股上市公司的公告标题，{_ANON_NOTE}请尽力猜测：
- guesses：这条公告最可能来自哪家上市公司，最多 3 个证券简称（完全没有头绪就给空列表）
- year：最可能的公告年份（2018～2026 的整数，没有头绪给 null）
只输出 JSON：{{"items": [{{"id": "...", "guesses": ["..."], "year": 2020}}]}}"""

SYS_BUNDLE = f"""下面每组是同一家 A股上市公司同一个月内的多条公告标题（用 | 分隔），{_ANON_NOTE}
请猜测每组来自哪家公司（最多 3 个证券简称）和年份。
只输出 JSON：{{"items": [{{"id": "...", "guesses": ["..."], "year": 2020}}]}}"""

SYS_MEMORY = """你是 A股分析员。下面每条是一家上市公司的公告。请判断：公告发布后约一个月（20 个交易日），
该股相对沪深300 指数的超额收益更可能为正还是负。
score：整数 -2～2（-2 很可能明显跑输，0 无法判断，2 很可能明显跑赢）。
只输出 JSON：{"items": [{"id": "...", "score": 0}]}"""


SYS_ANON = """你是信息脱敏员。下面每条是一家 A股上市公司的公告标题（附公司简称）。请改写成匿名版本：
- 去掉一切能识别具体公司的信息：公司名与简称、曾用名、子公司 / 母公司 / 关联公司名、品牌、英文名、产品或药品名称与代号、
  项目名、可转债等证券简称、地名、人名、具体年份；分别用「本公司」「某子公司」「某公司」「某品牌」「某产品」「某项目」
  「某债券」「某地」「某人」「某年」代替；
- 保留事件本身：事件类型、方向（增 / 减、获批 / 终止……）、金额、比例、月份、次数等；
- 不得增加原标题没有的信息，不要解释。
只输出 JSON：{"items": [{"id": "...", "anon": "..."}]}"""


def _tasks(name: str = "") -> dict[str, tuple[str, list[dict]]]:
    """任务名 → (系统提示, [{id, text}])。修订 1：Qwen 先做「匿名化改写」，写回样本（anon2）后再出现 *_v2 任务。"""
    d = out_dir()
    cls = _read_jsonl(d / "s_classify.jsonl")
    idn = _read_jsonl(d / "s_identity.jsonl")
    bnd = _read_jsonl(d / "s_bundle.jsonl")
    old = _read_jsonl(d / "s_memory_old.jsonl")
    new = _read_jsonl(d / "s_memory_new.jsonl")
    v2: dict = {}
    if name == "qwen":
        v2["anonymize"] = (SYS_ANON, [{"id": x["id"], "text": f"公司简称：{x['name']}；标题：{x['title']}"}
                                      for x in idn + old + new])
    if idn and "anon2" in idn[0]:
        v2 |= {"identity_v2": (SYS_IDENTITY, [{"id": x["id"], "text": x["anon2"]} for x in idn]),
               "memory_old_anon_v2": (SYS_MEMORY, [{"id": x["id"], "text": x["anon2"]} for x in old]),
               "memory_new_anon_v2": (SYS_MEMORY, [{"id": x["id"], "text": x["anon2"]} for x in new])}
    return v2 | {
        "classify": (SYS_CLASSIFY, [{"id": x["id"], "text": x["anon"]} for x in cls]),
        "identity": (SYS_IDENTITY, [{"id": x["id"], "text": x["anon"]} for x in idn]),
        "bundle": (SYS_BUNDLE, [{"id": x["id"], "text": " | ".join(x["titles"])} for x in bnd]),
        "memory_old_anon": (SYS_MEMORY, [{"id": x["id"], "text": x["anon"]} for x in old]),
        "memory_new_anon": (SYS_MEMORY, [{"id": x["id"], "text": x["anon"]} for x in new]),
        "memory_old_named": (SYS_MEMORY, [{"id": x["id"], "text": f"公司：{x['name']}（{x['symbol']}）；"
                                                                  f"日期：{x['date']}；标题：{x['title']}"}
                                          for x in old]),
    }


def _parse(raw: str) -> list[dict]:
    from app.agent.parse import DecisionParseError

    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        raise DecisionParseError("没有找到 JSON 对象")
    try:
        obj = json.loads(m.group(0))
    except ValueError as e:
        raise DecisionParseError(f"JSON 格式错误：{e}") from None
    items = obj.get("items") if isinstance(obj, dict) else None
    if not isinstance(items, list):
        raise DecisionParseError("缺少 items 列表")
    return [x for x in items if isinstance(x, dict) and "id" in x]


async def _run_model(name: str, only: set[str] | None = None) -> None:
    from app.agent.runner import run_llm_json
    from app.arena.strategy import llm_config_for
    from app.db.session import get_sessionmaker
    from app.llm import calc_cost, create_client

    with get_sessionmaker()() as s:
        cfg, model, _, json_mode = llm_config_for(s, PROVIDERS[name], None)
    client = create_client(cfg)
    params = {"temperature": 0}
    if name == "qwen":       # 本地 Qwen 关闭思考过程，加快批量分类（服务不支持时忽略）
        params["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
    path = out_dir() / f"raw_{name}.jsonl"
    done = {(r["task"], r["batch"]) for r in _read_jsonl(path) if r.get("status") == "ok"}
    sem = asyncio.Semaphore(CONCURRENCY[name])
    lock = asyncio.Lock()
    jobs = []
    for task, (system, items) in _tasks(name).items():
        if only and task not in only:
            continue
        for b in range(0, len(items), BATCH):
            if (task, b) not in done:
                jobs.append((task, b, system, items[b:b + BATCH]))
    print(f"{name}：{len(jobs)} 批待处理（已完成 {len(done)} 批）")

    async def one(task: str, b: int, system: str, chunk: list[dict]) -> None:
        user = "\n".join(f"[{x['id']}] {x['text']}" for x in chunk)
        async with sem:
            t0 = time.monotonic()
            out = await run_llm_json(client, model=model, system=system, user=user, params=params,
                                     json_mode=json_mode, timeout_s=600, parse=_parse)
        rec = {"task": task, "batch": b, "status": out.status, "error": out.error, "items": out.parsed or [],
               "n": len(chunk), "attempts": out.attempts, "tokens_in": out.tokens_in, "tokens_out": out.tokens_out,
               "cost": float(calc_cost(cfg, out.tokens_in, out.tokens_out)),
               "seconds": round(time.monotonic() - t0, 1)}
        async with lock:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"  {name} {task}#{b}: {out.status} {rec['seconds']}s")

    await asyncio.gather(*(one(*j) for j in jobs))


def cmd_anon2() -> None:
    """把 Qwen 的匿名化改写写回样本（anon2）：改写结果再过一遍规则；仍含公司简称、或比原标题长很多时退回规则版本。"""
    d = out_dir()
    o, _ = _latest("qwen")
    rewritten = o.get("anonymize", {})
    stats = Counter()
    for fname in ("s_identity.jsonl", "s_memory_old.jsonl", "s_memory_new.jsonl"):
        rows = _read_jsonl(d / fname)
        for x in rows:
            x["anon2"], x["anon2_src"] = finalize_anon(rewritten.get(x["id"], {}).get("anon"), x)
            stats[x["anon2_src"]] += 1
        _write_jsonl(d / fname, rows)
    print(f"两步匿名化：{dict(stats)}")


def finalize_anon(rewritten, x: dict) -> tuple[str, str]:
    """模型改写的匿名标题再过一遍规则；为空、仍含公司简称、或比原标题长很多时退回规则版本。返回 (标题, 来源)。"""
    text = str(rewritten or "").strip()
    text = ann.anonymize(text, x["name"], x["symbol"]) if text else ""
    alias = x["name"].replace("*", "").replace("ST", "").strip()
    if not text or (len(alias) >= 2 and alias in text) or len(text) > 1.5 * len(x["title"]) + 10:
        return x["anon"], "rule"
    return text, "llm"


def cmd_run(models: list[str]) -> None:
    async def main():
        await asyncio.gather(*(_run_model(m) for m in models))

    asyncio.run(main())


# ---------------------------------------------------------------- 4. 统计


def _latest(name: str) -> tuple[dict[str, dict[str, dict]], dict]:
    """每个任务每个 id 的最终输出（同一批重跑时取最后一次成功），以及调用统计。"""
    recs = _read_jsonl(out_dir() / f"raw_{name}.jsonl")
    out: dict[str, dict[str, dict]] = {}
    usage = {"calls": 0, "ok": 0, "tokens_in": 0, "tokens_out": 0, "cost": 0.0, "seconds": 0.0, "retries": 0}
    for r in recs:
        usage["calls"] += 1
        usage["tokens_in"] += r["tokens_in"]
        usage["tokens_out"] += r["tokens_out"]
        usage["cost"] += r["cost"]
        usage["seconds"] += r["seconds"]
        usage["retries"] += max(r.get("attempts", 1) - 1, 0)
        if r["status"] == "ok":
            usage["ok"] += 1
            for it in r["items"]:
                out.setdefault(r["task"], {})[str(it["id"])] = it
    return out, usage


def _int(v, lo: int, hi: int) -> int | None:
    try:
        x = int(round(float(v)))
    except (TypeError, ValueError):
        return None
    return x if lo <= x <= hi else None


def _kappa(a: list, b: list) -> float | None:
    if not a:
        return None
    labels = sorted(set(a) | set(b))
    n = len(a)
    po = sum(x == y for x, y in zip(a, b, strict=True)) / n
    pe = sum((a.count(lb) / n) * (b.count(lb) / n) for lb in labels)
    return round((po - pe) / (1 - pe), 3) if pe < 1 else None


def _spearman(x: list[float], y: list[float]) -> float | None:
    """秩相关（并列取平均秩：打分只有 5 档，并列很多）。"""
    import pandas as pd

    if len(x) < 10:
        return None
    rx = pd.Series(x).rank().to_numpy()
    ry = pd.Series(y).rank().to_numpy()
    if np.std(rx) == 0 or np.std(ry) == 0:
        return 0.0
    return float(np.corrcoef(rx, ry)[0, 1])


def _boot_diff(a: tuple[list, list], b: tuple[list, list], n: int = 2000) -> tuple[float, float, float] | None:
    """两组 Spearman 相关之差（a − b）及其 bootstrap 95% 区间。"""
    ca, cb = _spearman(*a), _spearman(*b)
    if ca is None or cb is None:
        return None
    rng = np.random.default_rng(SEED)
    xa, ya, xb, yb = map(np.asarray, (*a, *b))
    diffs = []
    for _ in range(n):
        ia = rng.integers(0, len(xa), len(xa))
        ib = rng.integers(0, len(xb), len(xb))
        da, db = _spearman(list(xa[ia]), list(ya[ia])), _spearman(list(xb[ib]), list(yb[ib]))
        if da is not None and db is not None:
            diffs.append(da - db)
    lo, hi = np.percentile(diffs, [2.5, 97.5])
    return round(ca - cb, 3), round(float(lo), 3), round(float(hi), 3)


def _norm(s: str) -> str:
    return re.sub(r"[\s*]|ST|股份|集团|控股|A$", "", str(s or "")).upper()


def _hit(guesses, name: str) -> bool:
    n = _norm(name)
    for g in guesses or []:
        gn = _norm(g)
        if len(gn) >= 2 and (gn == n or gn in n or n in gn):
            return True
    return False


def cmd_report() -> dict:
    d = out_dir()
    cls = {x["id"]: x for x in _read_jsonl(d / "s_classify.jsonl")}
    idn = {x["id"]: x for x in _read_jsonl(d / "s_identity.jsonl")}
    bnd = {x["id"]: x for x in _read_jsonl(d / "s_bundle.jsonl")}
    old = {x["id"]: x for x in _read_jsonl(d / "s_memory_old.jsonl")}
    new = {x["id"]: x for x in _read_jsonl(d / "s_memory_new.jsonl")}
    res: dict = {"models": {}, "agreement": {}}
    outs = {}
    for name in PROVIDERS:
        o, usage = _latest(name)
        outs[name] = o
        m: dict = {"usage": {k: round(v, 3) if isinstance(v, float) else v for k, v in usage.items()}}
        expected = len(cls) + len(idn) + len(bnd) + 2 * len(old) + len(new)
        v1 = ("classify", "identity", "bundle", "memory_old_anon", "memory_new_anon", "memory_old_named")
        got = sum(len(o.get(t, {})) for t in v1)
        m["parse_rate"] = round(got / expected, 4) if expected else None
        # 身份泄漏（v2 为修订 1 的两步匿名化；single = 只给 1 个猜测且命中，区分「认出公司」与「猜行业龙头」）
        years = Counter(v["date"][:4] for v in idn.values())
        m["year_base"] = round(max(years.values()) / max(len(idn), 1), 4) if years else None
        for suffix in ("", "_v2"):
            ii = o.get("identity" + suffix, {})
            if not ii:
                continue
            m["identity_hit" + suffix] = round(
                sum(_hit(ii.get(k, {}).get("guesses"), v["name"]) for k, v in idn.items()) / max(len(idn), 1), 4)
            m["identity_single_hit" + suffix] = round(sum(
                len(ii.get(k, {}).get("guesses") or []) == 1 and _hit(ii.get(k, {}).get("guesses"), v["name"])
                for k, v in idn.items()) / max(len(idn), 1), 4)
            m["identity_answered" + suffix] = round(
                sum(bool(ii.get(k, {}).get("guesses")) for k in idn) / max(len(idn), 1), 4)
            m["year_hit" + suffix] = round(
                sum(str(ii.get(k, {}).get("year")) == v["date"][:4] for k, v in idn.items()) / max(len(idn), 1), 4)
        bb = o.get("bundle", {})
        m["bundle_hit"] = round(sum(_hit(bb.get(k, {}).get("guesses"), v["name"]) for k, v in bnd.items())
                                / max(len(bnd), 1), 4)
        # 记忆泄漏
        def pairs(task: str, sample: dict, o: dict = o) -> tuple[list, list]:
            got_ = o.get(task, {})
            xs, ys = [], []
            for k, v in sample.items():
                sc = _int(got_.get(k, {}).get("score"), -2, 2)
                if sc is not None:
                    xs.append(float(sc))
                    ys.append(v["excess"])
            return xs, ys

        oa, na, on = pairs("memory_old_anon", old), pairs("memory_new_anon", new), pairs("memory_old_named", old)
        m["memory"] = {"old_anon": _spearman(*oa), "new_anon": _spearman(*na), "old_named": _spearman(*on),
                       "n": [len(oa[0]), len(na[0]), len(on[0])],
                       "anon_old_minus_new": _boot_diff(oa, na), "named_minus_anon_old": _boot_diff(on, oa)}
        if o.get("memory_old_anon_v2"):
            oa2, na2 = pairs("memory_old_anon_v2", old), pairs("memory_new_anon_v2", new)
            m["memory"] |= {"old_anon_v2": _spearman(*oa2), "new_anon_v2": _spearman(*na2),
                            "n_v2": [len(oa2[0]), len(na2[0])], "anon_old_minus_new_v2": _boot_diff(oa2, na2),
                            "named_minus_anon_old_v2": _boot_diff(on, oa2)}
        for k in ("old_anon", "new_anon", "old_named", "old_anon_v2", "new_anon_v2"):
            if m["memory"].get(k) is not None:
                m["memory"][k] = round(m["memory"][k], 3)
        res["models"][name] = m
    # 两个模型的分类一致性
    a, b = outs["qwen"].get("classify", {}), outs["deepseek"].get("classify", {})
    both = [k for k in cls if k in a and k in b]
    ta = [str(a[k].get("type")) for k in both]
    tb = [str(b[k].get("type")) for k in both]
    da = [_int(a[k].get("direction"), -2, 2) for k in both]
    db = [_int(b[k].get("direction"), -2, 2) for k in both]
    nz = [(x, y) for x, y in zip(da, db, strict=True) if x and y]
    dir_pairs = [(x, y) for x, y in zip(da, db, strict=True) if x is not None and y is not None]
    res["agreement"] = {
        "n": len(both), "type_kappa": _kappa(ta, tb),
        "type_same": round(sum(x == y for x, y in zip(ta, tb, strict=True)) / max(len(both), 1), 3),
        "sign_agree_nonzero": round(sum((x > 0) == (y > 0) for x, y in nz) / len(nz), 3) if nz else None,
        "nonzero_both": len(nz),
        "direction_spearman": round(_spearman([p[0] for p in dir_pairs], [p[1] for p in dir_pairs]) or 0, 3),
        "type_dist": {"qwen": Counter(ta).most_common(8), "deepseek": Counter(tb).most_common(8)},
    }
    (d / "report.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return res


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "download":
        cmd_download()
    elif cmd == "sample":
        cmd_sample()
    elif cmd == "run":
        cmd_run(argv[2:] or list(PROVIDERS))
    elif cmd == "anon2":
        cmd_anon2()
    elif cmd == "report":
        cmd_report()
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
