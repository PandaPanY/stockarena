"""竞技业务表：模型供应商、选手、规则集、赛道、账户、订单、成交、流水、净值、决策。"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import JSON, Boolean, Date, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.timeutils import utcnow
from app.db.types import DecimalString, UTCDateTime
from app.models.base import Base, TimestampMixin, UserOwnedMixin


class LLMProviderRow(TimestampMixin, UserOwnedMixin, Base):
    __tablename__ = "llm_providers"

    name: Mapped[str] = mapped_column(String(64))
    protocol: Mapped[str] = mapped_column(String(32))          # openai_compatible / anthropic / gemini
    base_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    api_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    default_model: Mapped[str] = mapped_column(String(255))
    timeout_s: Mapped[int] = mapped_column(Integer, default=600)
    price_in_per_m: Mapped[Decimal] = mapped_column(DecimalString(), default=Decimal("0"))
    price_out_per_m: Mapped[Decimal] = mapped_column(DecimalString(), default=Decimal("0"))
    supports_json_mode: Mapped[bool] = mapped_column(Boolean, default=True)
    knowledge_cutoff: Mapped[date | None] = mapped_column(Date, nullable=True)


class PromptTemplateRow(Base):
    """prompt 模板：只增不改，修改即新版本。"""

    __tablename__ = "prompt_templates"
    __table_args__ = (UniqueConstraint("name", "version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(Text, default="")
    system_tpl: Mapped[str] = mapped_column(Text)
    user_tpl: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class AgentRow(TimestampMixin, UserOwnedMixin, Base):
    """选手：AI（kind=llm）、基线（kind=baseline）或量化策略（kind=quant，跟随策略工坊的冠军版本）。"""

    __tablename__ = "agents"

    name: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16))               # llm / baseline / quant
    provider_id: Mapped[int | None] = mapped_column(ForeignKey("llm_providers.id"), nullable=True)
    model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    params: Mapped[dict] = mapped_column(JSON, default=dict)   # temperature / max_tokens / extra_body ...
    prompt_template_id: Mapped[int | None] = mapped_column(ForeignKey("prompt_templates.id"), nullable=True)
    persona: Mapped[str] = mapped_column(Text, default="")     # 策略人格 / 附加系统提示
    memory_rounds: Mapped[int] = mapped_column(Integer, default=5)
    baseline_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    description: Mapped[str] = mapped_column(Text, default="")
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    # 自我进化：定期复盘并改写自己的策略笔记（PRD 4.4.4）
    reflect_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    reflect_every: Mapped[int] = mapped_column(Integer, default=5, server_default="5")   # 每 N 个交易日复盘一次
    initial_strategy: Mapped[str] = mapped_column(Text, default="", server_default="")  # 第 1 版策略笔记（可空）
    quant_strategy_id: Mapped[int | None] = mapped_column(Integer, nullable=True)   # kind=quant 时跟随的策略
    quant_portfolio_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # kind=quant 时跟随的组合


class RuleSetRow(TimestampMixin, UserOwnedMixin, Base):
    __tablename__ = "rule_sets"

    name: Mapped[str] = mapped_column(String(64), unique=True)
    title: Mapped[str] = mapped_column(String(128))
    market: Mapped[str] = mapped_column(String(4))
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    param_sources: Mapped[dict] = mapped_column(JSON, default=dict)   # 参数 → 来源层级（system / global / ruleset）
    description: Mapped[str] = mapped_column(Text, default="")
    is_preset: Mapped[bool] = mapped_column(Boolean, default=False)


class TrackRow(TimestampMixin, UserOwnedMixin, Base):
    __tablename__ = "tracks"

    name: Mapped[str] = mapped_column(String(128))
    market: Mapped[str] = mapped_column(String(4))
    currency: Mapped[str] = mapped_column(String(8))
    universe_name: Mapped[str] = mapped_column(String(64))
    symbols: Mapped[list] = mapped_column(JSON, default=list)          # 启动时冻结的股票池
    benchmark_symbol: Mapped[str] = mapped_column(String(20))
    rule_set_id: Mapped[int] = mapped_column(ForeignKey("rule_sets.id"))
    rule_params: Mapped[dict] = mapped_column(JSON, default=dict)      # 启动时冻结的规则参数
    initial_cash: Mapped[Decimal] = mapped_column(DecimalString())
    frequency: Mapped[str] = mapped_column(String(16), default="daily")       # daily / intraday
    interval_min: Mapped[int] = mapped_column(Integer, default=15, server_default="15")   # 分钟级决策间隔
    event_move_pct: Mapped[Decimal | None] = mapped_column(DecimalString(), nullable=True)  # 异动触发阈值，如 0.03
    event_max_per_day: Mapped[int] = mapped_column(Integer, default=5, server_default="5")
    intraday_day: Mapped[date | None] = mapped_column(Date, nullable=True)    # 分钟级：已完成开盘前处理的交易日
    output_mode: Mapped[str] = mapped_column(String(16), default="targets")   # targets / orders
    # snapshot：行情等数据全部放进 prompt；tools：AI 通过工具按需查询（PRD 3.2）
    decision_mode: Mapped[str] = mapped_column(String(16), default="snapshot", server_default="snapshot")
    max_tool_calls: Mapped[int] = mapped_column(Integer, default=10, server_default="10")
    # 快照模式附带的数据：{"news": 是否附带新闻（仅实时）, "fundamentals": 是否附带财报指标}
    data_options: Mapped[dict] = mapped_column(JSON, default=dict)
    # 历史回放的 prompt 隐去证券代码、名称与日期（PRD 3.5）
    anonymize: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    decision_timeout_s: Mapped[int] = mapped_column(Integer, default=900)
    status: Mapped[str] = mapped_column(String(16), default="draft")  # draft / running / paused / finished
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)  # 首轮决策所用数据日期
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    last_settled_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # live：实时模拟；replay：历史回放（从过去的日期逐日快进到 replay_until，之后转为实时继续运行）
    mode: Mapped[str] = mapped_column(String(16), default="live", server_default="live")
    replay_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    description: Mapped[str] = mapped_column(Text, default="")


class AccountRow(TimestampMixin, Base):
    __tablename__ = "accounts"
    __table_args__ = (UniqueConstraint("track_id", "agent_id"),)

    track_id: Mapped[int] = mapped_column(ForeignKey("tracks.id"), index=True)
    agent_id: Mapped[int] = mapped_column(ForeignKey("agents.id"), index=True)
    cash: Mapped[Decimal] = mapped_column(DecimalString())
    status: Mapped[str] = mapped_column(String(16), default="active")
    margin_call_since: Mapped[date | None] = mapped_column(Date, nullable=True)   # 追保通知日期（恢复后清空）
    # 账户级覆盖（进化实验用）：为空时沿用选手设置
    strategy_seed: Mapped[str | None] = mapped_column(Text, nullable=True)        # 初始策略笔记
    reflect_override: Mapped[bool | None] = mapped_column(Boolean, nullable=True)  # 是否定期复盘
    reflect_every_override: Mapped[int | None] = mapped_column(Integer, nullable=True)


class PositionRow(Base):
    __tablename__ = "positions"
    __table_args__ = (UniqueConstraint("account_id", "symbol"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    symbol: Mapped[str] = mapped_column(String(20))
    qty: Mapped[int] = mapped_column(Integer)
    avg_cost: Mapped[Decimal] = mapped_column(DecimalString())
    last_buy_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    last_buy_qty: Mapped[int] = mapped_column(Integer, default=0)
    opened_date: Mapped[date | None] = mapped_column(Date, nullable=True)   # 本轮建仓日（红利税按持有期）


class RoundRow(Base):
    """决策轮次：赛道在某个数据日期统一拉取快照、请求所有选手决策。"""

    __tablename__ = "rounds"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    track_id: Mapped[int] = mapped_column(ForeignKey("tracks.id"), index=True)
    data_date: Mapped[date] = mapped_column(Date, index=True)       # 快照数据日期（T 日收盘）
    fill_date: Mapped[date | None] = mapped_column(Date, nullable=True)   # 订单撮合日期（T+1）
    trigger: Mapped[str] = mapped_column(String(16))                # schedule / manual / replay / interval / event
    snapshot_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)   # 分钟级：快照时刻
    status: Mapped[str] = mapped_column(String(16))                 # running / done / failed / missed
    snapshot_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    market_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class DecisionRow(Base):
    __tablename__ = "decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    round_id: Mapped[int] = mapped_column(ForeignKey("rounds.id"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    status: Mapped[str] = mapped_column(String(16))     # ok / invalid / timeout / error / baseline
    model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    template_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    template_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prompt_system: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_user: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)   # 模型返回的推理过程（如有）
    parsed: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    analysis: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[list] = mapped_column(JSON, default=list)              # 解析 / 转换过程中的提示
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[Decimal] = mapped_column(DecimalString(), default=Decimal("0"))
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    strategy_version: Mapped[int | None] = mapped_column(Integer, nullable=True)   # 决策时使用的策略笔记版本
    tool_trace: Mapped[list | None] = mapped_column(JSON, nullable=True)            # 工具模式：每次工具调用的记录
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class StrategyVersionRow(Base):
    """账户的策略笔记：只增不改，每次修改（复盘 / 手动编辑 / 回滚）都是新版本，最新版本即当前生效版本。"""

    __tablename__ = "strategy_versions"
    __table_args__ = (UniqueConstraint("account_id", "version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(16))          # initial / reflection / manual / rollback
    changes: Mapped[str] = mapped_column(Text, default="")   # 本次修改说明
    data_date: Mapped[date | None] = mapped_column(Date, nullable=True)   # 生成时的数据日期（之后的决策开始使用）
    reflection_id: Mapped[int | None] = mapped_column(ForeignKey("reflections.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class ReflectionRow(Base):
    """一次复盘：把一段时间的交易结果交给 AI 总结，并由 AI 改写策略笔记。"""

    __tablename__ = "reflections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    data_date: Mapped[date] = mapped_column(Date)                 # 复盘时点（只使用截至该日收盘的数据）
    period_start: Mapped[date | None] = mapped_column(Date, nullable=True)   # 复盘区间起点（不含）
    trigger: Mapped[str] = mapped_column(String(16))              # schedule / manual
    status: Mapped[str] = mapped_column(String(16))               # ok / invalid / timeout / error
    model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    template_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    base_version: Mapped[int | None] = mapped_column(Integer, nullable=True)    # 复盘前的策略版本
    new_version: Mapped[int | None] = mapped_column(Integer, nullable=True)     # 生成的新版本（未修改为空）
    stats: Mapped[dict] = mapped_column(JSON, default=dict)       # 区间收益、基准、排名等，供界面展示
    review: Mapped[str | None] = mapped_column(Text, nullable=True)
    changes: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_system: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_user: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[Decimal] = mapped_column(DecimalString(), default=Decimal("0"))
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class OrderRow(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    decision_id: Mapped[int | None] = mapped_column(ForeignKey("decisions.id"), nullable=True, index=True)
    symbol: Mapped[str] = mapped_column(String(20))
    side: Mapped[str] = mapped_column(String(8))
    order_type: Mapped[str] = mapped_column(String(16), default="market")
    qty: Mapped[int] = mapped_column(Integer)
    filled_qty: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20))     # pending / filled / partially_filled / rejected / expired
    reason: Mapped[str] = mapped_column(Text, default="")        # 下单理由（AI 给出）
    note: Mapped[str] = mapped_column(Text, default="")          # 拒绝 / 失效原因
    placed_date: Mapped[date] = mapped_column(Date)              # 决策数据日期
    target_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)   # 最早撮合日期
    forced: Mapped[bool] = mapped_column(Boolean, default=False)
    limit_price: Mapped[Decimal | None] = mapped_column(DecimalString(), nullable=True)
    stop_price: Mapped[Decimal | None] = mapped_column(DecimalString(), nullable=True)   # 止损 / 止盈触发价
    tif: Mapped[str] = mapped_column(String(8), default="day", server_default="day")     # day / gtc
    expires_on: Mapped[date | None] = mapped_column(Date, nullable=True)                 # GTC 最晚有效日期
    oco_group: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    attach_stop: Mapped[Decimal | None] = mapped_column(DecimalString(), nullable=True)
    attach_take_profit: Mapped[Decimal | None] = mapped_column(DecimalString(), nullable=True)
    active_from: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)  # 分钟级：最早撮合时刻
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class FillRow(Base):
    __tablename__ = "fills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    symbol: Mapped[str] = mapped_column(String(20))
    side: Mapped[str] = mapped_column(String(8))
    qty: Mapped[int] = mapped_column(Integer)
    price: Mapped[Decimal] = mapped_column(DecimalString())
    amount: Mapped[Decimal] = mapped_column(DecimalString())
    fees: Mapped[dict] = mapped_column(JSON, default=dict)
    total_fee: Mapped[Decimal] = mapped_column(DecimalString())
    realized_pnl: Mapped[Decimal | None] = mapped_column(DecimalString(), nullable=True)
    price_source: Mapped[str] = mapped_column(String(64), default="")


class CashLedgerRow(Base):
    __tablename__ = "cash_ledger"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    trade_date: Mapped[date] = mapped_column(Date)
    type: Mapped[str] = mapped_column(String(16))
    amount: Mapped[Decimal] = mapped_column(DecimalString())
    balance_after: Mapped[Decimal] = mapped_column(DecimalString())
    symbol: Mapped[str | None] = mapped_column(String(20), nullable=True)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"), nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class NavSnapshotRow(Base):
    __tablename__ = "nav_snapshots"
    __table_args__ = (UniqueConstraint("account_id", "date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    date: Mapped[date] = mapped_column(Date)
    nav: Mapped[Decimal] = mapped_column(DecimalString())
    cash: Mapped[Decimal] = mapped_column(DecimalString())
    market_value: Mapped[Decimal] = mapped_column(DecimalString())
    benchmark_close: Mapped[Decimal | None] = mapped_column(DecimalString(), nullable=True)
    positions_count: Mapped[int] = mapped_column(Integer, default=0)
    long_value: Mapped[Decimal | None] = mapped_column(DecimalString(), nullable=True)    # 多头市值
    short_value: Mapped[Decimal | None] = mapped_column(DecimalString(), nullable=True)   # 空头市值（正数）


class EventRow(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    track_id: Mapped[int | None] = mapped_column(ForeignKey("tracks.id"), nullable=True, index=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"), nullable=True, index=True)
    trade_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    type: Mapped[str] = mapped_column(String(32))
    level: Mapped[str] = mapped_column(String(8), default="info")   # info / warning / error
    message: Mapped[str] = mapped_column(Text)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class ConsistencyCheckRow(Base):
    """推理-行为一致性评估（PRD 3.6）：评审模型判断一次决策的分析结论与实际订单是否一致。"""

    __tablename__ = "consistency_checks"
    __table_args__ = (UniqueConstraint("decision_id", "judge_model"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    decision_id: Mapped[int] = mapped_column(ForeignKey("decisions.id"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    judge_provider_id: Mapped[int | None] = mapped_column(ForeignKey("llm_providers.id"), nullable=True)
    judge_model: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16))           # ok / invalid / timeout / error
    verdict: Mapped[str | None] = mapped_column(String(16), nullable=True)   # consistent / partial / inconsistent
    score: Mapped[float | None] = mapped_column(Float, nullable=True)        # 0～1
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[Decimal] = mapped_column(DecimalString(), default=Decimal("0"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class ExperimentRow(TimestampMixin, UserOwnedMixin, Base):
    """离线进化实验（PRD 3.7 第 3 层）：在训练区间反复「回放 → 复盘 → 改策略」，每一代的策略在之后的验证区间检验。"""

    __tablename__ = "experiments"

    name: Mapped[str] = mapped_column(String(128))
    agent_id: Mapped[int] = mapped_column(ForeignKey("agents.id"))
    universe_name: Mapped[str] = mapped_column(String(64))
    rule_set_id: Mapped[int] = mapped_column(ForeignKey("rule_sets.id"))
    initial_cash: Mapped[Decimal] = mapped_column(DecimalString())
    output_mode: Mapped[str] = mapped_column(String(16), default="targets")
    train_start: Mapped[date] = mapped_column(Date)
    train_end: Mapped[date] = mapped_column(Date)
    valid_start: Mapped[date] = mapped_column(Date)
    valid_end: Mapped[date] = mapped_column(Date)
    generations: Mapped[int] = mapped_column(Integer, default=3)
    reflect_every: Mapped[int] = mapped_column(Integer, default=5)
    anonymize: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default="pending")   # pending / running / done / failed / cancelled
    progress: Mapped[str] = mapped_column(Text, default="")
    results: Mapped[list] = mapped_column(JSON, default=list)            # 每一代的训练 / 验证结果
    description: Mapped[str] = mapped_column(Text, default="")
