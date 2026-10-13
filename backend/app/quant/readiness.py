"""「可实盘」检查（PRD 3.9）：策略的冠军版本或多风格组合同时满足以下条件时标记为「可实盘」。

1. 开发期（2018～2024）风险调整后跑赢基准：夏普高于基准同期夏普；
2. 留出期跑赢基准且没有明显退化：留出期夏普高于基准同期夏普，且不低于开发期夏普的一半；
3. 逐年稳定：开发期完整年份中，至少一半的年份跑赢基准；
4. 实时跟踪：当前版本已跟踪至少 60 个交易日（约 3 个月），健康度不是「提醒」，回测—跟踪对账「一致」或「有偏差」；
5. 资金容量：计划投入的资金（实盘账户初始资金，没有时取跟踪本金）不低于最低资金、不超过可承载资金。

第 4 条需要时间积累，未满足前为「待观察」；任一条不满足为「未通过」。
实盘对照（G8）作为参考列出，不参与判定——实盘之后用它持续检验。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analytics.metrics import nav_metrics
from app.arena.deps import universe_resolver
from app.models import AccountRow, TrackRow
from app.models.quant import QuantPortfolioRow, QuantStrategyRow, QuantVersionRow, RealAccountRow
from app.quant import capacity as capacity_mod
from app.quant import data, reconcile, service
from app.quant.backtest import run_backtest
from app.quant.evaluate import window_report
from app.quant.spec import parse_spec

MIN_TRACK_DAYS = 60
HOLDOUT_KEEP = 0.5
YEAR_WIN = 0.5
DEFAULT_CAPITAL = 1_000_000.0


def _item(key: str, name: str, level: str, detail: str, value=None) -> dict:
    """level：pass 通过 / fail 未通过 / pending 待观察 / info 参考。"""
    return {"key": key, "name": name, "level": level, "detail": detail, "value": value}


def _bench_sharpe(res, start, end) -> float | None:
    b = res.bench_series()
    b = b[(b.index >= start) & (b.index <= end)]
    return nav_metrics(b, None).get("sharpe") if len(b) > 2 else None


def _fmt(v: float | None, pct: bool = False) -> str:
    if v is None:
        return "-"
    return f"{v:.1%}" if pct else f"{v:.2f}"


def check(s: Session, kind: str, ref_id: int) -> dict:
    """计算检查结果（不写库）。kind：strategy / portfolio。"""
    from app.quant.portfolio import alloc_of, members_of, run_portfolio

    if kind == "strategy":
        st = s.get(QuantStrategyRow, ref_id)
        if st is None or not st.champion_id:
            return {"status": "未通过", "items": [], "message": "策略没有冠军版本"}
        v = s.get(QuantVersionRow, st.champion_id)
        members = [(parse_spec(v.spec), 1.0)]
        cfg, panel, follow = service.settings_of(st), service.panel_for(s, st), st.follow_account_id
        label, cap_key, alloc = f"{st.name} v{v.number}", ("v", v.id), None
    else:
        pf = s.get(QuantPortfolioRow, ref_id)
        ms = members_of(s, pf) if pf else []
        if len(ms) < 2:
            return {"status": "未通过", "items": [], "message": "组合中可用的策略少于 2 个"}
        members = [(m.spec, m.weight) for m in ms]
        st0 = ms[0].strategy
        cfg, panel, follow = service.settings_of(st0), service.panel_for(s, st0), pf.follow_account_id
        label, alloc = pf.name, alloc_of(pf)
        cap_key = ("p", pf.id, tuple((m.version.id, m.weight) for m in ms), str(alloc.to_dict()))
    w = cfg.windows(panel.dates[-1])
    items = []

    # 0. 股票池必须是全时点版本：只用今天的成分回看历史（如快速体验股票池）会有幸存者偏差，回测偏乐观
    uname = st.universe_name if kind == "strategy" else st0.universe_name
    uspec = universe_resolver().specs.get(uname)
    if uspec and uspec.history and uspec.point_in_time and not data.pit_ready(s, uname):
        items.append(_item("universe", "股票池为全时点版本", "fail",
                           f"「{uspec.title}」的历史成分还没准备好（获取 Baostock 历史成分失败），"
                           "目前只用今天的成分回看历史，"
                           "有幸存者偏差。请管理员在策略库重新「准备完整数据」"))
    elif not (uspec and uspec.history and uspec.point_in_time):
        items.append(_item("universe", "股票池为全时点版本", "fail",
                           f"「{uspec.title if uspec else uname}」只用今天的成分股回看历史，有幸存者偏差，回测偏乐观。"
                           "请在全时点股票池（如沪深300 全时点）上重新创建或另存这个策略"))
    if len(members) > 1:
        res = run_portfolio(members, panel, cfg.train_start, w.end, cfg.initial_cash, alloc=alloc)
    else:
        res = run_backtest(members[0][0], panel, cfg.train_start, w.end, cfg.initial_cash)
    dev = window_report(res, *w.dev)
    ho = window_report(res, *w.holdout) if w.holdout else {}

    # 1. 开发期风险调整后跑赢基准
    sd, bd = dev.get("sharpe"), _bench_sharpe(res, *w.dev)
    ok = sd is not None and bd is not None and sd > bd
    items.append(_item("dev", "开发期跑赢基准（风险调整后）", "pass" if ok else "fail",
                       f"开发期 {w.dev[0].year}～{w.dev[1].year} 夏普 {_fmt(sd)}，基准 {_fmt(bd)}；"
                       f"年化 {_fmt(dev.get('annualized_return'), True)}，"
                       f"最大回撤 {_fmt(dev.get('max_drawdown'), True)}",
                       sd))

    # 2. 留出期跑赢基准且没有明显退化
    if not ho.get("days"):
        items.append(_item("holdout", "留出期跑赢基准且不明显退化", "pending", "还没有留出期数据"))
    else:
        sh, bh = ho.get("sharpe"), _bench_sharpe(res, *w.holdout)
        beat = sh is not None and bh is not None and sh > bh
        keep = sh is not None and (sd is None or sd <= 0 or sh >= HOLDOUT_KEEP * sd)
        items.append(_item("holdout", "留出期跑赢基准且不明显退化", "pass" if beat and keep else "fail",
                           f"留出期（{w.holdout[0]} 起）夏普 {_fmt(sh)}，基准 {_fmt(bh)}；"
                           f"需高于基准且不低于开发期的 {HOLDOUT_KEEP:.0%}（{_fmt((sd or 0) * HOLDOUT_KEEP)}）；"
                           f"累计 {_fmt(ho.get('cumulative_return'), True)}，"
                           f"基准 {_fmt(ho.get('benchmark_return'), True)}",
                           sh))

    # 3. 逐年稳定
    years = [y for y in dev.get("yearly", []) if not y.get("partial")]
    wins = sum(1 for y in years if (y.get("excess") or 0) > 0)
    rate = wins / len(years) if years else 0.0
    items.append(_item("yearly", "逐年稳定", "pass" if years and rate >= YEAR_WIN else "fail",
                       f"开发期 {len(years)} 个完整年份中 {wins} 年跑赢基准（需 ≥ {YEAR_WIN:.0%}）：" + "，".join(
                           f"{y['year']} {y['excess']:+.1%}" for y in years), round(rate, 3)))

    # 4. 实时跟踪
    acc = s.get(AccountRow, follow) if follow else None
    track = s.get(TrackRow, acc.track_id) if acc else None
    hl: dict | None = None
    if acc is None or track is None or track.status not in ("running", "paused"):
        items.append(_item("tracking", "实时跟踪 ≥ 3 个月且表现正常", "pending", "尚未开始实时跟踪"))
    else:
        recon = reconcile.reconcile(s, acc.id)
        hl = reconcile.health(s, acc.id, recon)
        days = recon.get("days") or 0
        bad = hl.get("status") == "提醒" or recon.get("status") == "偏差较大"
        detail = (f"当前版本已跟踪 {days} 个交易日（需 ≥ {MIN_TRACK_DAYS}）；健康度：{hl.get('status')}；"
                  f"回测—跟踪对账：{recon.get('status')}")
        if days >= MIN_TRACK_DAYS and recon.get("gap_annual") is not None:
            detail += f"（年化差 {recon['gap_annual']:+.1%}）"
        level = "fail" if bad else ("pass" if days >= MIN_TRACK_DAYS else "pending")
        items.append(_item("tracking", "实时跟踪 ≥ 3 个月且表现正常", level, detail, days))

    # 5. 资金容量
    real = s.scalars(select(RealAccountRow).where(RealAccountRow.kind == kind, RealAccountRow.ref_id == ref_id,
                                                  RealAccountRow.status == "active")
                     .order_by(RealAccountRow.id.desc())).first()
    capital = (real.initial_cash if real else (float(track.initial_cash) if track else DEFAULT_CAPITAL))
    src = "实盘账户初始资金" if real else ("跟踪本金" if track else "默认")
    cap = capacity_mod.capacity(members, panel, cfg, key=cap_key, alloc=alloc)
    lo, hi = cap.get("min_capital"), cap.get("capacity")
    ok = lo is not None and capital >= lo and (hi is None or capital <= hi)
    items.append(_item("capacity", "资金规模在容量范围内", "pass" if ok else "fail",
                       f"计划资金 {capital / 1e4:,.0f} 万（{src}）；最低资金 "
                       f"{'-' if lo is None else f'{lo / 1e4:,.0f} 万'}，可承载 "
                       f"{'-' if hi is None else f'{hi / 1e4:,.0f} 万'}", capital))

    # 参考：实盘对照
    if real is not None:
        from app.quant import real as real_mod

        try:
            rep = real_mod.report(s, real)
            items.append(_item("real", "实盘对照（参考）", "info",
                               f"实盘账户「{real.name}」：{rep.get('status')}；" + (
                                   f"实盘 {rep['real_return']:+.1%} / 模拟 {rep['sim_return']:+.1%}，"
                                   f"跟踪误差 {rep['tracking_error']:.1%}" if "sim_return" in rep else
                                   rep.get("message", ""))))
        except Exception as e:  # noqa: BLE001 - 参考项失败不影响判定
            items.append(_item("real", "实盘对照（参考）", "info", f"无法生成对比报告：{e}"))

    levels = [it["level"] for it in items if it["level"] != "info"]
    if "fail" in levels:
        status = "未通过"
        message = "以下标为「未通过」的条件不满足，暂不建议用于实盘"
    elif "pending" in levels:
        status = "待观察"
        message = "历史检验已通过，等待实时跟踪积累到 3 个月"
    else:
        status = "可实盘"
        message = "所有条件均满足，可以考虑按跟踪的规则用于实盘；实盘后请录入成交，持续对照"
    return {"status": status, "label": label, "message": message, "items": items,
            "health": {k: hl[k] for k in ("status", "message", "checks") if k in hl} if hl else None,
            "checked_at": datetime.now(UTC).isoformat()}


def refresh(s: Session, kind: str, ref_id: int) -> dict:
    """计算并保存到策略 / 组合（先算后写，写入在调用方的短事务中提交）。"""
    out = check(s, kind, ref_id)
    row = s.get(QuantStrategyRow if kind == "strategy" else QuantPortfolioRow, ref_id)
    if row is not None:
        row.readiness = out
    return out
