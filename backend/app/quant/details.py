"""策略版本的交易明细（PRD 3.8）：调仓记录与选股理由、每笔持有的收益、标的汇总、成交流水。

按需重新回测（带 explain），结果按「版本 + 数据最新日期」缓存。
"""

from __future__ import annotations

import json
from collections import OrderedDict
from datetime import date

from app.quant.backtest import BacktestResult, run_backtest
from app.quant.panel import Panel
from app.quant.service import LabSettings
from app.quant.spec import parse_spec

_cache: OrderedDict[tuple, BacktestResult] = OrderedDict()
_CACHE_SIZE = 6
WINDOWS = ("train", "valid", "dev", "holdout", "all")


def _result(version_id: int, spec: dict, panel: Panel, cfg: LabSettings) -> BacktestResult:
    from app.quant.costs import current as current_costs

    key = (version_id, json.dumps(spec, sort_keys=True), id(panel), panel.dates[-1], len(panel.symbols),
           current_costs().signature())                    # 不同用户的交易成本设置不同
    res = _cache.get(key)
    if res is None:
        res = run_backtest(parse_spec(spec), panel, cfg.train_start, panel.dates[-1], cfg.initial_cash, explain=True)
        _cache[key] = res
        while len(_cache) > _CACHE_SIZE:
            _cache.popitem(last=False)
    _cache.move_to_end(key)
    return res


def window_range(cfg: LabSettings, window: str, last: date) -> tuple[date, date]:
    if window == "train":
        return cfg.train_start, cfg.train_end
    if window == "valid":
        return cfg.valid_start, cfg.valid_end
    if window == "dev":
        return cfg.train_start, cfg.valid_end
    if window == "holdout":
        return cfg.holdout_start, last
    return cfg.train_start, last


def details(version_id: int, spec: dict, panel: Panel, cfg: LabSettings, window: str = "holdout",
            trade_limit: int = 2000) -> dict:
    res = _result(version_id, spec, panel, cfg)
    start, end = window_range(cfg, window, panel.dates[-1])
    names = dict(zip(panel.symbols, panel.names, strict=True))
    types = dict(zip(panel.symbols, panel.types, strict=True))
    s_iso, e_iso = start.isoformat(), end.isoformat()

    # 每笔持有：在区间内卖出，或区间结束时仍持有（买入早于区间结束）
    eps = []
    for ep in res.episodes:
        exit_d = ep.exit
        in_window = (exit_d is not None and start <= exit_d <= end) or \
                    (exit_d is None and ep.entry <= end and end >= panel.dates[-1])
        if not in_window:
            continue
        eps.append({"symbol": ep.symbol, "name": names.get(ep.symbol, ep.symbol), "type": types.get(ep.symbol),
                    "entry": ep.entry.isoformat(), "exit": exit_d.isoformat() if exit_d else None,
                    "days": ep.days, "cost": round(ep.cost, 2), "proceeds": round(ep.proceeds, 2),
                    "pnl": round(ep.pnl, 2), "ret": round(ep.ret, 4), "reason": ep.exit_reason,
                    "open": exit_d is None})
    eps.sort(key=lambda e: e["exit"] or "9999", reverse=True)

    by_sym: dict[str, dict] = {}
    for e in eps:
        x = by_sym.setdefault(e["symbol"], {"symbol": e["symbol"], "name": e["name"], "type": e["type"],
                                            "episodes": 0, "wins": 0, "pnl": 0.0, "days": 0, "best": None,
                                            "worst": None})
        x["episodes"] += 1
        x["wins"] += 1 if e["pnl"] > 0 else 0
        x["pnl"] = round(x["pnl"] + e["pnl"], 2)
        x["days"] += e["days"]
        x["best"] = e["ret"] if x["best"] is None else max(x["best"], e["ret"])
        x["worst"] = e["ret"] if x["worst"] is None else min(x["worst"], e["ret"])
    symbols = sorted(by_sym.values(), key=lambda x: -x["pnl"])

    trades = [{"date": t.date.isoformat(), "symbol": t.symbol, "name": names.get(t.symbol, t.symbol),
               "side": t.side, "qty": round(t.qty), "price": round(t.price, 3), "amount": round(t.amount, 2),
               "fee": round(t.fee, 2), "note": t.note}
              for t in res.trades if start <= t.date <= end]
    rebalances = [r for r in res.rebalances if s_iso <= r["date"] <= e_iso]

    closed = [e for e in eps if not e["open"]]
    wins = [e for e in closed if e["pnl"] > 0]
    losses = [e for e in closed if e["pnl"] <= 0]
    summary = {
        "window": window, "start": s_iso, "end": e_iso, "trades": len(trades),
        "buys": sum(1 for t in trades if t["side"] == "buy"), "sells": sum(1 for t in trades if t["side"] == "sell"),
        "fees": round(sum(t["fee"] for t in trades), 2), "closed": len(closed), "open": len(eps) - len(closed),
        "win_rate": round(len(wins) / len(closed), 4) if closed else None,
        "avg_win": round(sum(e["ret"] for e in wins) / len(wins), 4) if wins else None,
        "avg_loss": round(sum(e["ret"] for e in losses) / len(losses), 4) if losses else None,
        "avg_days": round(sum(e["days"] for e in closed) / len(closed), 1) if closed else None,
        "pnl": round(sum(e["pnl"] for e in eps), 2), "symbols": len(symbols),
        "rebalances": sum(1 for r in rebalances if r["kind"] == "rebalance"),
        "stops": sum(len(r["drops"]) for r in rebalances if r["kind"] == "stop"),
        "exit_reasons": _reasons(closed),
    }
    return {"summary": summary, "rebalances": rebalances[::-1], "episodes": eps, "symbols": symbols,
            "trades": trades[::-1][:trade_limit], "trades_total": len(trades)}


# 卖出原因归类：(关键词, 类别)，按顺序匹配
_REASON_GROUPS = (("移动止损", "移动止损"), ("止损", "止损"), ("排名", "掉出排名"), ("过滤", "不满足过滤条件"),
                  ("择时", "大盘择时"), ("范围", "不在选股范围"), ("成交额", "不在选股范围"), ("停牌", "停牌"))


def _reasons(closed: list[dict]) -> list[dict]:
    """卖出原因归类统计。"""
    groups: dict[str, list[dict]] = {}
    for e in closed:
        key = next((g for kw, g in _REASON_GROUPS if kw in e["reason"]), "其他")
        groups.setdefault(key, []).append(e)
    return [{"reason": k, "count": len(v), "pnl": round(sum(e["pnl"] for e in v), 2),
             "avg_ret": round(sum(e["ret"] for e in v) / len(v), 4)} for k, v in groups.items()]
