"""规则集参数。默认值为 A股只做多赛道的示例值，均可在 config/rulesets/*.yaml 中覆盖。"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class RuleParams(BaseModel):
    model_config = {"frozen": True}

    # ---- 撮合 ----
    slippage_bps: Decimal = Field(Decimal("5"), ge=0, description="市价单滑点（基点）")
    participation_rate: Decimal = Field(Decimal("0.10"), gt=0, le=1, description="单笔成交量占 K 线成交量上限")

    # ---- 风控 ----
    max_position_pct: Decimal = Field(Decimal("0.30"), gt=0, le=1, description="单票持仓市值占净值上限")
    max_orders_per_round: int = Field(10, ge=1, description="单轮最多订单数")
    rebalance_threshold_pct: Decimal = Field(
        Decimal("0.01"), ge=0, description="目标仓位模式：调仓金额低于净值的该比例时忽略（避免频繁微调）")
    rebalance_new_free: bool = Field(
        False, description="目标仓位模式：新建仓不受调仓门槛限制（门槛只用于已有持仓的加减仓）")
    cash_buffer_pct: Decimal = Field(Decimal("0.005"), ge=0, description="目标仓位模式：预留现金比例，覆盖费用与滑点")

    # ---- 费用（A股）⚠️ 以交易所与券商最新标准为准 ----
    commission_rate: Decimal = Field(Decimal("0.00025"), ge=0, description="佣金费率（双边）")
    commission_min: Decimal = Field(Decimal("5"), ge=0, description="单笔最低佣金")
    stamp_duty_rate: Decimal = Field(Decimal("0.0005"), ge=0, description="印花税（股票卖出）")
    transfer_fee_rate: Decimal = Field(Decimal("0.00001"), ge=0, description="过户费（股票双边）")
    dividend_tax_rate: Decimal = Field(Decimal("0"), ge=0, le=1, description="红利税（简化为统一税率，默认不扣）")
    cn_dividend_tax_by_holding: bool = Field(
        False, description="A股股票红利税按持有期差别化计算（≤1 个月 20%、1 个月～1 年 10%、>1 年免征；"
                           "ETF 等基金分红免征）；开启时代替 dividend_tax_rate")
    platform_fee: Decimal = Field(Decimal("0"), ge=0, description="每笔固定平台费（部分港美股券商收取）")

    # ---- 费用（港股）⚠️ 以港交所、证监会、会财局最新收费表为准 ----
    hk_stamp_duty_rate: Decimal = Field(Decimal("0.001"), ge=0,
                                        description="港股印花税（买卖双方，向上取整到元；ETF 等豁免）")
    hk_trading_levy_rate: Decimal = Field(Decimal("0.000027"), ge=0, description="证监会交易征费")
    hk_afrc_levy_rate: Decimal = Field(Decimal("0.0000015"), ge=0, description="会财局交易征费")
    hk_trading_fee_rate: Decimal = Field(Decimal("0.0000565"), ge=0, description="港交所交易费")
    hk_settlement_fee_rate: Decimal = Field(Decimal("0.00002"), ge=0, description="中央结算费")
    hk_settlement_fee_min: Decimal = Field(Decimal("2"), ge=0, description="中央结算费最低（港元）")
    hk_settlement_fee_max: Decimal = Field(Decimal("100"), ge=0, description="中央结算费最高（港元）")

    # ---- 费用（美股）⚠️ SEC 费率每个财年调整 ----
    us_sec_fee_rate: Decimal = Field(Decimal("0.0000278"), ge=0, description="SEC 费（卖出，按成交额）")
    us_taf_per_share: Decimal = Field(Decimal("0.000166"), ge=0, description="FINRA 交易活动费（卖出，每股）")
    us_taf_max: Decimal = Field(Decimal("8.30"), ge=0, description="FINRA 交易活动费单笔上限（美元）")

    # ---- 做空 / 杠杆（PRD 4.6.4、4.6.5）⚠️ 保证金比例与费率为示例值 ----
    allow_short: bool = Field(False, description="允许融券卖空")
    allow_margin: bool = Field(False, description="允许融资买入（现金可以为负）")
    max_leverage: Decimal = Field(Decimal("1"), ge=1, le=10,
                                  description="总敞口（多头市值 + 空头市值）/ 净值上限；开启做空或融资时默认 2")
    allow_leveraged_etf: bool = Field(False, description="允许交易杠杆 / 反向 ETF")
    shortable_mode: Literal["ideal"] = Field("ideal", description="可做空标的：ideal 为池内股票与 ETF 均可做空")
    margin_check_method: Literal["equity_ratio", "collateral_ratio"] = Field(
        "equity_ratio",
        description="保证金比率：equity_ratio 净值 / 总敞口（港美股）；"
                    "collateral_ratio 总资产 / 总负债（A股维持担保比例）")
    initial_margin: Decimal = Field(Decimal("0.5"), gt=0, le=1, description="初始保证金率（杠杆上限不超过其倒数）")
    maintenance_margin: Decimal = Field(Decimal("0.3"), gt=0, description="维持线：低于该比率发出追保通知")
    liquidation_margin: Decimal = Field(Decimal("0.25"), gt=0, description="强平线：低于该比率立即强制平仓")
    liquidation_buffer: Decimal = Field(Decimal("0.05"), ge=0, description="强平后比率恢复到维持线之上的幅度")
    margin_interest_rate: Decimal = Field(Decimal("0.06"), ge=0, description="融资年利率")
    borrow_fee_rate: Decimal = Field(Decimal("0.08"), ge=0, description="融券年费率")
    day_count: int = Field(365, gt=0, description="计息天数基准")
    cn_short_t1: bool = Field(True, description="A股：当日融券卖出的证券当日不得买券还券")

    @model_validator(mode="before")
    @classmethod
    def _leverage_default(cls, data):
        # 开启做空或融资但未指定杠杆上限时，默认 2 倍
        leveraged = isinstance(data, dict) and (data.get("allow_short") or data.get("allow_margin"))
        if leveraged and data.get("max_leverage") is None:
            data = {**data, "max_leverage": Decimal("2")}
        return data

    @model_validator(mode="after")
    def _check_margin_lines(self):
        if self.liquidation_margin >= self.maintenance_margin:
            raise ValueError("强平线必须低于维持线")
        return self

    @property
    def leveraged(self) -> bool:
        return self.allow_short or self.allow_margin

    @property
    def leverage_cap(self) -> Decimal:
        """实际生效的杠杆上限：max_leverage 与 1 / 初始保证金率中较小者；只做多现金账户为 1。"""
        if not self.leveraged:
            return Decimal("1")
        return min(self.max_leverage, 1 / self.initial_margin)
