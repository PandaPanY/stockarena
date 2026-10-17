"""策略体检报告：一页汇总冠军版本的表现、过拟合检验、规则、AI 优化过程和可实盘检查，导出为独立的 HTML 文件
（样式内嵌、图表为内联 SVG，离线打开即可查看，方便分享）。

数据都来自已有的计算：冠军版本的回测报告、research_quality、DSR、优化时间线、可实盘检查。
报告固定附上数据来源与免责声明。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.arena.deps import universe_resolver
from app.arena.export import _CSS, PALETTE, _e, _num, _pct, _svg_lines
from app.core.config import get_settings
from app.core.timeutils import to_market_time, utcnow
from app.market.universe import BENCHMARK_NAMES
from app.models.quant import QuantStrategyRow, QuantVersionRow
from app.quant import service, timeline

_EXTRA_CSS = """
.removed { color:#c0392b; text-decoration:line-through; }
.added { color:#1b8a5a; }
.pass { color:#1b8a5a; } .fail { color:#c0392b; } .pending { color:#b07800; }
.big { font-size:20px; font-weight:600; }
.explain { font-size:12px; color:var(--muted); margin:4px 0 0; }
"""

_PERIODS = (("开发期", "dev_full"), ("其中训练", "train_full"), ("其中验证", "valid_full"), ("留出期", "holdout_full"))
_LEVEL = {"pass": "通过", "fail": "未通过", "pending": "待观察", "info": "参考"}


def _period_rows(detail: dict) -> str:
    rows = []
    for label, key in _PERIODS:
        r = detail.get(key) or {}
        if not r.get("days"):
            continue
        rows.append(
            f"<tr><td>{label}</td><td>{_e(r.get('start'))} ～ {_e(r.get('end'))}</td>"
            f"<td>{_pct(r.get('annualized_return'))}</td><td>{_pct(r.get('cumulative_return'))}</td>"
            f"<td>{_pct(r.get('benchmark_return'))}</td><td>{_pct(r.get('excess_return'))}</td>"
            f"<td>{_num(r.get('sharpe'))}</td><td>{_pct(r.get('max_drawdown'), sign=False)}</td>"
            f"<td>{_pct(r.get('volatility'), sign=False)}</td><td>{_num(r.get('turnover'), 1)} 倍</td>"
            f"<td>{_pct(r.get('cost_ratio'), sign=False)}</td></tr>")
    return ("<table><tr><th>区间</th><th>日期</th><th>年化</th><th>累计</th><th>基准</th><th>超额</th><th>夏普</th>"
            "<th>最大回撤</th><th>波动</th><th>年化换手</th><th>年化成本</th></tr>" + "".join(rows) + "</table>")


def _yearly_rows(detail: dict) -> str:
    rows = []
    for key, tag in (("dev_full", ""), ("holdout_full", "留出")):
        for y in (detail.get(key) or {}).get("yearly") or []:
            label = f"{y['year']}" + (f" <span class='tag'>{tag}</span>" if tag else "") + \
                (" <span class='tag'>不满一年</span>" if y.get("partial") else "")
            rows.append(f"<tr><td>{label}</td><td>{_pct(y.get('return'))}</td><td>{_pct(y.get('benchmark'))}</td>"
                        f"<td>{_pct(y.get('excess'))}</td><td>{_pct(y.get('max_drawdown'), sign=False)}</td></tr>")
    if not rows:
        return "<p class='muted'>没有分年数据</p>"
    return ("<table><tr><th>年份</th><th>收益</th><th>基准</th><th>超额</th><th>最大回撤</th></tr>"
            + "".join(rows) + "</table>")


def _curve(detail: dict, bench_name: str) -> str:
    c = detail.get("curves") or {}
    dates = c.get("dates") or []
    if len(dates) < 2:
        return "<p class='muted'>没有净值曲线</p>"
    series = [("冠军版本", PALETTE[0], [(d, v - 1) for d, v in zip(dates, c.get("nav") or [], strict=False)]),
              (f"基准（{bench_name}）", PALETTE[1],
               [(d, v - 1) for d, v in zip(dates, c.get("bench") or [], strict=False)])]
    return _svg_lines(series)


def _overfit(s: Session, st: QuantStrategyRow, champ: QuantVersionRow) -> str:
    q = service.research_quality(s, st)
    trials = len(service.trial_scores(s, st))
    dsr = service.dsr_of(s, st, champ)
    wf = q.get("walk_forward") or {}
    items = [
        ("折算可信度（DSR）", _pct(dsr, 0, sign=False) if dsr is not None else "-",
         f"扣除「试了 {trials} 次挑最好的」之后，冠军开发期夏普真实大于 0 的概率。越接近 100% 越可信。"),
        ("累计尝试次数", str(trials),
         "这条策略（含另存来源、新建前的试算）至今回测过的规则版本数。试得越多，偶然好看的概率越大。"),
        ("当前冠军以来已检验候选", str(q.get("tests_since_champion") or 0),
         "升级检验的显著性门槛按这个数收紧（p 需不大于 0.05 ÷ 候选数）。"),
        ("回测过拟合概率（PBO）", _pct(q.get("pbo"), 0, sign=False) if q.get("pbo") is not None else "版本太少",
         "在全部版本的开发期月度收益上做组合交叉验证。接近 50% 说明「开发期挑出来的最好版本」在样本外只是随机水平。"),
        ("开发期与留出期排名相关", _num(q.get("rank_corr")) if q.get("rank_corr") is not None else "版本太少",
         "开发期越好的版本，留出期是否也越好。≤ 0 说明继续在开发期上优化没有样本外价值。"),
    ]
    if wf.get("n"):
        verdict = f"{wf['beat_seed']}/{wf['n']} 年好于初始版本，{wf['beat_avg']}/{wf['n']} 年好于平均"
        items.append(("滚动选择检验", verdict,
                      f"每年初只用之前的数据挑版本，看下一年的表现。累计：挑选 {_pct(wf.get('chosen_total'))}，"
                      f"一直用初始版本 {_pct(wf.get('seed_total'))}，所有版本平均 {_pct(wf.get('avg_total'))}。"))
    cells = "".join(f"<div><span class='muted'>{_e(k)}</span><span class='big'>{_e(v)}</span>"
                    f"<p class='explain'>{_e(x)}</p></div>" for k, v, x in items)
    return f"<div class='meta'>{cells}</div>"


def _ai_process(tl: dict) -> str:
    sm = tl["summary"]
    head = (f"<p>共 {sm['versions']} 个版本；AI 优化 {sm['rounds']} 轮，提出 {sm['ai_candidates']} 个候选，"
            f"采纳 {sm['ai_promoted']} 个；手动修改 {sm['manual']} 次。只看开发期，留出期不参与任何决定。</p>")
    adopted = []
    for e in tl["events"]:
        for c in e["candidates"]:
            if c.get("status") in ("champion", "retired") and c.get("number"):
                diff = c.get("diff") or {}
                lines = "".join(f"<div class='removed'>− {_e(x)}</div>" for x in diff.get("removed", [])) + \
                    "".join(f"<div class='added'>＋ {_e(x)}</div>" for x in diff.get("added", []))
                adopted.append(f"<li><b>v{c['number']} {_e(c.get('title'))}</b>（{_e(c.get('outcome'))}）<br>"
                               f"<span class='muted'>为什么改：{_e(c.get('rationale'))}</span>{lines}"
                               f"<div class='muted'>结论：{_e(c.get('decision'))}</div></li>")
    body = (f"<h4>被采纳的改动</h4><ul class='analysis'>{''.join(adopted)}</ul>" if adopted
            else "<p class='muted'>还没有 AI 提出的改动被采纳（升级检验很严格，大多数候选都过不了）。</p>")
    rounds = [e for e in tl["events"] if e["type"] == "round"][-5:]
    if rounds:
        rows = "".join(
            f"<tr><td>{_e(e['at'][:10])}</td><td>研究 #{e['run_id']} 第 {e['round']} 轮</td>"
            f"<td>{'升级到 v' + str(e['promoted']) if e.get('promoted') else '未升级'}</td>"
            f"<td>{_e(_candidates_text(e))}</td></tr>"
            for e in reversed(rounds))
        body += ("<h4>最近几轮</h4><table><tr><th>日期</th><th>轮次</th><th>结果</th><th>候选</th></tr>"
                 + rows + "</table>")
    return head + body


def _candidates_text(event: dict) -> str:
    return "；".join(f"{c.get('title')}（{c.get('outcome')}）" for c in event["candidates"])


def _readiness(st: QuantStrategyRow) -> str:
    r = st.readiness or {}
    if not r.get("items"):
        return "<p class='muted'>还没有可实盘检查的结果（开始实时跟踪后每晚自动检查）。</p>"
    rows = "".join(f"<tr><td>{_e(i['name'])}</td><td class='{_e(i['level'])}'>{_e(_LEVEL.get(i['level'], i['level']))}"
                   f"</td><td>{_e(i['detail'])}</td></tr>" for i in r["items"])
    return (f"<p><b>{_e(r.get('status'))}</b>：{_e(r.get('message'))}</p>"
            "<table><tr><th>条件</th><th>结果</th><th>说明</th></tr>" + rows + "</table>")


def render(s: Session, st: QuantStrategyRow, include_private: bool) -> str:
    """生成报告 HTML。include_private：是否包含所有者私有的内容（可实盘检查涉及实时跟踪账户）。"""
    champ = s.get(QuantVersionRow, st.champion_id) if st.champion_id else None
    if champ is None:
        raise service.LabError("策略还没有冠军版本")
    detail = service.version_detail(champ)
    tl = timeline.build(s, st)
    bench_name = BENCHMARK_NAMES.get(st.benchmark, st.benchmark)
    now = to_market_time(utcnow(), "CN").strftime("%Y-%m-%d %H:%M")
    src = get_settings().source_url
    rules = "".join(f"<li>{_e(r)}</li>" for r in detail.get("rules") or [])
    styles = {x["style"]: x["title"] for x in service.load_seeds()}
    uspec = universe_resolver().specs.get(st.universe_name)
    meta = [("风格", styles.get(st.style, "自定义")), ("股票池", uspec.title if uspec else st.universe_name),
            ("基准", f"{bench_name}（{st.benchmark}）"),
            ("冠军版本", f"v{champ.number} {champ.title}"), ("版本数", tl["summary"]["versions"]),
            ("生成时间", f"{now}（北京时间）")]
    meta_html = "".join(f"<div><span class='muted'>{_e(k)}</span><span>{_e(v)}</span></div>" for k, v in meta)
    private = (f"<section class='card'><h3>可实盘检查</h3>{_readiness(st)}</section>" if include_private else "")
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(st.name)} · 策略体检报告</title><style>{_CSS}{_EXTRA_CSS}</style></head>
<body><main>
<h1>{_e(st.name)} · 策略体检报告</h1>
<p class="muted">{_e(st.description)}</p>
<div class="warn">本报告由 StockArena 根据历史数据回测生成，<b>不构成任何投资建议</b>，回测和模拟结果不代表未来收益。
开发期用于研究和挑选版本，留出期从不参与任何决定，是样本外的检验；两者差距大说明可能过拟合。</div>
<section class="card"><div class="meta">{meta_html}</div></section>
<section class="card"><h3>表现</h3><div class="wide">{_period_rows(detail)}</div></section>
<section class="card"><h3>净值（相对起点的累计收益）</h3>{_curve(detail, bench_name)}</section>
<section class="card"><h3>分年收益</h3><div class="wide">{_yearly_rows(detail)}</div></section>
<section class="card"><h3>过拟合检验</h3>{_overfit(s, st, champ)}</section>
<section class="card"><h3>当前规则（v{champ.number}）</h3><ul>{rules}</ul></section>
<section class="card"><h3>AI 优化过程</h3>{_ai_process(tl)}</section>
{private}
<section class="card"><h3>数据与声明</h3>
<p class="muted">行情、财报、指数成分等数据由部署者从第三方公开接口获取
（新浪财经、腾讯财经、东方财富、同花顺、巨潮资讯、中证指数、Baostock 等），可能延迟、缺失或出错。
回测按 A 股规则模拟（T+1、整手、涨跌停、停牌、佣金与印花税），但无法完全反映真实成交。</p>
<p class="muted">本报告仅用于学习和策略研究，不构成投资建议。是否交易、如何交易由读者自行决定并承担后果。</p>
<p class="muted">生成工具：StockArena（AGPL-3.0）<a href="{_e(src)}">{_e(src)}</a></p>
</section>
</main></body></html>"""
