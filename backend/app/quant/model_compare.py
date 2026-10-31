"""研究模型对比（实验设计：docs/prereg/2026-10-31-model-compare.md）。

命令（服务器上运行，记录与结果写入 data/research/model_compare.json）：
  uv run python -m app.quant.model_compare setup 2 3   为供应商 2、3 各建一套风格库策略（建好即归档）
  uv run python -m app.quant.model_compare run         依次优化（可中断，再次运行时补完剩余轮数）
  uv run python -m app.quant.model_compare report      汇总指标

研究任务在本进程内运行，不经过后端的任务队列。运行期间不要重启后端：后端启动时会把未完成的研究标记为中断。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import date

from sqlalchemy import select

logger = logging.getLogger(__name__)

ROUNDS = 10
NAME_PREFIX = "模型对比"


def _path():
    from app.core.config import get_settings

    return get_settings().data_dir / "research" / "model_compare.json"


def _load() -> dict:
    p = _path()
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}


def _save(data: dict) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def setup(s, provider_ids: list[int], user_id: int, rounds: int = ROUNDS, settings: dict | None = None) -> dict:
    """为每个模型各建一套风格库策略（与风格库相同的初始规则、没有研究笔记），建好即归档。逐个提交。"""
    from app.models.arena import LLMProviderRow
    from app.quant.service import create_strategy, load_seeds

    providers = []
    for pid in provider_ids:
        p = s.get(LLMProviderRow, pid)
        if p is None or p.user_id != user_id:
            raise ValueError(f"模型供应商 {pid} 不存在或不属于该用户")
        providers.append({"id": p.id, "name": p.name, "model": p.default_model})
    items = []
    for seed in load_seeds():           # 同一风格的各模型相邻，运行时也按这个顺序
        for p in providers:
            st = create_strategy(s, name=f"{NAME_PREFIX}·{p['name']}·{seed['title']}"[:64], style=seed["style"],
                                 description=seed["description"], spec=seed["spec"], provider_id=p["id"],
                                 user_id=user_id, settings=settings)
            st.status = "archived"      # 不出现在策略库、不参加每晚优化
            s.commit()
            items.append({"style": seed["style"], "title": seed["title"], "provider_id": p["id"],
                          "strategy_id": st.id, "runs": []})
            logger.info("已建：%s", st.name)
    return {"rounds": rounds, "providers": providers, "items": items, "created_at": date.today().isoformat()}


def _rounds_done(s, item: dict) -> int:
    from app.models.quant import ResearchRunRow

    return sum(s.get(ResearchRunRow, rid).rounds_done for rid in item["runs"])


async def run_all(data: dict, save=_save) -> None:
    """依次优化每条策略到满 rounds 轮。被中断的运行标记为失败，下次只补剩余轮数（已完成的轮次照常计入）。"""
    from app.db.session import get_sessionmaker
    from app.models.quant import QuantStrategyRow, ResearchRunRow
    from app.quant import research

    for item in data["items"]:
        with get_sessionmaker()() as s:
            for rid in item["runs"]:
                run = s.get(ResearchRunRow, rid)
                if run.status in ("pending", "running"):
                    run.status, run.error, run.progress = "failed", "对比脚本中断", "中断"
            s.commit()
            left = data["rounds"] - _rounds_done(s, item)
            if left <= 0:
                continue
            run = research.start_run(s, s.get(QuantStrategyRow, item["strategy_id"]), left, trigger="compare")
            s.commit()
            item["runs"].append(run.id)
            save(data)
        logger.info("开始：%s（%d 轮）", item["title"], left)
        msg = await research.run_research(run.id)
        logger.info("结束：%s，%s", item["title"], msg)


def _sharpe(rep: dict | None) -> float | None:
    return (rep or {}).get("sharpe")


def summarize(s, data: dict) -> dict:
    """按实验设计汇总每个模型 × 每种风格的指标。"""
    from app.models.quant import QuantStrategyRow, QuantVersionRow, ResearchRunRow

    rows = []
    for item in data["items"]:
        st = s.get(QuantStrategyRow, item["strategy_id"])
        first = s.scalars(select(QuantVersionRow).where(QuantVersionRow.strategy_id == st.id,
                                                        QuantVersionRow.number == 1)).one()
        champ = s.get(QuantVersionRow, st.champion_id)
        runs = [s.get(ResearchRunRow, rid) for rid in item["runs"]]
        log = [e for r in runs for e in r.log or []]
        cands = [c for e in log for c in e.get("candidates") or []]
        seconds = sum((r.finished_at - r.created_at).total_seconds() for r in runs if r.finished_at)
        delta = {k: (round(_sharpe(getattr(champ, k)) - _sharpe(getattr(first, k)), 4)
                     if _sharpe(getattr(champ, k)) is not None and _sharpe(getattr(first, k)) is not None else None)
                 for k in ("dev", "holdout")}
        rows.append({
            "style": item["style"], "title": item["title"], "provider_id": item["provider_id"],
            "strategy_id": st.id, "rounds": sum(r.rounds_done for r in runs),
            "ok_rounds": sum(1 for e in log if e.get("status") == "ok"),
            "candidates": len(cands), "duplicates": sum(1 for c in cands if c.get("status") == "duplicate"),
            "promoted": sum(1 for e in log if e.get("promoted")),
            "champion": champ.number, "dev_sharpe": [_sharpe(first.dev), _sharpe(champ.dev)],
            "holdout_sharpe": [_sharpe(first.holdout), _sharpe(champ.holdout)],
            "dev_delta": delta["dev"], "holdout_delta": delta["holdout"],
            "tokens_in": sum(e.get("tokens_in") or 0 for e in log),
            "tokens_out": sum(e.get("tokens_out") or 0 for e in log),
            "cost": round(sum(e.get("cost") or 0 for e in log), 4), "minutes": round(seconds / 60, 1),
            "interrupted": sum(1 for r in runs if r.error == "对比脚本中断"),
            "failed_runs": sum(1 for r in runs if r.status == "failed" and r.error != "对比脚本中断"),
        })
    models = {}
    for p in data["providers"]:
        mine = [r for r in rows if r["provider_id"] == p["id"]]
        hd = [r["holdout_delta"] for r in mine if r["holdout_delta"] is not None]
        dd = [r["dev_delta"] for r in mine if r["dev_delta"] is not None]
        models[p["name"]] = {
            "rounds": sum(r["rounds"] for r in mine), "ok_rounds": sum(r["ok_rounds"] for r in mine),
            "candidates": sum(r["candidates"] for r in mine), "duplicates": sum(r["duplicates"] for r in mine),
            "promoted": sum(r["promoted"] for r in mine),
            "dev_delta_mean": round(sum(dd) / len(dd), 4) if dd else None,
            "holdout_delta_mean": round(sum(hd) / len(hd), 4) if hd else None,
            "tokens_in": sum(r["tokens_in"] for r in mine), "tokens_out": sum(r["tokens_out"] for r in mine),
            "cost": round(sum(r["cost"] for r in mine), 4), "minutes": round(sum(r["minutes"] for r in mine), 1),
        }
    wins = {}
    if len(data["providers"]) == 2:
        a, b = (p["id"] for p in data["providers"])
        names = {p["id"]: p["name"] for p in data["providers"]}
        for style in dict.fromkeys(r["style"] for r in rows):
            ra = next((r for r in rows if r["style"] == style and r["provider_id"] == a), None)
            rb = next((r for r in rows if r["style"] == style and r["provider_id"] == b), None)
            if ra and rb and ra["holdout_delta"] is not None and rb["holdout_delta"] is not None \
                    and ra["holdout_delta"] != rb["holdout_delta"]:
                w = names[a] if ra["holdout_delta"] > rb["holdout_delta"] else names[b]
                wins[w] = wins.get(w, 0) + 1
    # 实验设计：同一个模型在至少 6 种风格上留出期提升更高，才说「可能更好」
    leader = next((n for n, k in wins.items() if k >= 6), None)
    return {"rows": rows, "models": models, "holdout_wins": wins,
            "verdict": f"{leader} 可能更好（只跑了一次，需要重复验证）" if leader else "看不出差别"}


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO)
    from app.db.session import get_sessionmaker
    from app.models import DEFAULT_USER_ID

    stage = argv[0] if argv else ""
    data = _load()
    if stage == "setup":
        if data.get("items"):
            print("已经建过对比策略（见 model_compare.json），不重复创建")
            return 1
        with get_sessionmaker()() as s:
            data = setup(s, [int(x) for x in argv[1:]], DEFAULT_USER_ID)
        _save(data)
        print(f"已建 {len(data['items'])} 条对比策略")
        return 0
    if not data.get("items"):
        print("还没有建对比策略：先运行 setup")
        return 1
    if stage == "run":
        asyncio.run(run_all(data))
        _save(data)
        return 0
    if stage == "report":
        with get_sessionmaker()() as s:
            data["report"] = summarize(s, data)
        _save(data)
        print(json.dumps({k: data["report"][k] for k in ("models", "holdout_wins", "verdict")},
                         ensure_ascii=False, indent=2))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
