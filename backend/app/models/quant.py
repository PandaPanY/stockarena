"""策略工坊（PRD 3.8）：规则化策略、版本与 AI 研究记录。"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import JSON, Boolean, Date, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.types import UTCDateTime
from app.models.base import Base, TimestampMixin, UserOwnedMixin


class QuantStrategyRow(TimestampMixin, UserOwnedMixin, Base):
    """一条按风格持续进化的策略线；champion_id 指向当前冠军版本。"""

    __tablename__ = "quant_strategies"

    name: Mapped[str] = mapped_column(String(64))
    style: Mapped[str] = mapped_column(String(32), default="custom")      # 风格库 key（config/personas.yaml）或 custom
    description: Mapped[str] = mapped_column(Text, default="")            # 风格说明，研究时提供给 AI
    market: Mapped[str] = mapped_column(String(4), default="CN")
    universe_name: Mapped[str] = mapped_column(String(64))
    benchmark: Mapped[str] = mapped_column(String(20))
    # 研究员模型：沿用模型供应商配置
    provider_id: Mapped[int | None] = mapped_column(ForeignKey("llm_providers.id"), nullable=True)
    model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    llm_params: Mapped[dict] = mapped_column(JSON, default=dict)
    # 检验区间、回撤上限、升级门槛等（见 app/quant/research.py ResearchSettings）
    settings: Mapped[dict] = mapped_column(JSON, default=dict)
    champion_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    auto_optimize: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default="active")     # active / archived
    # 实时跟踪：策略作为量化选手参加的账户
    follow_account_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 长期研究笔记：AI 每轮总结的经验（哪些方向有效 / 无效），跨多次优化保留，用户可编辑
    research_notes: Mapped[list] = mapped_column(JSON, default=list)
    # 「可实盘」检查的最近结果（app/quant/readiness.py）
    readiness: Mapped[dict] = mapped_column(JSON, default=dict)
    # 另存来源（app/quant/service.py fork_strategy）：来源策略与版本、继承的检验次数；空表示不是另存的
    origin: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 公共策略：所有用户都能查看和另存，只有所有者（管理员）能修改、优化、跟踪。管理员创建的风格策略库默认公共
    is_public: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")


class QuantVersionRow(TimestampMixin, Base):
    """回测过的一个策略规格。status：champion / candidate / rejected / retired / invalid。"""

    __tablename__ = "quant_versions"

    strategy_id: Mapped[int] = mapped_column(ForeignKey("quant_strategies.id"), index=True)
    number: Mapped[int] = mapped_column(Integer)                          # 策略内序号，从 1 开始
    parent_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="ai")         # initial / ai / manual
    title: Mapped[str] = mapped_column(String(128), default="")
    rationale: Mapped[str] = mapped_column(Text, default="")              # AI 的改动理由
    spec: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="candidate")
    decision: Mapped[str] = mapped_column(Text, default="")               # 升级 / 淘汰的原因
    train: Mapped[dict] = mapped_column(JSON, default=dict)               # 各区间指标与分年收益
    valid: Mapped[dict] = mapped_column(JSON, default=dict)
    holdout: Mapped[dict] = mapped_column(JSON, default=dict)
    dev: Mapped[dict] = mapped_column(JSON, default=dict)                 # 开发期（训练 + 验证）报告，含逐年与月度收益
    robustness: Mapped[dict] = mapped_column(JSON, default=dict)
    curves: Mapped[dict] = mapped_column(JSON, default=dict)              # 降采样的净值与基准曲线
    score: Mapped[float | None] = mapped_column(Float, nullable=True)     # 验证区间夏普
    promoted_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class ResearchRunRow(TimestampMixin, Base):
    """一次 AI 研究（优化）运行。"""

    __tablename__ = "quant_research_runs"

    strategy_id: Mapped[int] = mapped_column(ForeignKey("quant_strategies.id"), index=True)
    trigger: Mapped[str] = mapped_column(String(16), default="manual")    # manual / schedule
    status: Mapped[str] = mapped_column(String(16), default="pending")    # pending/running/done/failed/cancelled
    rounds: Mapped[int] = mapped_column(Integer, default=5)
    rounds_done: Mapped[int] = mapped_column(Integer, default=0)
    promoted: Mapped[int] = mapped_column(Integer, default=0)             # 本次升级次数
    progress: Mapped[str] = mapped_column(Text, default="")
    log: Mapped[list] = mapped_column(JSON, default=list)                 # 每轮：AI 分析、候选、检验结果、token 用量
    summary: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class QuantPortfolioRow(TimestampMixin, UserOwnedMixin, Base):
    """多风格组合：若干策略的冠军版本按权重分配资金，在同一个账户中运行。"""

    __tablename__ = "quant_portfolios"

    name: Mapped[str] = mapped_column(String(64))
    members: Mapped[list] = mapped_column(JSON, default=list)            # [{"strategy_id": 1, "weight": 0.25}]
    # 组合设置：{"alloc": {method, lookback, every, vol_target, min_exposure}}（见 app/quant/allocation.py）
    settings: Mapped[dict] = mapped_column(JSON, default=dict)
    description: Mapped[str] = mapped_column(Text, default="")
    follow_account_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="active")
    readiness: Mapped[dict] = mapped_column(JSON, default=dict)


class RealAccountRow(TimestampMixin, UserOwnedMixin, Base):
    """实盘账户（PRD 3.9 G8）：用户在券商的真实账户，与某个策略或组合的实时跟踪（模拟）对照。

    只记录用户录入 / 导入的成交，不连接券商、不保存任何券商账号信息。
    """

    __tablename__ = "real_accounts"

    name: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16))                         # strategy / portfolio
    ref_id: Mapped[int] = mapped_column(Integer)                          # 策略或组合 id
    initial_cash: Mapped[float] = mapped_column(Float)
    start_date: Mapped[date] = mapped_column(Date)
    # 资金转入（正）/ 转出（负）：[{"date": "2026-10-10", "amount": 100000, "note": ""}]
    cash_flows: Mapped[list] = mapped_column(JSON, default=list)
    note: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="active")


class RealFillRow(TimestampMixin, Base):
    """实盘成交（手工录入或从券商导出的成交明细导入）。"""

    __tablename__ = "real_fills"

    real_account_id: Mapped[int] = mapped_column(ForeignKey("real_accounts.id"), index=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    symbol: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(64), default="")
    side: Mapped[str] = mapped_column(String(8))                          # buy / sell
    qty: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    fee: Mapped[float] = mapped_column(Float, default=0.0)                # 佣金 + 印花税 + 过户费等合计
    source: Mapped[str] = mapped_column(String(16), default="manual")    # manual / import
    note: Mapped[str] = mapped_column(Text, default="")
