"""离线进化实验（PRD 3.7 第 3 层）。

流程（全部为历史回放，依次执行）：
  第 0 代：用初始策略（选手设置，可为空）跑验证区间，作为对照
  第 g 代（g = 1..N）：
    训练：从上一代的策略出发回放训练区间，期间每 N 个交易日复盘改写策略
    验证：用训练结束时的策略回放验证区间，不复盘（策略固定）
判断进化是否有效看验证区间：后几代的验证收益是否稳定好于第 0 代。
训练区间的收益会因为「反复做同一段行情」而偏高，不能作为依据。

每一轮回放都是一个普通赛道（名称带实验编号），可以在赛道页查看详细决策与复盘。
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.arena import cycle, queries, strategy
from app.arena.lifecycle import ArenaError, create_track, enroll, start_track
from app.core.timeutils import utcnow
from app.db.session import get_sessionmaker
from app.models import AccountRow, AgentRow, ExperimentRow, ReflectionRow, StrategyVersionRow, TrackRow

logger = logging.getLogger(__name__)


class ExperimentCancelled(Exception):
    pass


def validate(s: Session, exp: ExperimentRow) -> None:
    agent = s.get(AgentRow, exp.agent_id)
    if agent is None or agent.kind != "llm":
        raise ArenaError("进化实验需要选择一个 AI 选手")
    if not exp.train_start < exp.train_end < exp.valid_start <= exp.valid_end:
        raise ArenaError("日期须满足：训练开始 < 训练结束 < 验证开始 ≤ 验证结束（验证区间必须在训练区间之后）")
    if not 1 <= exp.generations <= 10:
        raise ArenaError("代数应在 1～10 之间")
    if (exp.train_end - exp.train_start).days < 7:
        raise ArenaError("训练区间至少一周，否则来不及复盘")


def _status(exp_id: int) -> str:
    with get_sessionmaker()() as s:
        return s.get(ExperimentRow, exp_id).status


def _update(exp_id: int, **fields) -> None:
    with get_sessionmaker()() as s:
        exp = s.get(ExperimentRow, exp_id)
        for k, v in fields.items():
            setattr(exp, k, v)
        s.commit()


def _create_track(exp_id: int, phase: str, gen: int, seed: str, reflect: bool) -> int:
    with get_sessionmaker()() as s:
        exp = s.get(ExperimentRow, exp_id)
        start, end = (exp.train_start, exp.train_end) if phase == "train" else (exp.valid_start, exp.valid_end)
        label = "训练" if phase == "train" else "验证"
        t = create_track(s, exp.user_id, name=f"进化实验#{exp.id} 第{gen}代 {label}", universe_name=exp.universe_name,
                         rule_set_id=exp.rule_set_id, initial_cash=exp.initial_cash, output_mode=exp.output_mode,
                         mode="replay", replay_start=start, end_date=end, anonymize=exp.anonymize,
                         description=f"「{exp.name}」第 {gen} 代{label}（{'复盘改写策略' if reflect else '策略固定'}）")
        acc = enroll(s, t, exp.agent_id)
        acc.strategy_seed, acc.reflect_override, acc.reflect_every_override = seed, reflect, exp.reflect_every
        start_track(s, t, utcnow())
        s.commit()
        return t.id


def _summarize(track_id: int) -> dict:
    with get_sessionmaker()() as s:
        track = s.get(TrackRow, track_id)
        acc = s.scalars(select(AccountRow).where(AccountRow.track_id == track_id)).one()
        m = queries.account_metrics(s, acc, track)
        sv = strategy.current_version(s, acc.id)
        return {
            "track_id": track_id, "days": m.get("days"), "return": m.get("cumulative_return"),
            "benchmark": m.get("benchmark_return"), "excess": m.get("excess_return"),
            "max_drawdown": m.get("max_drawdown"), "trades": m.get("trades"),
            "reflections": len(list(s.scalars(select(ReflectionRow.id).where(ReflectionRow.account_id == acc.id)))),
            "strategy_versions": len(list(s.scalars(select(StrategyVersionRow.id)
                                                    .where(StrategyVersionRow.account_id == acc.id)))),
            "final_strategy": sv.content if sv else "",
        }


async def _phase(exp_id: int, phase: str, gen: int, seed: str, reflect: bool) -> dict:
    import asyncio

    if _status(exp_id) == "cancelled":
        raise ExperimentCancelled
    track_id = await asyncio.to_thread(_create_track, exp_id, phase, gen, seed, reflect)
    _update(exp_id, progress=f"第 {gen} 代{'训练' if phase == 'train' else '验证'}回放中（赛道 #{track_id}）")
    msg = await cycle.run_replay(track_id)
    if _status(exp_id) == "cancelled":
        raise ExperimentCancelled
    if "回放完成" not in msg:
        raise RuntimeError(f"赛道 #{track_id} 回放未完成：{msg}")
    return await asyncio.to_thread(_summarize, track_id)


async def run(exp_id: int) -> str:
    with get_sessionmaker()() as s:
        exp = s.get(ExperimentRow, exp_id)
        agent = s.get(AgentRow, exp.agent_id)
        seed = agent.initial_strategy or ""
        generations = exp.generations
    _update(exp_id, status="running", progress="开始", results=[])
    results: list[dict] = []
    try:
        control = await _phase(exp_id, "valid", 0, seed, reflect=False)
        results.append({"generation": 0, "strategy": seed, "train": None, "valid": control})
        _update(exp_id, results=list(results))
        for g in range(1, generations + 1):
            train = await _phase(exp_id, "train", g, seed, reflect=True)
            seed = train["final_strategy"] or seed
            valid = await _phase(exp_id, "valid", g, seed, reflect=False)
            results.append({"generation": g, "strategy": seed, "train": train, "valid": valid})
            _update(exp_id, results=list(results))
    except ExperimentCancelled:
        _update(exp_id, progress=f"已取消（完成 {len(results)} 组）")
        return "实验已取消"
    except Exception as e:
        logger.exception("进化实验 %s 失败", exp_id)
        _update(exp_id, status="failed", progress=f"失败：{e}")
        return f"实验失败：{e}"
    _update(exp_id, status="done", progress=f"完成：{generations} 代")
    return f"实验完成：{generations} 代"


def cancel(s: Session, exp: ExperimentRow) -> None:
    """取消实验：停止正在回放的赛道，编排在当前一步结束后退出。"""
    exp.status = "cancelled"
    for t in s.scalars(select(TrackRow).where(TrackRow.name.like(f"进化实验#{exp.id} %"),
                                              TrackRow.status == "running")):
        t.status = "finished"


def default_dates(today: date) -> dict:
    """界面默认值：训练为三个月前到一个月前，验证为最近一个月（均为过去的日期）。"""
    return {"train_start": str(today - timedelta(days=95)), "train_end": str(today - timedelta(days=36)),
            "valid_start": str(today - timedelta(days=35)), "valid_end": str(today - timedelta(days=3))}
