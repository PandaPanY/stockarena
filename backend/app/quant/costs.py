"""交易成本设置：佣金费率、最低佣金、是否扣红利税。每个用户各自设置，对该用户的策略回测与实时跟踪生效。

- 回测：run_backtest 未指定成本模型时使用「当前用户」的设置（current()）。当前用户由 use(uid) 设定（上下文变量，
  随 asyncio.to_thread 传入线程）；没有设定时为管理员。计算某个策略 / 组合时一律用它的所有者的设置（公共策略即管理员）；
- 实时跟踪：保存时同步到该用户运行中的量化跟踪赛道的规则参数（新建跟踪时按赛道所有者套用）；
- 修改后（或升级了成本计算口径 COST_LOGIC 后）需要重新回测该用户所有策略的版本，启动时会自动检查并在后台执行。
- 存储：app_settings 的 trade_costs（管理员）/ trade_costs@用户ID（其他用户），未设置时用默认值。
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DEFAULT_USER_ID, AccountRow, AgentRow, TrackRow
from app.models.settings import AppSettingRow, user_key
from app.quant.backtest import CostModel

KEY = "trade_costs"
EVALUATED_KEY = "evaluated_costs"
# 成本计算口径版本：回测的成本计算逻辑有变化时加 1（2：A股按持有期扣红利税）
COST_LOGIC = 2


class CostError(ValueError):
    pass


@dataclass(frozen=True)
class TradeCosts:
    commission_rate: float = 0.00025        # 佣金费率（双边），万 2.5
    commission_min: float = 5.0             # 单笔最低佣金（元）；「免五」券商填 0
    dividend_tax: bool = True               # A股股票按持有期扣红利税（≤1 个月 20%、≤1 年 10%、>1 年免征）

    @classmethod
    def from_dict(cls, d: dict | None) -> TradeCosts:
        base = cls()
        d = d or {}
        out = cls(commission_rate=float(d.get("commission_rate", base.commission_rate)),
                  commission_min=float(d.get("commission_min", base.commission_min)),
                  dividend_tax=bool(d.get("dividend_tax", base.dividend_tax)))
        out.validate()
        return out

    def validate(self) -> None:
        if not 0 <= self.commission_rate <= 0.003:
            raise CostError("佣金费率需要在 0 ～ 千分之三之间")
        if not 0 <= self.commission_min <= 50:
            raise CostError("最低佣金需要在 0 ～ 50 元之间")

    def to_dict(self) -> dict:
        return asdict(self)

    def signature(self) -> str:
        return json.dumps({**self.to_dict(), "logic": COST_LOGIC}, sort_keys=True)

    def cost_model(self) -> CostModel:
        return CostModel(commission_rate=self.commission_rate, commission_min=self.commission_min,
                         dividend_tax=self.dividend_tax)

    def rule_overrides(self) -> dict:
        """写入跟踪赛道规则参数的部分（JSON 形式，与 RuleParams.model_dump(mode='json') 一致）。"""
        return {"commission_rate": str(Decimal(str(self.commission_rate))),
                "commission_min": str(Decimal(str(self.commission_min))),
                "cn_dividend_tax_by_holding": self.dividend_tax}

    def describe(self) -> str:
        rate = f"万 {self.commission_rate * 1e4:g}"
        low = f"最低 {self.commission_min:g} 元" if self.commission_min else "免五"
        return f"佣金 {rate}（{low}）；红利税{'按持有期扣除' if self.dividend_tax else '不扣'}"


_by_user: dict[int, TradeCosts] = {}
_lock = threading.Lock()
_ctx_user: ContextVar[int | None] = ContextVar("costs_user", default=None)


@contextmanager
def use(uid: int | None) -> Iterator[None]:
    """在此范围内 current() 返回 uid 的设置（计算某个用户的策略 / 组合时使用）。"""
    token = _ctx_user.set(uid)
    try:
        yield
    finally:
        _ctx_user.reset(token)


def for_user(uid: int) -> TradeCosts:
    """某个用户的设置（内存缓存；首次读取数据库）。"""
    with _lock:
        hit = _by_user.get(uid)
    if hit is not None:
        return hit
    from app.db.session import get_sessionmaker

    with get_sessionmaker()() as s:
        return load(s, uid)


def current() -> TradeCosts:
    uid = _ctx_user.get()
    return for_user(DEFAULT_USER_ID if uid is None else uid)


def _row(s: Session, key: str) -> AppSettingRow | None:
    return s.get(AppSettingRow, key)


def load(s: Session, uid: int = DEFAULT_USER_ID) -> TradeCosts:
    """从数据库读取某个用户的设置并缓存（启动时读取管理员的）。"""
    row = _row(s, user_key(KEY, uid))
    costs = TradeCosts.from_dict(row.value) if row else TradeCosts()
    with _lock:
        _by_user[uid] = costs
    return costs


def _set(s: Session, key: str, value: dict) -> None:
    row = _row(s, key) or AppSettingRow(key=key)
    row.value = value
    s.add(row)


def save(s: Session, costs: TradeCosts, uid: int = DEFAULT_USER_ID) -> int:
    """保存某个用户的设置、同步到该用户运行中的跟踪赛道并清空回测缓存。返回同步的赛道数（调用方提交事务）。"""
    costs.validate()
    _set(s, user_key(KEY, uid), costs.to_dict())
    with _lock:
        _by_user[uid] = costs
    clear_caches()
    return sync_tracks(s, costs, uid)


def evaluated_signature(s: Session, uid: int = DEFAULT_USER_ID) -> str | None:
    row = _row(s, user_key(EVALUATED_KEY, uid))
    return (row.value or {}).get("signature") if row else None


def mark_evaluated(s: Session, signature: str, uid: int = DEFAULT_USER_ID) -> None:
    _set(s, user_key(EVALUATED_KEY, uid), {"signature": signature})


def quant_tracks(s: Session, uid: int | None = None) -> list[TrackRow]:
    """运行中（含暂停）的量化跟踪赛道；给出 uid 时只取该用户的。"""
    ids = set(s.scalars(select(AccountRow.track_id).join(AgentRow, AccountRow.agent_id == AgentRow.id)
                        .where(AgentRow.kind == "quant")))
    if not ids:
        return []
    q = select(TrackRow).where(TrackRow.id.in_(ids), TrackRow.status.in_(("running", "paused")))
    if uid is not None:
        q = q.where(TrackRow.user_id == uid)
    return list(s.scalars(q))


def apply_to_track(track: TrackRow, costs: TradeCosts | None = None) -> None:
    """把成本设置写入赛道规则参数；未指定时用赛道所有者的设置。"""
    track.rule_params = {**(track.rule_params or {}), **(costs or for_user(track.user_id)).rule_overrides()}


def sync_tracks(s: Session, costs: TradeCosts | None = None, uid: int = DEFAULT_USER_ID) -> int:
    tracks = quant_tracks(s, uid)
    for t in tracks:
        apply_to_track(t, costs)
    return len(tracks)


def clear_caches() -> None:
    """成本变化后，内存中按旧成本算出的回测结果全部作废。"""
    from app.quant import allocation, capacity, details, portfolio

    for mod in (allocation, capacity, details, portfolio):
        mod._cache.clear()
