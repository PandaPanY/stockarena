"""导出与报告（PRD US-6.5）：订单、成交、净值、决策导出为 CSV / JSON；单赛道静态 HTML 报告。

HTML 报告是一个独立文件：样式内嵌，图表用内联 SVG 绘制，离线打开即可查看。
"""

from __future__ import annotations

import csv
import html
import io
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.arena import queries
from app.core.timeutils import utcnow
from app.market.universe import BENCHMARK_NAMES
from app.models import (
    AccountRow,
    AgentRow,
    DecisionRow,
    FillRow,
    NavSnapshotRow,
    OrderRow,
    RoundRow,
    RuleSetRow,
    TrackRow,
)
from app.trading.types import SIDE_LABELS

KINDS = ("orders", "fills", "nav", "decisions")
_CSS = """
:root { --bg:#fcfcfb; --fg:#0b0b0b; --muted:#52514e; --line:#e1e0d9; --card:#ffffff; --warn:#fff4e5; }
@media (prefers-color-scheme: dark) { :root { --bg:#1a1a19; --fg:#f5f5f4; --muted:#c3c2b7; --line:#383835;
  --card:#222220; --warn:#3a2c16; } }
body { background:var(--bg); color:var(--fg); font:14px/1.6 system-ui,-apple-system,"Segoe UI","Microsoft YaHei",
  sans-serif;
  margin:0; padding:24px 16px; }
main { max-width:1000px; margin:0 auto; }
h1 { font-size:22px; margin:0 0 4px; }
h3 { margin:0 0 8px; font-size:16px; }
h4 { margin:12px 0 6px; font-size:13px; }
.muted { color:var(--muted); font-size:12px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:14px 16px; margin:14px 0; }
.meta { display:grid; grid-template-columns:repeat(auto-fill,minmax(220px,1fr)); gap:8px 16px; }
.meta div { display:flex; flex-direction:column; }
table { width:100%; border-collapse:collapse; font-size:12px; font-variant-numeric:tabular-nums; }
th,td { text-align:left; padding:5px 6px; border-bottom:1px solid var(--line); }
th { color:var(--muted); font-weight:500; }
.wide { overflow-x:auto; }
.cols { display:grid; grid-template-columns:repeat(auto-fit,minmax(300px,1fr)); gap:16px; }
.dot { display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:6px; }
.tag { font-size:11px; color:var(--muted); border:1px solid var(--line); border-radius:4px; padding:0 4px; }
.warn { background:var(--warn); border-radius:8px; padding:10px 14px; margin:14px 0; }
.chart { width:100%; height:auto; }
.grid { stroke:var(--line); }
.zero { stroke:var(--muted); stroke-dasharray:2 3; }
.axis { fill:var(--muted); font-size:11px; }
.legend { display:flex; flex-wrap:wrap; gap:4px 14px; font-size:12px; margin-bottom:4px; }
.key i { display:inline-block; width:14px; height:3px; margin-right:5px; vertical-align:middle; }
.analysis li { margin-bottom:6px; }
"""
# 与前端图表一致的类别色（dataviz 参考调色板）
PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]


def _accounts(s: Session, track: TrackRow) -> list[tuple[AccountRow, AgentRow]]:
    return [(a, s.get(AgentRow, a.agent_id))
            for a in s.scalars(select(AccountRow).where(AccountRow.track_id == track.id).order_by(AccountRow.id))]


def rows(s: Session, track: TrackRow, kind: str) -> list[dict]:
    out: list[dict] = []
    for acc, agent in _accounts(s, track):
        base = {"account_id": acc.id, "agent": agent.name}
        if kind == "orders":
            for o in s.scalars(select(OrderRow).where(OrderRow.account_id == acc.id).order_by(OrderRow.id)):
                out.append({**base, "order_id": o.id, "placed_date": str(o.placed_date),
                            "target_date": str(o.target_date or ""), "symbol": o.symbol, "side": o.side,
                            "side_label": SIDE_LABELS.get(o.side, o.side), "order_type": o.order_type, "qty": o.qty,
                            "filled_qty": o.filled_qty, "status": o.status, "limit_price": str(o.limit_price or ""),
                            "stop_price": str(o.stop_price or ""), "forced": o.forced, "reason": o.reason,
                            "note": o.note, "decision_id": o.decision_id})
        elif kind == "fills":
            for f in s.scalars(select(FillRow).where(FillRow.account_id == acc.id).order_by(FillRow.id)):
                out.append({**base, "fill_id": f.id, "order_id": f.order_id, "trade_date": str(f.trade_date),
                            "symbol": f.symbol, "side": f.side, "side_label": SIDE_LABELS.get(f.side, f.side),
                            "qty": f.qty, "price": str(f.price), "amount": str(f.amount), "fee": str(f.total_fee),
                            "realized_pnl": str(f.realized_pnl if f.realized_pnl is not None else "")})
        elif kind == "nav":
            for n in s.scalars(select(NavSnapshotRow).where(NavSnapshotRow.account_id == acc.id)
                               .order_by(NavSnapshotRow.date)):
                out.append({**base, "date": str(n.date), "nav": str(n.nav), "cash": str(n.cash),
                            "market_value": str(n.market_value), "long_value": str(n.long_value or ""),
                            "short_value": str(n.short_value or ""),
                            "benchmark_close": str(n.benchmark_close or "")})
        elif kind == "decisions":
            q = (select(DecisionRow, RoundRow).join(RoundRow, DecisionRow.round_id == RoundRow.id)
                 .where(DecisionRow.account_id == acc.id).order_by(DecisionRow.id))
            for d, r in s.execute(q).all():
                out.append({**base, "decision_id": d.id, "data_date": str(r.data_date), "trigger": r.trigger,
                            "status": d.status, "model": d.model or "", "template": f"{d.template_name or ''}"
                            f"{' v' + str(d.template_version) if d.template_version else ''}",
                            "strategy_version": d.strategy_version or "", "analysis": d.analysis or "",
                            "error": d.error or "", "tokens_in": d.tokens_in, "tokens_out": d.tokens_out,
                            "cost": str(d.cost), "latency_ms": d.latency_ms,
                            "tool_calls": len(d.tool_trace or []), "parsed": d.parsed})
        else:
            raise ValueError(f"不支持的导出类型：{kind}")
    return out


def to_csv(data: list[dict]) -> str:
    """带 BOM 的 UTF-8 CSV，Excel 直接打开不会乱码。复杂字段（dict / list）以 JSON 文本写入。"""
    import json

    buf = io.StringIO()
    buf.write(chr(0xFEFF))   # BOM
    if not data:
        return buf.getvalue()
    writer = csv.DictWriter(buf, fieldnames=list(data[0].keys()))
    writer.writeheader()
    for row in data:
        writer.writerow({k: json.dumps(v, ensure_ascii=False) if isinstance(v, dict | list) else v
                         for k, v in row.items()})
    return buf.getvalue()


# ---------------------------------------------------------------- HTML 报告

def _e(x) -> str:
    return html.escape("" if x is None else str(x))


def _pct(x, digits: int = 2, sign: bool = True) -> str:
    if x is None:
        return "-"
    return f"{x * 100:+.{digits}f}%" if sign else f"{x * 100:.{digits}f}%"


def _num(x, digits: int = 2) -> str:
    return "-" if x is None else f"{x:,.{digits}f}"


def _svg_lines(series: list[tuple[str, str, list[tuple[str, float]]]], height: int = 260, pct: bool = True) -> str:
    """简单折线图：series 为 (名称, 颜色, [(日期, 值)])。所有序列共用日期轴。"""
    dates = sorted({d for _, _, pts in series for d, _ in pts})
    values = [v for _, _, pts in series for _, v in pts]
    if len(dates) < 2 or not values:
        return "<p class='muted'>数据不足，无法绘图</p>"
    w, h, left, right, top, bottom = 760, height, 56, 16, 12, 28
    lo, hi = min(values), max(values)
    if hi == lo:
        hi, lo = hi + 0.01, lo - 0.01
    pad = (hi - lo) * 0.08
    lo, hi = lo - pad, hi + pad
    xi = {d: left + (w - left - right) * i / (len(dates) - 1) for i, d in enumerate(dates)}

    def y(v: float) -> float:
        return top + (h - top - bottom) * (hi - v) / (hi - lo)

    parts = [f"<svg viewBox='0 0 {w} {h}' class='chart' role='img'>"]
    for i in range(5):
        v = lo + (hi - lo) * i / 4
        parts.append(f"<line x1='{left}' x2='{w - right}' y1='{y(v):.1f}' y2='{y(v):.1f}' class='grid'/>")
        label = f"{v * 100:+.1f}%" if pct else f"{v:.2f}"
        parts.append(f"<text x='{left - 6}' y='{y(v) + 4:.1f}' class='axis' text-anchor='end'>{label}</text>")
    for d, anchor in ((dates[0], "start"), (dates[len(dates) // 2], "middle"), (dates[-1], "end")):
        parts.append(f"<text x='{xi[d]:.1f}' y='{h - 8}' class='axis' text-anchor='{anchor}'>{_e(d)}</text>")
    if pct and lo < 0 < hi:
        parts.append(f"<line x1='{left}' x2='{w - right}' y1='{y(0):.1f}' y2='{y(0):.1f}' class='zero'/>")
    for name, color, pts in series:
        if len(pts) < 2:
            continue
        path = " ".join(f"{xi[d]:.1f},{y(v):.1f}" for d, v in pts)
        dash = " stroke-dasharray='5 4'" if name.startswith("基准") else ""
        parts.append(f"<polyline points='{path}' fill='none' stroke='{color}' stroke-width='2'{dash}>"
                     f"<title>{_e(name)}</title></polyline>")
    parts.append("</svg>")
    legend = "".join(f"<span class='key'><i style='background:{c}'></i>{_e(n)}</span>" for n, c, _ in series)
    return f"<div class='legend'>{legend}</div>" + "".join(parts)


def report_html(s: Session, track: TrackRow) -> str:
    board = queries.leaderboard(s, track)
    navs = queries.nav_series(s, track)
    rs = s.get(RuleSetRow, track.rule_set_id)
    risks = queries.lookahead_risks(s, track)
    color = {a["account_id"]: PALETTE[i % len(PALETTE)] for i, a in enumerate(navs["accounts"])}

    ret_series = [(a["name"], color[a["account_id"]], [(d, v - 1) for d, v, _ in a["points"]])
                  for a in navs["accounts"]]
    if navs["benchmark"]["points"]:
        ret_series.append((f"基准·{navs['benchmark']['name']}", "#52514e",
                           [(d, v - 1) for d, v, _ in navs["benchmark"]["points"]]))
    dd_series = []
    for a in navs["accounts"]:
        peak, pts = 0.0, []
        for d, v, _ in a["points"]:
            peak = max(peak, v)
            pts.append((d, v / peak - 1 if peak else 0.0))
        dd_series.append((a["name"], color[a["account_id"]], pts))

    head = ["#", "选手", "净值", "累计收益", "超额收益", "最大回撤", "夏普", "成交", "胜率", "费用", "AI 有效率",
            "AI 费用"]
    body_rows = []
    for r in board:
        ai = r.get("ai") or {}
        body_rows.append("<tr>" + "".join(f"<td>{c}</td>" for c in [
            r["rank"], f"<i class='dot' style='background:{color.get(r['account_id'], '#999')}'></i>"
                       f"{_e(r['agent_name'])}" + (" <span class='tag'>基线</span>" if r["kind"] == "baseline" else ""),
            _num(r.get("nav")), _pct(r.get("cumulative_return")), _pct(r.get("excess_return")),
            _pct(r.get("max_drawdown"), sign=False), _num(r.get("sharpe")), r.get("trades", 0),
            _pct(r.get("win_rate"), 0, False), _num(r.get("total_fees")),
            _pct(ai.get("valid_rate"), 0, False) if ai.get("llm") else "—",
            _num(ai.get("cost")) if ai.get("llm") else "—"]) + "</tr>")

    details = []
    for acc, agent in _accounts(s, track):
        pos = queries.positions(s, acc, track)[:10]
        fills = list(s.scalars(select(FillRow).where(FillRow.account_id == acc.id)
                               .order_by(FillRow.id.desc()).limit(10)))
        decs = s.execute(select(DecisionRow, RoundRow).join(RoundRow, DecisionRow.round_id == RoundRow.id)
                         .where(DecisionRow.account_id == acc.id, DecisionRow.analysis.is_not(None))
                         .order_by(DecisionRow.id.desc()).limit(3)).all()
        pos_html = "".join(f"<tr><td>{_e(p['symbol'])} {_e(p['name'])}</td><td>{p['qty']}</td>"
                           f"<td>{_e(p['avg_cost'])}</td><td>{_e(p['close'])}</td>"
                           f"<td>{_pct(p['pnl_pct'])}</td><td>{_pct(p['weight'], 1, False)}</td></tr>" for p in pos)
        fill_html = "".join(f"<tr><td>{f.trade_date}</td><td>{SIDE_LABELS.get(f.side, f.side)}</td>"
                            f"<td>{_e(f.symbol)}</td><td>{f.qty}</td><td>{f.price}</td>"
                            f"<td>{'' if f.realized_pnl is None else f'{f.realized_pnl:,.2f}'}</td></tr>"
                            for f in fills)
        dec_html = "".join(f"<li><b>{r.data_date}</b>：{_e((d.analysis or '')[:300])}</li>" for d, r in decs)
        details.append(f"""
<section class='card'>
  <h3><i class='dot' style='background:{color.get(acc.id, '#999')}'></i>{_e(agent.name)}
      <span class='muted'>{_e(agent.model or ('基线' if agent.kind == 'baseline' else ''))}</span></h3>
  <div class='cols'>
    <div><h4>持仓（前 10）</h4>
      <table><tr><th>标的</th><th>数量</th><th>成本</th><th>现价</th><th>盈亏</th><th>占净值</th></tr>
      {pos_html or "<tr><td colspan='6' class='muted'>空仓</td></tr>"}</table></div>
    <div><h4>最近成交</h4>
      <table><tr><th>日期</th><th>方向</th><th>标的</th><th>数量</th><th>价格</th><th>已实现盈亏</th></tr>
      {fill_html or "<tr><td colspan='6' class='muted'>暂无成交</td></tr>"}</table></div>
  </div>
  {f"<h4>最近的决策分析</h4><ul class='analysis'>{dec_html}</ul>" if dec_html else ""}
</section>""")

    risk_html = ""
    if risks:
        items = "".join(
            f"<li>{_e(r['agent_name'])}：" + ("知识截止 " + _e(r["cutoff"]) + "，晚于回放开始，<b>前视偏差风险高</b>"
                                             if r["level"] == "high" else
                                             "未填写模型知识截止日期，无法判断" if r["level"] == "unknown" else
                                             "知识截止 " + _e(r["cutoff"]) + "，早于回放开始") + "</li>" for r in risks)
        risk_html = (f"<div class='warn'><b>历史回放（{_e(track.start_date)} ～ {_e(track.replay_until)}）</b>："
                     "模型可能已在训练中见过这段行情。" + ("已开启代码匿名化。" if track.anonymize else "")
                     + f"<ul>{items}</ul></div>")

    days = max((b.get("days") or 0) for b in board) if board else 0
    sample = "<p class='muted'>运行不足 20 个交易日，结果主要反映短期波动，仅供参考。</p>" if days < 20 else ""
    meta = [
        ("市场 / 货币", f"{track.market} / {track.currency}"),
        ("股票池", f"{track.universe_name}（{len(track.symbols)} 个标的）"),
        ("业绩基准", BENCHMARK_NAMES.get(track.benchmark_symbol, track.benchmark_symbol)),
        ("规则集", rs.title if rs else "-"), ("初始本金", f"{track.initial_cash:,.0f}"),
        ("运行区间", f"{track.start_date or '-'} ～ {track.last_settled_date or '-'}（{days} 个交易日）"),
        ("决策方式", ("目标仓位" if track.output_mode == "targets" else "直接下单") + " · "
         + ("工具调用" if track.decision_mode == "tools" else "快照") + " · "
         + ("分钟级" if track.frequency == "intraday" else "日频")),
        ("运行模式", "历史回放" if track.mode == "replay" else "实时模拟"),
    ]
    meta_html = "".join(f"<div><span class='muted'>{_e(k)}</span><b>{_e(v)}</b></div>" for k, v in meta)
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(track.name)} · 赛道报告</title>
<style>
{_CSS}</style></head>
<body><main>
<h1>{_e(track.name)}</h1>
<p class="muted">StockArena 赛道报告 · 生成于 {utcnow():%Y-%m-%d %H:%M} UTC ·
本报告仅用于模拟交易与 AI 能力研究，不构成投资建议</p>
{_e(track.description) and f"<p>{_e(track.description)}</p>"}
{risk_html}
<section class="card"><div class="meta">{meta_html}</div></section>
<section class="card"><h3>排行榜</h3>{sample}<div class="wide"><table>
<tr>{"".join(f"<th>{h}</th>" for h in head)}</tr>{"".join(body_rows)}</table></div></section>
<section class="card"><h3>累计收益</h3>{_svg_lines(ret_series)}</section>
<section class="card"><h3>回撤</h3>{_svg_lines(dd_series, height=180)}</section>
{"".join(details)}
</main></body></html>"""


def filename(track: TrackRow, suffix: str) -> str:
    return f"track{track.id}_{date.today():%Y%m%d}_{suffix}"
