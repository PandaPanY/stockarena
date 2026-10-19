"""某个模型供应商被哪些功能用着，以及把它一次换成另一个模型（模型失效、无法恢复时用）。

- AI 优化：在用策略的研究模型；
- 自选股监控：没单独设置时沿用该用户 AI 复核的模型；
- AI 对比选手：在运行中赛道里有账户的 AI 选手；
- AI 交易复核：只列出、不在这里替换——换复核模型会结束当前一段评估，必须在组合页走「更换复核模型」
  （app/quant/review.py change_model）。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session


class UsageError(ValueError):
    pass


def usages(s: Session, provider_id: int) -> dict:
    from app.models import AccountRow, AgentRow, LLMProviderRow, TrackRow
    from app.models.quant import QuantPortfolioRow, QuantStrategyRow
    from app.watch import service as watch

    prov = s.get(LLMProviderRow, provider_id)
    if prov is None:
        return {"strategies": [], "reviews": [], "watch": None, "agents": []}
    strategies = [{"id": st.id, "name": st.name, "model": st.model, "auto_optimize": st.auto_optimize}
                  for st in s.scalars(select(QuantStrategyRow).where(QuantStrategyRow.provider_id == provider_id,
                                                                     QuantStrategyRow.status == "active")
                                      .order_by(QuantStrategyRow.id))]
    reviews = []
    for pf in s.scalars(select(QuantPortfolioRow).where(QuantPortfolioRow.status == "active",
                                                        QuantPortfolioRow.user_id == prov.user_id)):
        cfg = (pf.settings or {}).get("review") or {}
        if cfg.get("enabled") and cfg.get("provider_id") == provider_id:
            reviews.append({"portfolio_id": pf.id, "name": pf.name, "model": cfg.get("model")})
    w = watch.settings(s, prov.user_id)
    agents = []
    for ag in s.scalars(select(AgentRow).where(AgentRow.provider_id == provider_id, AgentRow.kind == "llm")):
        tracks = [t.name for t in s.scalars(select(TrackRow).join(AccountRow, AccountRow.track_id == TrackRow.id)
                                            .where(AccountRow.agent_id == ag.id, AccountRow.status == "active",
                                                   TrackRow.status.in_(("running", "paused"))))]
        agents.append({"id": ag.id, "name": ag.name, "model": ag.model, "tracks": tracks})
    return {"strategies": strategies, "reviews": reviews,
            "watch": {"model": w.get("model"), "enabled": w.get("enabled")}
            if w.get("provider_id") == provider_id else None,
            "agents": agents}


def usage_lines(u: dict) -> list[str]:
    """推送里用的简短列表。"""
    out = []
    if u["strategies"]:
        auto = [x["name"] for x in u["strategies"] if x["auto_optimize"]]
        out.append(f"- AI 优化：{len(u['strategies'])} 条策略"
                   + (f"（每晚自动优化：{'、'.join(auto)}）" if auto else ""))
    if u["reviews"]:
        out.append(f"- AI 交易复核：{'、'.join(x['name'] for x in u['reviews'])}（失败时按规则执行）")
    if u["watch"] and u["watch"].get("enabled"):
        out.append("- 自选股监控")
    live = [x for x in u["agents"] if x["tracks"]]
    if live:
        out.append(f"- AI 对比选手：{'、'.join(x['name'] for x in live)}")
    return out


def replace(s: Session, provider_id: int, target_id: int, model: str | None, *, strategies: bool, watch_: bool,
            agent_ids: list[int], user_id: int) -> dict:
    """把用到 provider_id 的 AI 优化 / 自选股监控 / 指定的 AI 对比选手换成 target_id 的模型。AI 复核不在这里换。"""
    from app.models import AgentRow, LLMProviderRow
    from app.models.quant import QuantStrategyRow
    from app.watch import service as watch

    src, dst = s.get(LLMProviderRow, provider_id), s.get(LLMProviderRow, target_id)
    if src is None or src.user_id != user_id or dst is None or dst.user_id != user_id:
        raise UsageError("模型供应商不存在（只能使用自己配置的模型）")
    if target_id == provider_id and not model:
        raise UsageError("请选择另一个供应商，或填写同一供应商下的其他模型")
    out = {"strategies": 0, "watch": False, "agents": 0}
    if strategies:
        for st in s.scalars(select(QuantStrategyRow).where(QuantStrategyRow.provider_id == provider_id,
                                                           QuantStrategyRow.status == "active",
                                                           QuantStrategyRow.user_id == user_id)):
            st.provider_id, st.model = target_id, model or None
            out["strategies"] += 1
    if watch_ and watch.settings(s, user_id).get("provider_id") == provider_id:
        watch.set_settings(s, user_id, provider_id=target_id, model=model or "")
        out["watch"] = True
    for aid in agent_ids:
        ag = s.get(AgentRow, aid)
        if ag is None or ag.provider_id != provider_id or ag.user_id != user_id or ag.kind != "llm":
            raise UsageError(f"选手 {aid} 不在用这个模型")
        ag.provider_id, ag.model = target_id, model or None
        out["agents"] += 1
    s.flush()
    return out
