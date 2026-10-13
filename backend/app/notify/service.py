"""消息推送服务：每日调仓提醒与异常提醒。

- 调仓提醒（orders）：跟踪中的策略 / 组合在最近一次决策中生成了下一交易日的订单时，推送调仓清单；
  同一账户同一数据日期只推送一次（按通道去重，失败的下次检查时重试）。没有调仓的日子不推送。
- 异常提醒（errors）：晚间最后一次检查时，若某个跟踪账户没有最近一个交易日的决策，推送提醒。
- 健康度与可实盘（health）：每晚刷新「可实盘」检查后，健康度为「提醒」时推送（持续期间每周最多一次）；
  「可实盘」结论变为通过、或由通过变为未通过时推送一次。

发送在数据库事务之外进行（网络请求较慢，不能持有 SQLite 写锁）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.arena.deps import market_service, secret_box
from app.arena.lifecycle import latest_complete_date
from app.core.config import get_settings
from app.core.crypto import UNDECRYPTABLE
from app.core.timeutils import to_market_time, utcnow
from app.db.session import get_sessionmaker
from app.models import (
    DEFAULT_USER_ID,
    AccountRow,
    DailyBarRow,
    DecisionRow,
    InstrumentRow,
    NavSnapshotRow,
    OrderRow,
    RoundRow,
    TrackRow,
)
from app.models.notify import NotificationRow, NotifyChannelRow
from app.models.quant import QuantPortfolioRow, QuantStrategyRow
from app.notify import channels

logger = logging.getLogger(__name__)

EVENTS = {"orders": "调仓提醒", "errors": "异常提醒", "health": "健康度与可实盘", "skills": "技能申请",
          "watch": "自选股提醒"}
MAX_LINES = 60
SIDE = {"buy": "买入", "sell": "卖出", "short": "卖空", "cover": "平空"}


@dataclass
class Message:
    event: str
    key: str            # 去重键
    title: str
    body: str
    user_id: int = DEFAULT_USER_ID      # 接收者：只发到该用户自己配置的通道（系统类提醒发给管理员）


# ---------------------------------------------------------------- 发送


def deliver(msg: Message, sender=channels.send) -> list[dict]:
    """把消息发到接收者订阅了该事件的启用通道（已成功发送过的跳过）。返回各通道结果。"""
    with get_sessionmaker()() as s:
        rows = list(s.scalars(select(NotifyChannelRow).where(NotifyChannelRow.enabled.is_(True),
                                                             NotifyChannelRow.user_id == msg.user_id)))
        targets = []
        for ch in rows:
            if msg.event not in (ch.events or []):
                continue
            done = s.scalars(select(NotificationRow).where(NotificationRow.channel_id == ch.id,
                                                           NotificationRow.dedupe_key == msg.key)).first()
            if done is not None and done.status == "ok":
                continue
            targets.append((ch.id, ch.kind, secret_box().try_decrypt(ch.secret_enc)))
    results = []
    for ch_id, kind, secret in targets:
        try:
            if secret is None:
                raise channels.NotifyError(UNDECRYPTABLE)
            sender(kind, secret, msg.title, msg.body)
            status, error = "ok", ""
        except channels.NotifyError as e:
            status, error = "failed", str(e)
        except Exception as e:  # noqa: BLE001 - 推送失败不影响其他通道与日终流程
            status, error = "failed", f"{type(e).__name__}"
        with get_sessionmaker()() as s:
            row = s.scalars(select(NotificationRow).where(NotificationRow.channel_id == ch_id,
                                                          NotificationRow.dedupe_key == msg.key)).first()
            row = row or NotificationRow(channel_id=ch_id, dedupe_key=msg.key)
            row.event, row.title, row.body, row.status, row.error = msg.event, msg.title, msg.body, status, error
            row.created_at = utcnow()
            s.add(row)
            ch = s.get(NotifyChannelRow, ch_id)
            ch.last_status, ch.last_error, ch.last_sent_at = status, error, utcnow()
            s.commit()
        if status != "ok":
            logger.warning("推送失败（通道 %s）：%s", ch_id, error)
        results.append({"channel_id": ch_id, "status": status, "error": error})
    return results


# ---------------------------------------------------------------- 内容


def _followed(s: Session) -> list[tuple[str, str, int, AccountRow, TrackRow]]:
    """跟踪中的策略与组合：[(类型, 名称, id, 账户, 赛道)]。"""
    out = []
    for kind, model in (("strategy", QuantStrategyRow), ("portfolio", QuantPortfolioRow)):
        for row in s.scalars(select(model).where(model.status == "active", model.follow_account_id.is_not(None))):
            acc = s.get(AccountRow, row.follow_account_id)
            track = s.get(TrackRow, acc.track_id) if acc else None
            if acc and track and track.status == "running":
                out.append((kind, row.name, row.id, acc, track))
    return out


def _link(kind: str, ref_id: int) -> str:
    base = get_settings().public_url
    if not base:
        return ""
    path = f"/lab/{ref_id}" if kind == "strategy" else "/portfolios"
    return f"{base.rstrip('/')}{path}"


def orders_message(s: Session, kind: str, name: str, ref_id: int, acc: AccountRow,
                   today: date | None = None) -> Message | None:
    """账户最近一次决策的调仓清单；没有决策、没有订单或订单的执行日已过时返回 None。"""
    last = s.execute(select(DecisionRow, RoundRow).join(RoundRow, DecisionRow.round_id == RoundRow.id)
                     .where(DecisionRow.account_id == acc.id).order_by(RoundRow.data_date.desc(),
                                                                       DecisionRow.id.desc()).limit(1)).first()
    if last is None:
        return None
    dec, rnd = last
    if today is not None and rnd.fill_date is not None and rnd.fill_date < today:
        return None                     # 执行日已过（例如刚配置通道时的旧决策），不再提醒
    orders = list(s.scalars(select(OrderRow).where(OrderRow.decision_id == dec.id,
                                                   OrderRow.status.in_(["pending", "filled", "partially_filled"]))))
    if not orders:
        return None
    syms = {o.symbol for o in orders}
    names = {r.symbol: r.name for r in s.scalars(select(InstrumentRow).where(InstrumentRow.symbol.in_(syms)))}
    px = {r.symbol: float(r.close) for r in s.scalars(select(DailyBarRow).where(
        DailyBarRow.symbol.in_(syms), DailyBarRow.date == rnd.data_date))}
    nav = s.scalars(select(NavSnapshotRow.nav).where(NavSnapshotRow.account_id == acc.id)
                    .order_by(NavSnapshotRow.date.desc()).limit(1)).first()
    sells = sorted((o for o in orders if o.side in ("sell", "cover")), key=lambda o: -o.qty * px.get(o.symbol, 0))
    buys = sorted((o for o in orders if o.side in ("buy", "short")), key=lambda o: -o.qty * px.get(o.symbol, 0))
    amount = {o.id: o.qty * px.get(o.symbol, 0.0) for o in orders}
    fill = rnd.fill_date.strftime("%m-%d") if rnd.fill_date else "下一交易日"
    title = f"【调仓提醒】{name}：{fill} 开盘 卖 {len(sells)} 买 {len(buys)}"
    lines = [f"**{name}**（依据 {rnd.data_date:%m-%d} 收盘，**{fill} 开盘**执行）"]
    if nav is not None:
        lines.append(f"模拟账户净值 {float(nav):,.0f} 元")
    lines.append(f"卖出 {len(sells)} 笔约 {sum(amount[o.id] for o in sells):,.0f} 元；"
                 f"买入 {len(buys)} 笔约 {sum(amount[o.id] for o in buys):,.0f} 元")
    lines.append("")
    shown = 0
    for group, label in ((sells, "卖出"), (buys, "买入")):
        if not group:
            continue
        lines.append(f"**{label}**")
        for o in group:
            if shown >= MAX_LINES:
                break
            p = px.get(o.symbol)
            lines.append(f"- {SIDE.get(o.side, o.side)} {o.symbol.split('.')[0]} {names.get(o.symbol, '')} "
                         f"{o.qty} 股" + (f" × {p:.2f} ≈ {amount[o.id]:,.0f} 元" if p else ""))
            shown += 1
    if shown < len(orders):
        lines.append(f"……其余 {len(orders) - shown} 笔请在网页查看")
    if kind == "portfolio":
        lines += _review_lines(s, ref_id, rnd.data_date)
    lines.append("")
    lines.append("金额按收盘价估算；实盘请按自己的持仓与资金核对后下单。")
    link = _link(kind, ref_id)
    if link:
        lines.append(f"详情：{link}")
    return Message("orders", f"orders:{acc.id}:{rnd.data_date.isoformat()}", title, "\n".join(lines))


def _review_lines(s: Session, portfolio_id: int, data_date: date) -> list[str]:
    """组合开启 AI 复核时，同一天 AI 复核账户的结论（否决了哪些新买入、为什么）。"""
    pf = s.get(QuantPortfolioRow, portfolio_id)
    cfg = ((pf.settings or {}).get("review") or {}) if pf else {}
    if not cfg.get("enabled") or not cfg.get("account_id"):
        return []
    dec = s.scalars(select(DecisionRow).join(RoundRow, DecisionRow.round_id == RoundRow.id)
                    .where(DecisionRow.account_id == cfg["account_id"], RoundRow.data_date == data_date)
                    .order_by(DecisionRow.id.desc()).limit(1)).first()
    rv = ((dec.parsed or {}).get("review") or {}) if dec else {}
    items = rv.get("items") or []
    if not dec or not items:
        return []
    vetoed = [x for x in items if x.get("decision") == "veto"]
    lines = ["", f"**AI 复核**（{rv.get('model') or '模型'}）：新买入 {len(items)} 只，否决 {len(vetoed)} 只"]
    for x in vetoed[:20]:
        lines.append(f"- 否决 {x['symbol'].split('.')[0]} {x.get('name', '')}：{x.get('reason', '')}")
    if vetoed:
        lines.append("（「AI 复核」对照账户不买入以上股票，其余与规则一致）")
    if rv.get("errors"):
        lines.append(f"复核失败 {len(rv['errors'])} 批，相应股票按规则执行")
    return lines


def daily_check(final: bool = False, sender=channels.send, now: datetime | None = None) -> list[dict]:
    """收盘后检查并推送调仓提醒；final=True（晚间最后一次）时对没有完成决策的跟踪账户推送异常提醒。"""
    messages: list[Message] = []
    today = to_market_time(now or utcnow(), "CN").date()
    with get_sessionmaker()() as s:
        if not s.scalars(select(NotifyChannelRow.id).where(NotifyChannelRow.enabled.is_(True))).first():
            return []
        followed = _followed(s)
        latest = None
        if final and followed:
            cal = market_service(s, "CN").calendar()
            latest = latest_complete_date(cal, to_market_time(now or utcnow(), "CN"))
        for kind, name, ref_id, acc, track in followed:
            msg = orders_message(s, kind, name, ref_id, acc, today)
            if msg:
                msg.user_id = track.user_id        # 发给策略 / 组合的所有者
                messages.append(msg)
            if latest is not None:
                last_date = s.scalar(select(RoundRow.data_date).join(DecisionRow, DecisionRow.round_id == RoundRow.id)
                                     .where(DecisionRow.account_id == acc.id)
                                     .order_by(RoundRow.data_date.desc()).limit(1))
                if last_date is None or last_date < latest:
                    messages.append(Message(
                        "errors", f"missing:{acc.id}:{latest.isoformat()}",
                        f"【异常提醒】{name}：{latest:%m-%d} 未完成决策",
                        f"**{name}** 的实时跟踪还没有 {latest:%Y-%m-%d} 的决策（最近一次："
                        f"{last_date or '无'}）。\n可能是行情数据延迟或服务异常，请检查系统状态。"
                        + (f"\n{_link(kind, ref_id)}" if _link(kind, ref_id) else ""), track.user_id))
    out = []
    for m in messages:
        out += [r | {"title": m.title} for r in deliver(m, sender)]
    return out


LEVEL_MARK = {"pass": "✅", "fail": "❌", "pending": "⏳", "info": "ℹ️", "提醒": "❗", "关注": "⚠️", "正常": "✅"}


def readiness_messages(kind: str, name: str, ref_id: int, prev: dict | None, new: dict,
                       today: date | None = None) -> list[Message]:
    """比较前后两次「可实盘」检查，生成健康度提醒与结论变化提醒。"""
    today = today or to_market_time(utcnow(), "CN").date()
    link = _link(kind, ref_id)
    out: list[Message] = []
    hl = new.get("health") or {}
    if hl.get("status") == "提醒":
        year, week, _ = today.isocalendar()
        lines = [f"**{name}** 的实时跟踪表现明显差于回测历史：", ""]
        lines += [f"- {LEVEL_MARK.get(c['level'], '')} {c['item']}：{c['detail']}" for c in hl.get("checks", [])]
        lines += ["", hl.get("message", ""), "策略可能失效或市场环境变化，建议复查后再决定是否继续按提醒调仓。"]
        if link:
            lines.append(f"详情：{link}")
        out.append(Message("health", f"health:{kind}:{ref_id}:{year}-W{week:02d}",
                           f"【健康度提醒】{name}：跟踪期表现明显偏弱", "\n".join(lines)))
    before, after = (prev or {}).get("status"), new.get("status")
    if after != before and (after == "可实盘" or before == "可实盘"):
        title = (f"【可实盘】{name} 通过全部检查" if after == "可实盘"
                 else f"【状态变化】{name}：可实盘 → {after}")
        lines = [f"**{name}**：{before or '未检查'} → **{after}**", "", new.get("message", ""), ""]
        lines += [f"- {LEVEL_MARK.get(it['level'], '')} {it['name']}：{it['detail']}" for it in new.get("items", [])]
        if link:
            lines.append(f"详情：{link}")
        out.append(Message("health", f"ready:{kind}:{ref_id}:{after}:{today.isoformat()}", title, "\n".join(lines)))
    return out


def data_quality_message(issues: list[dict], today: date | None = None) -> Message:
    """每晚数据质量扫描新发现的错误。"""
    today = today or to_market_time(utcnow(), "CN").date()
    lines = [f"每晚数据扫描新发现 {len(issues)} 处行情异常，可能让回测出现假收益：", ""]
    lines += [f"- {x['symbol'].split('.')[0]} {x['name']} {x['date']}：{x['detail']}" for x in issues[:20]]
    if len(issues) > 20:
        lines.append(f"……其余 {len(issues) - 20} 处请在网页查看")
    base = get_settings().public_url
    lines += ["", "可在「系统状态 → 数据质量」中重新下载该标的数据（之后自动重新回测），或确认无误后忽略。"]
    if base:
        lines.append(f"{base.rstrip('/')}/system")
    return Message("errors", f"dq:{today.isoformat()}", f"【数据异常】新发现 {len(issues)} 处行情异常",
                   "\n".join(lines))


def test_message() -> Message:
    return Message("test", f"test:{utcnow().isoformat()}", "【测试】StockArena 推送测试",
                   "这是一条测试消息。收到说明推送通道配置正确。\n\n每个交易日收盘后，跟踪中的组合有调仓时会推送调仓清单。")

