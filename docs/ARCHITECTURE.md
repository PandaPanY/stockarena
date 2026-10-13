# StockArena 架构文档

| 项目 | 内容 |
|---|---|
| 对应版本 | v1.9 策略工坊（2026-10-05） |
| 读者 | 开发者、需要了解系统如何运作的使用者 |
| 相关文档 | 需求与决策：[PRD.md](PRD.md)；功能说明：[FEATURES.md](FEATURES.md) |

> 本文描述**当前实际实现**。PRD 第 4 章是项目初期的设计稿，与实现不一致之处以本文为准。

---

## 1. 系统定位

StockArena 是本地运行的单用户 Web 应用，分两大模块：

| 模块 | 定位 | 核心对象 |
|---|---|---|
| **策略工坊**（主线） | AI 作为策略研究员，在真实历史行情上反复回测、诊断、修改规则化策略；按风格维护策略库供用户挑选；选中的策略每日出信号并在实时模拟账户中跟踪 | 策略、版本、研究轮 |
| **AI 对比**（附带） | 多个 AI 与基线选手在同一「赛道」用虚拟本金模拟交易，比较交易能力 | 赛道、选手、账户、决策 |

两个模块共用行情数据、交易规则与交易引擎。策略工坊的「实时跟踪」就是一条特殊的赛道，量化策略以「量化选手」身份参赛。

最终目标（PRD 3.9）：经过多轮优化的策略，能在真实交易中使用并达到模拟中的效果。因此架构上强调：**无前视、规则真实、样本外检验、模拟与实时执行可对照**。

---

## 2. 总体架构

```
┌────────────────────────── 前端 Vue 3 + TypeScript + Vite（端口 15173）──────────────────────────┐
│ 策略库 │ 策略详情（冠军报告 / 交易明细 / 信号与跟踪 / 版本 / 研究记录）│ 每日信号                       │
│ AI 对比：总览 · 赛道 · 账户 · 决策回放 · 对比分析 · 进化实验 │ 配置中心 │ 系统状态                    │
│ Element Plus · ECharts（vue-echarts）· axios（经 Vite 代理访问后端）                              │
└───────────────────────────────────────────┬──────────────────────────────────────────────────────┘
                                   REST JSON │（只监听 127.0.0.1:18080）
┌───────────────────────────────────────────▼──────────────────────────────────────────────────────┐
│ FastAPI 单进程（app/main.py）  启动：迁移数据库 → 初始化种子数据 → 注册调度任务 → 恢复中断任务       │
│                                                                                                    │
│  api/ 路由 ──┬─► quant/ 策略工坊 ──────────────┐        ┌─► agent/ prompt 与解析 ─► llm/ 适配层 ─► 模型 │
│              │   panel 行情矩阵 · factors 因子  │        │                                            │
│              │   spec 规格 · backtest 快速回测   │        │                                            │
│              │   evaluate 检验 · tune 调参       │        │                                            │
│              │   research AI 研究 · explain 解释 │────────┘（研究员调用同一套 LLM 适配层）           │
│              │   details 交易明细 · live 量化选手│                                                   │
│              │                                  ▼                                                   │
│              └─► arena/ 赛道编排（日终流程、盘中引擎、复盘、实验、导出）──► trading/ 交易引擎（纯逻辑）│
│                        ▲                                                                           │
│  scheduler/ APScheduler（收盘后日终流程、每晚策略优化）                                              │
│                        │                                                                           │
│  market/ 行情服务：数据源适配 + 本地缓存 + 交易日历 + 股票池 + 财报 / 新闻                          │
│  analytics/ 指标（策略回测与赛道账户共用同一套）                                                    │
└───────────────────────┬────────────────────────────────────────────────────────────────────────────┘
                        ▼
   SQLite（data/stockarena.db，WAL 模式；ORM 兼容 PostgreSQL）
   数据源：新浪 / 腾讯（AKShare）A股、港股、美股日线；同花顺 / 东方财富财报；东方财富新闻；港交所证券名单
```

---

## 3. 技术栈

| 层 | 选型 |
|---|---|
| 后端 | Python 3.13、FastAPI、SQLAlchemy 2 + Alembic（SQLite 批量迁移模式）、Pydantic v2、APScheduler、pandas 3 + numpy、AKShare、uv 管理依赖、ruff（行宽 120）、pytest + pytest-asyncio |
| 前端 | Vue 3、TypeScript、Vite、Element Plus、ECharts（vue-echarts）、Pinia、vue-tsc 类型检查、npm |
| 模型接入 | OpenAI 兼容协议（含本地 llama.cpp / Ollama / LM Studio）、Anthropic、Gemini；支持函数调用 |
| 存储 | SQLite（WAL，busy_timeout 30 秒）；金额以字符串存储的 Decimal；时间统一存 UTC |

---

## 4. 目录结构

```
StockTrading/
├─ backend/
│  ├─ app/
│  │  ├─ main.py            FastAPI 入口与启动流程
│  │  ├─ api/               HTTP 路由：quant（策略工坊）、tracks / accounts / experiments（AI 对比）、
│  │  │                     providers / agents / meta（配置）、system（状态）
│  │  ├─ core/              配置、日志、加密（Fernet）、Decimal 与时区工具、网络直连设置
│  │  ├─ db/                引擎与会话、自定义列类型（DecimalString、UTCDateTime）、迁移
│  │  ├─ models/            ORM：market（行情）、arena（赛道）、quant（策略工坊）、user
│  │  ├─ market/            行情服务、数据源（providers/）、交易日历、股票池、财报、新闻、实时行情、缓存修复
│  │  ├─ trading/           交易引擎（纯逻辑）：规则、费用、撮合、风控、目标仓位、账户、保证金
│  │  ├─ quant/             策略工坊（第 5 节）
│  │  ├─ arena/             赛道编排：日终流程、盘中引擎、生命周期、复盘、实验、一致性评估、导出
│  │  ├─ agent/             AI 选手：快照上下文、prompt 渲染、输出解析、工具调用循环、复盘解析
│  │  ├─ llm/               模型适配层（三种协议、工具调用、费用计算）
│  │  ├─ analytics/         指标计算
│  │  ├─ baselines/         基线选手（买入持有、等权、随机）
│  │  └─ scheduler/         调度任务注册
│  ├─ alembic/versions/     数据库迁移
│  └─ tests/                pytest（market / trading / arena / quant）
├─ frontend/src/
│  ├─ views/                LabView（策略库）、StrategyDetailView、SignalsView、Dashboard / Tracks / ...（AI 对比）
│  ├─ components/lab/       LabCurve、VersionReport、TradeDetails、TrackingPanel
│  ├─ api/                  quant.ts（策略工坊）、arena.ts、system.ts
│  └─ router/               路由与侧边栏菜单
├─ config/                  规则集、股票池、prompt 模板、复盘模板、风格库、strategies.yaml、research/
├─ data/                    运行时数据（数据库、密钥、缓存、日志），不进版本库
└─ docs/                    PRD、本文、功能说明
```

---

## 5. 策略工坊（app/quant）

### 5.1 模块职责

| 文件 | 职责 |
|---|---|
| `data.py` | 数据准备：把股票池日线回填到训练开始前约 1.5 年（因子预热期），刷新股票财报；行情矩阵缓存（最多 3 个） |
| `panel.py` | 行情矩阵：一次性把「日期 × 标的」的开高低收、成交额、复权因子、停牌标记读入 numpy；财报按最早可用日期向前填充（每股收益换算为滚动 12 个月，估算总股本 = 归母净利润 / 每股收益，业绩加速度）；滚动 12 个月股息率（按除息日）；申万一级行业（按变更日期生效）；ETF 份额折算兜底修正 |
| `factors.py` | 因子库（29 个）：价格类（动量、反转、波动、特质波动、偏度、非流动性、均线、RSI、回撤、贝塔……）、财报类（E/P、B/P、ROE、增速、净利率、业绩加速度、市值、换手……）与股息率；每个因子第 t 行只用 t 日及以前的数据；结果缓存在行情矩阵上 |
| `spec.py` | 策略规格（Pydantic）：选股范围、打分因子、过滤、选股数量、权重、调仓、择时、风控；校验失败返回中文说明；`describe()` 翻译成中文规则 |
| `signal.py` | 由规格计算某日收盘后的目标权重：范围与过滤（含行业）→ 各因子百分位加权打分（可行业内排名）→ 选前 N（含持仓缓冲、同行业上限）→ 权重 → 择时缩放与防守资产 |
| `backtest.py` | 快速日频回测（A股规则、冲击成本）。「收盘后目标」可插拔：`SingleTargeter`（单策略）、`PortfolioTargeter`（组合在单账户中运行，与实时组合选手同一规则）。输出净值、成交（含原因）、每笔完整持有、成交统计、可选的调仓解释 |
| `explain.py` | 选股解释：入选标的的排名与因子百分位、卖出原因、择时状态（只用于展示，不影响回测） |
| `evaluate.py` | 分区间报告（训练 / 验证 / 开发期 / 留出，开发期含月度收益）、分年统计、回撤区间诊断、稳健性检验、月度配对检验、按尝试次数折算的夏普（DSR）、CSCV 回测过拟合概率（PBO）、升级门槛 |
| `factor_research.py` | 因子成绩单：全股票池横截面 IC、ICIR、t 值、逐年 IC、五分组收益（给 AI 与「因子研究」页） |
| `tune.py` | 程序调参：只用训练区间做坐标搜索，窗口取常用值，有回测次数上限（实测易过拟合，默认关闭） |
| `portfolio.py` | 多风格组合：单账户组合回测、成员表现与相关性、组合信号 |
| `capacity.py` | 资金容量：20 万～10 亿多档资金回测，最低资金与可承载资金 |
| `reconcile.py` | 回测—跟踪对账（交易引擎记录 vs 快速回测）与跟踪期健康度 |
| `readiness.py` | 「可实盘」检查：开发期 / 留出期 / 逐年 / 实时跟踪 / 资金容量五项条件，结果保存在策略与组合上 |
| `real.py` | 实盘对照：券商成交明细解析（编码与列名识别）、实盘净值重建、实盘 vs 模拟报告（收益差、执行价差、漏做 / 多做、持仓差） |
| `service.py` | 策略设置（`LabSettings`）、版本评估与写入、升级、初始策略库、重新回测 |
| `research.py` | AI 研究循环：组装研究员提示 → 调用模型 → 解析与修复 JSON → 调参 → 检验 → 升级 |
| `details.py` | 版本交易明细：按区间汇总调仓记录、持仓收益、标的汇总、成交流水（按需回测并缓存） |
| `live.py` | 量化选手与组合选手：在赛道中按冠军版本决策（调仓节奏、止损、选股理由）；创建实时跟踪赛道；最新信号 |
| `tasks.py` | 后台任务：数据准备、创建初始策略库、研究、每晚自动优化、启动时恢复中断任务 |

### 5.2 回测时序与规则

```
第 t 日：
 1. 复权因子变化（分红送转、ETF 折算）→ 按比例调整持股数
 2. 执行 t-1 日收盘产生的信号：先卖后买，按开盘价 ± 滑点（默认 5 个基点）
    · 停牌不成交；开盘即涨停不能买、即跌停不能卖；单笔不超过当日成交额的 10%；100 股整手
    · 费用：佣金万 2.5（最低 5 元）、股票过户费、股票卖出印花税（2023-08-28 前 0.1%，之后 0.05%）
    · 调仓门槛：新建仓与清仓不受限制；已有持仓的加减仓，目标金额与当前市值之差超过「最小交易比例」才交易
      （单策略 = 规格的 min_trade_pct；组合 = max(成员比例 × 成员权重, 0.5%)）
 3. 收盘估值（停牌按最后价格）
 4. 收盘后：检查止损 / 移动止损 → 是调仓日则计算新目标权重（止损的标的到下次调仓前不买回）
```

- 不使用 t 日之后的任何数据：因子按窗口滚动计算，财报按法定披露截止日（或公告日次日）才可用。
- 冲击成本（平方根模型）：额外价格偏离 = 系数 1.0 × 前一日 20 日波动率 × √(成交额 / 当日成交额)；100 万资金时约 1～2 个基点，资金大时显著。
- 回测用 float 计算以求速度（8 年、310 个标的约 0.2～0.4 秒）；实时跟踪由交易引擎用 Decimal 精确记账，两者用对账检验一致性（5.7）。

### 5.3 检验区间与升级门槛（v1.6）

| 区间 | 默认 | 用途 |
|---|---|---|
| 开发期 | 2018～2024（训练 + 验证） | AI 研究与诊断；升级检验在这一段上逐年进行 |
| 留出 | 2025～最新 | 从不参与任何决策，只展示 |

v1.5 的「单一验证区间 + 夏普高 0.1」在实测中几乎没有预测力（训练—验证秩相关 0.05，同一策略内验证—留出相关多为 0 或负），v1.6 改为（`evaluate.check_gates`）：

1. 开发期夏普高于冠军；
2. **逐年对比**：候选月收益按两者波动之比换算到冠军的波动水平，按年汇总差值，至少一半年份跑赢；
3. **月度配对 bootstrap**：等波动月度差值的单侧 p ≤ p_max / 同轮候选数（默认 0.05 / 3，多重检验校正；v1.6 实测后由 0.2 收紧）；
4. 开发期最大回撤不超过上限（冠军本身超限时不比冠军差）、年化换手不超过上限、股票策略持仓不少于 20 只；
5. 参数 ±20% 扰动后开发期夏普中位数不低于原值 70%。

另外按策略至今的尝试次数计算**折算夏普 DSR**（Bailey & López de Prado），展示在策略卡片上，作为「冠军是否只是多次尝试中碰巧最好」的参考。

### 5.3.1 时点股票池

股票池可声明 `point_in_time: <指数代码>`。数据准备时从新浪拉取指数成分的纳入日期（`index_members` 表），行情矩阵据此生成成分掩码 `member[t, j]`：选股与因子成绩单只使用当天已是成分的股票，ETF 不受限制。⚠️ 新浪「历史成份」只更新到 2010 年，拿不到之后被剔除的股票，因此是「半时点」：只消除了「提前买入后来才纳入指数的股票」这部分偏差。实时跟踪使用 `live_universe` 指定的当前成分股票池。

### 5.4 AI 研究循环

```
start_run（手动或每晚 19:30）→ 全局锁（一次只跑一个研究，避免 CPU 与写锁争用）
每一轮：
  build_prompt：风格说明、股票池、因子库、因子成绩单（开发期）、规格字段说明（config/research/default.yaml）、
                冠军规则与开发期报告（逐年、回撤区间、盈亏最多的标的）、升级条件、
                长期研究笔记（最近 20 条）、冠军开发期交易诊断（胜率、盈亏比、卖出原因、亏损最多的持有）、最近 8 次尝试
  run_llm_json：调用研究模型（超时 15 分钟，解析失败带错误重试一次）
  parse_research：容错解析（补全错位或缺失的括号）→ 逐个校验规格 → 提取本轮经验（lessons）
  _round_evaluate（线程中执行，先计算后短事务写入）：
      去重 →（可选）程序调参 → 全部候选回测 → 开发期夏普不高于冠军的直接淘汰 → 稳健性检验
      → 升级门槛（显著性门槛按本轮候选数收紧）→ 通过者中开发期夏普最高的升级，其余写明淘汰原因
      → 本轮经验追加到长期研究笔记（保留 40 条）
  写入研究日志（分析、经验、候选、相对冠军的超额与 p 值、原因、token 与耗时）
```

### 5.5 交易明细与选股解释

`details.details(version)` 以 `explain=True` 重跑该版本（约 0.5 秒，按「版本 + 数据最新日期」缓存），按区间返回：

- **概览**：成交笔数、完整持有次数、胜率、平均盈利 / 亏损、平均持有天数、交易盈亏、费用、卖出原因归类统计；
- **调仓记录**：每次调仓日的可选标的数、择时状态、入选标的（综合排名、目标权重、每个因子的数值与百分位）、卖出标的及原因；止损事件单列；
- **持仓收益**：每次「空仓→买入→全部卖出」的买入日、卖出日、持有天数、收益率、盈亏、卖出原因；期末仍持有的按市值计；
- **标的汇总、成交流水**（每笔成交带原因）。

不变量（有测试）：所有持有记录的盈亏之和 = 期末净值 − 初始资金；带解释与不带解释的回测结果完全一致。

### 5.6 每日信号与实时跟踪

- **最新信号**（`live.signal`）：用最新收盘数据按冠军版本计算目标持仓，可按用户资金量换算整手股数，不依赖账户。
- **实时跟踪**（`live.follow`）：为策略创建一条实时赛道（量化选手 + 买入持有基准，本金 100 万）。每个交易日收盘后的日终流程中，量化选手调用 `decide_quant`：
  - 每 N 个交易日调仓一次（与回测一致）；冠军版本更新后的下一轮立即按新版本调仓；
  - 非调仓日只检查止损（成本价取持仓均价，高点从最近一次买入算起）；
  - 决策说明包含选股理由与卖出原因；目标权重交给交易引擎，按真实规则撮合、记账、处理分红送转。
- **多风格组合**（`portfolio.py`、`live.decide_portfolio`、`backtest.PortfolioTargeter`）：若干策略冠军按权重配置，在**同一个账户**中运行。
  每个成员按自己的调仓周期更新「成员目标权重」，成员规则中的止损按账户持仓成本检查；任一成员变化时账户整体调到 Σ 成员权重 × 成员目标（相当于成员间定期再平衡）。
  回测与实时跟踪使用完全相同的规则（v1.7 之前回测为成员分仓相加，对账发现累计差 7%，已统一）。
- **调仓门槛**（v1.8 前）：组合的加减仓门槛原为成员门槛按权重折算（约 0.2%），产生大量几千元的补仓单；现为 max(折算值, 0.5%)，新建仓不受门槛限制。交易引擎（`trading/targets.py`）的门槛按「目标金额 − 当前市值」判断，目标权重大于 0 但不足一手时按减仓处理（不当作清仓），与快速回测一致——回放对账曾因这两处差异出现 −3.5% 的年化差。
- 实时决策与研究共用全量行情矩阵（按日期取行），与快速回测在计算上等价，历史回放也不必每天重读数据。

### 5.7 对账、回放校验、资金容量与健康度（v1.7）

| 功能 | 做法 | 判定 |
|---|---|---|
| 回测—跟踪对账 | 取跟踪账户最后一次版本变化之后的区间，用同一版本（组合为同一组成员版本）、同样本金做快速回测，比较日收益、累计收益、成交笔数与成交价、期末持仓 | 跟踪误差 ≤ 3% 且年化差 ≤ 1%：一致；≤ 8% 且 ≤ 3%：有偏差；否则偏差较大 |
| 回放校验 | 新建回放赛道（名称以「回放校验·」开头），用交易引擎从指定日期重放到最近收盘日后结束，再对账 | 同上 |
| 资金容量 | 10 档资金回测，统计年化、成本、冲击成本、平均仓位、持仓偏离（调仓后 ½Σ\|实际权重−目标权重\|）、买不起一手次数、受成交额限制次数 | 最低资金：额外偏离（比各档最小偏离多出的部分）≤ 3 个百分点；可承载资金：年化比最低资金（≥ 100 万）时下降 ≤ 1 个百分点且额外偏离 ≤ 5 个百分点 |
| 健康度 | 跟踪期最近 n（≤ 60）日收益在回测历史同长度收益中的分位、跟踪期最大回撤与回测历史最大回撤、对账偏差 | 分位 < 5% 或回撤 > 1.2 倍：提醒；分位 < 15% 或回撤 > 0.8 倍：关注 |

---

## 6. AI 对比模块（app/arena、app/agent）

| 组件 | 说明 |
|---|---|
| 生命周期（`lifecycle.py`） | 创建赛道（实时 / 历史回放、日频 / 分钟级、快照 / 工具调用）、报名选手、启动时冻结股票池与规则、复制赛道 |
| 日终流程（`cycle.py`） | 收盘后：更新行情与除权事件 → 结算到最新交易日（撮合、分红送转、计息、保证金检查与强平）→ 记录净值 → 到期复盘 → 生成快照并请所有选手决策 → 风控与订单入库（下一交易日开盘撮合） |
| 盘中引擎（`intraday.py`） | 新浪实时行情 15 秒轮询合成分钟线，按间隔决策，下一分钟生效；只支持实时 |
| 选手 | AI（`agent/`：快照或工具调用、策略笔记、定期复盘）、基线（`baselines/`）、量化（`quant/live.py`） |
| 评估与输出 | 指标与雷达评分、对比页、推理-行为一致性评估、离线进化实验、CSV / JSON 导出、HTML 报告 |

交易引擎（`trading/`）是纯逻辑：输入「账户状态 + 订单 + 行情」，输出「新状态 + 流水」，不依赖网络与数据库；按市场拆分规则（`market_rules.py`），支持做空、融资、保证金与强平。

### 5.8 可实盘检查与实盘对照（v1.8）

**可实盘检查**（`readiness.py`）：策略冠军版本或组合同时满足以下五项时标记为「可实盘」；第 4 项未满 3 个月为「待观察」，任一项不满足为「未通过」。

| # | 条件 | 判定 |
|---|---|---|
| 1 | 开发期风险调整后跑赢基准 | 开发期夏普 > 基准同期夏普 |
| 2 | 留出期跑赢基准且不明显退化 | 留出期夏普 > 基准同期夏普，且 ≥ 开发期夏普的 50% |
| 3 | 逐年稳定 | 开发期完整年份中 ≥ 50% 跑赢基准 |
| 4 | 实时跟踪 ≥ 3 个月且表现正常 | 当前版本跟踪 ≥ 60 个交易日；健康度不是「提醒」；对账不是「偏差较大」 |
| 5 | 资金规模在容量范围内 | 计划资金（实盘账户初始资金，没有时取跟踪本金）在最低资金与可承载资金之间 |

结果写入 `quant_strategies.readiness` / `quant_portfolios.readiness`；每晚任务刷新跟踪中的策略与组合，页面可手动重新检查。实盘对照结果作为参考列出，不参与判定。

**实盘对照**（`real.py`）：

- 实盘账户（`real_accounts`）关联一个策略或组合，对照它当前的实时跟踪账户；记录初始资金、开始日期与资金转入转出。
- 成交（`real_fills`）手工录入或导入券商导出的成交明细：自动识别 UTF-8 / GBK、表头前的说明行、常见中文列名（成交日期、证券代码、买卖标志、成交数量、成交价格 / 均价、佣金 + 印花税 + 过户费 或 费用合计），6 位代码按首位补交易所后缀；非买卖记录跳过并说明原因；与已有记录完全相同的视为重复。
- 实盘净值：从初始资金出发按成交逐日重建持股与现金，收盘价估值，复权因子变化按比例调整持股数（与交易引擎一致）；有资金转入转出时用时间加权收益。
- 对比：与模拟账户的收益差、跟踪误差、日收益相关；逐笔配对（同标的、同方向，实盘日期在模拟日期后 0～5 个交易日内就近配对）得到成交价差（基点，正数表示实盘更差）、价差损耗金额、平均延迟；未配对的为「漏做 / 多做」；最新持仓权重对照。
- 判定：跟踪误差 ≤ 5% 且年化差 ≤ 2%：一致；≤ 10% 且 ≤ 5%：有偏差；否则偏差较大。
- 不连接券商，不保存券商账号或密码，只保存用户提供的成交数据。

### 5.9 访问保护（app/auth）

- 多用户（2026-10-11）：用户名 + 密码登录，角色 `admin` / `user`，可停用。密码用 scrypt（N=2^14、r=8、p=1，16 字节随机盐）哈希后存于 `users.password_hash`。
- 首次设置：管理员（id 1）没有密码时，`POST /api/auth/setup` 设置密码，必须带**首次设置码**（2026-10-13）。设置码在启动时生成，写进日志和 `data/setup-code.txt`（权限 600），用过即删；错误计入登录限速。
- 注册：`POST /api/auth/register` 只在管理员打开「开放注册」时可用（`app_settings` 的 `auth.registration`；新安装默认关闭，迁移 `a8c1d5f7e3b9` 让已有安装保持开放），同一 IP 每小时最多 5 个。
- 登录凭证：`用户ID.代数.到期时间.签名`，HMAC-SHA256 签名密钥由 `data/secret.key` 派生；放在 HttpOnly、SameSite=Lax 的 Cookie `sa_session` 中，有效 30 天。修改密码、退出所有设备、命令行重置时 `users.session_gen` 加 1，旧凭证全部失效。
- `AuthMiddleware`（纯 ASGI）拦截 `/api/*`、`/ws/*` 与接口文档，只放行 `/api/auth/status|setup|login|logout`；未登录返回 401 和 `code`（`login_required` / `setup_required`），WebSocket 直接关闭（4401）。页面静态文件不拦截，前端路由守卫与 axios 拦截器负责跳转登录页。中间件查询密码状态带 5 秒缓存。
- 登录限速：按来源 IP，15 分钟内失败 5 次后锁定到最早一次失败满 15 分钟（内存中，重启清零）。
- 忘记密码：`uv run python -m app.auth reset-password` 清除管理员密码、使其所有凭证失效，并生成新的首次设置码。
- 主密钥：`data/secret.key` 格式不对时启动即报错；启动时检查已保存的密钥能否解密（`core/keycheck.py`），解密失败的条目在页面上标出，调用时返回 400 和明确提示。
- 配置 `auth_enabled`（默认开启；测试中关闭，`tests/test_auth.py` 单独开启验证）。

---

### 5.10 交易成本、数据质量与滚动选择检验

- **交易成本**（`quant/costs.py`）：`TradeCosts`（佣金费率、最低佣金、是否扣红利税）存于 `app_settings`（键 `trade_costs`）。`run_backtest` 未指定成本模型时使用当前设置；保存时同步到运行中量化跟踪赛道的 `rule_params`（`commission_rate`、`commission_min`、`cn_dividend_tax_by_holding`），新建跟踪时也套用。设置签名（含成本计算口径版本 `COST_LOGIC`）与上次重新回测时（键 `evaluated_costs`）不同时，启动时在后台 `tasks.reevaluate_all`（逐个策略先算后写，之后刷新「可实盘」检查）。
- **红利税**：回测在除息日按 `Panel.cash_div`（由 `corporate_actions` 加载的每股派现）× 除息前持股数 × 持有期税率从现金扣除；交易引擎 `apply_actions` 按 `positions.opened_date`（本轮建仓日）用 `fees.dividend_tax_rate` 计算。ETF 免征。
- **数据质量**（`quant/quality.py`）：`scan_panel` 在行情矩阵上逐标的检查跳变、价格不一致、复权因子下降、长期无行情、未更新；结果存于 `app_settings`（`data_quality`，忽略列表 `data_quality_ignored`）。每晚 `tasks.nightly` 数据更新后扫描，新出现的未忽略错误推送「异常提醒」；`repair` 调用 `refetch_bars` + `update_corporate_actions` 后后台重新回测并重新扫描。
- **基金份额折算识别**：行情矩阵 `_fix_fund_splits` 按各基金的涨跌停限制（+1.5% 余量）识别非交易造成的跳变并计入复权因子；数据源 `fund_actions` 仍用 ±45%（只把明确的拆分 / 合并写成除权事件）。
- **滚动选择检验**（`evaluate.walk_forward`）：在各版本开发期 + 留出期月度收益上逐年按此前夏普挑版本，输出每年挑出版本 / 初始版本 / 平均 / 当年最好的收益与排名分位；结果在 `service.research_quality` 的 `walk_forward` 中。
- **健康度推送**：`readiness.check` 输出附带 `health`；`tasks.refresh_readiness` 比较前后两次结果，用 `notify.service.readiness_messages` 生成「健康度与可实盘」消息（事件 `health`）。

### 5.11 AI 交易复核（quant/review.py）

- 组合设置 `settings.review = {enabled, account_id, agent_id, provider_id, model, since}`；AI 复核选手为 `kind=quant`、`quant_portfolio_id=组合`、`params.ai_review=true`（`provider_id` / `model` 为复核模型），账户直接加入规则账户所在的运行中赛道（有意绕过「启动后不能加选手」，作为孪生对照）。`review.rule_agent` 查找规则选手时排除它。
- 每日流程（`arena/cycle.py`）：AI 复核选手走 `review.decide_reviewed`：读取规则账户当前状态与上一轮决策，调用 `decide_portfolio(规则账户)` 得到与规则账户完全相同的决策；有目标时，对本账户未持有、目标 > 0 的股票收集材料（`_candidates_text`：行情矩阵 + 巨潮公告 + 东方财富新闻），按 10 只一批调用模型（`review_symbols`，并发 2，JSON 输出），否决的目标置 0；结果写入决策 `parsed.review`（items、vetoed、errors、tokens、cost）。规则不调仓但本账户首次决策且空仓时，复制规则账户最近一次目标。
- 失败处理：模型调用失败或缺少结果的股票按规则执行，记录在 `errors` / 理由中。
- 报告 `review.report`：两账户共同日期的净值归一对比；每条复核的执行日开盘 → 最新收盘收益与相对沪深300 的超额，按否决 / 放行汇总。
- 推送：`notify.service._review_lines` 在组合的调仓提醒后附上同一数据日期 AI 复核账户的否决与理由。
- 接口：`GET/POST/DELETE /api/quant/portfolios/{id}/review`。

### 5.12 技能系统（app/skills）

- 表：`skills`（包、状态 pending / installing / installed / failed / rejected / removed、审阅清单 manifest、运行策略 allow_domains / commands / user_only / setup_command、包摘要）、`skill_calls`（审计）、`skill_markets`（市场与索引缓存）、`skill_requests`（AI 申请）。包与工作目录在 `data/skills/<id>/pkg|home`，出网代理配置在 `data/skills/_proxy/`。
- `package.py`：GitHub（codeload zip，取子目录）/ 上传 zip；安全解压（路径穿越、符号链接、60 MB / 3000 文件上限）；解析 SKILL.md 元数据；扫描文件类型（ELF / PE / Mach-O 为程序，脚本扩展名或 #! 为脚本）、域名、风险提示；推测命令入口与安装命令。
- `gate.py`：总开关 `skills_enabled`（配置不写时：已有已安装技能则开启，新安装关闭）；关闭时技能接口返回 409、`tools.catalog` 为空、市场工具不提供、启动不清理会话容器，`/api/auth/status` 的 `features.skills` 告诉前端隐藏技能页。
- `sandbox.py`：镜像 `stockarena-skill-runner:1`（Debian + bash / curl / python3 / nodejs，以主机网络构建）；内部网络 `sa-skill-net`（--internal，172.31.250.0/24）；出网代理容器 `sa-skill-proxy`（主机网络、只绑定 172.31.250.1:3128、只读、无权限）。技能容器：`--rm --user <服务用户> --cap-drop ALL --security-opt no-new-privileges --read-only --tmpfs /tmp --pids-limit 256 --memory 1g --cpus 2`，挂载包（只读）与工作目录，HTTP(S)_PROXY 带技能令牌。服务器 `net.ipv4.ip_forward=0`，容器本身无法出网，只能经代理。
- `egress_proxy.py`：独立脚本，CONNECT / HTTP 代理，按 Proxy-Authorization 的令牌查 `allow.json`（文件变化自动重读），目标主机等于或属于允许域名才放行，逐条写 `proxy.log`；运行结果中的「被拦截域名」由日志汇总。
- `service.py`：暂存（只扫描）、批准（`approve` → 后台 `install` 在沙箱中执行安装命令）、启停、卸载、`call`（命令策略：只能调用声明的入口、不经 shell、参数长度限制、`user_only` 子命令只允许用户）、`read_file`（只读包内文本）、审计、`sync_proxy`（写允许清单）。
- `tools.py`：给模型的 `read_skill` / `run_skill` 与市场工具 `search_skill_market` / `request_skill_install`；系统提示只列技能名称与说明（渐进式披露），并声明技能返回内容不是指令。
- `market.py`：github 市场（Git Trees API 列出 SKILL.md，读取元数据建索引）、skillsmp 市场（公开搜索接口，缓存一天，只保留 GitHub 地址）。
- `requests.py`：AI 申请（去重、待处理上限、每日搜索上限、开关 `app_settings.skills.ai_requests`）、暂存并推送（事件 `skills`），批准 / 拒绝技能时同步申请状态。
- AI 复核（`quant/review.py`）：有技能或市场可用时用工具模式（`run_llm_tools`，每批最多 8 次工具调用、最多 1 次安装申请），失败退回一次性复核；技能调用记入复核记录。
- 接口：`/api/skills`（列表、沙箱初始化、暂存、详情、文件、策略、批准、拒绝、卸载、终端、调用记录）、`/api/skills/markets*`、`/api/skills/market-search`、`/api/skills/requests*`、`/api/skills/settings`。

---

## 7. 数据模型

| 分组 | 表 | 说明 |
|---|---|---|
| 行情 | `instruments` | 证券信息（类型、板块、每手股数、价位、涨跌幅、ST、`history_from` 已回填的最早日期） |
| | `index_members` | 指数成分的纳入 / 剔除日期（时点股票池） |
| | `daily_bars` | 日线（未复权价、成交额、交易所前收盘价、后复权因子） |
| | `industries` | 申万 2021 一级 / 二级行业及生效日期（巨潮资讯行业变更记录） |
| | `corporate_actions` | 分红送转（含由价格跳变识别的 ETF 份额折算） |
| | `trading_days`、`minute_bars`、`fundamentals`（含最早可用日期）、`news`、`data_source_status` | |
| 策略工坊 | `quant_strategies` | 策略：风格、说明、股票池、基准、研究模型、设置（区间与门槛）、冠军版本、自动优化、跟踪账户、长期研究笔记、可实盘检查结果 |
| | `quant_portfolios` | 多风格组合：成员与权重、跟踪账户、可实盘检查结果 |
| | `real_accounts`、`real_fills` | 实盘对照：实盘账户（对照的策略 / 组合、初始资金、开始日期、资金转入转出）与实盘成交 |
| | `quant_versions` | 版本：规格、来源、理由、状态、检验结论、各区间报告（开发期含月度收益、诊断与升级时的配对检验）、稳健性、曲线、开发期夏普 |
| | `quant_research_runs` | 研究运行：状态、进度、轮数、升级次数、每轮日志 |
| AI 对比 | `llm_providers`（API Key 加密存储）、`agents`（kind：llm / baseline / quant；quant 跟随策略或组合）、`prompt_templates`、`rule_sets` | 配置 |
| | `tracks`、`accounts`、`positions`、`orders`、`fills`、`cash_ledger`、`nav_snapshots`、`rounds`、`decisions`、`events` | 赛道运行与账务（现金以流水为准，可对账） |
| | `strategy_versions`、`reflections`、`consistency_checks`、`experiments` | 自我进化、一致性评估、进化实验 |
| 推送 | `notify_channels`、`notifications` | 推送通道（Server 酱 / PushPlus / 企业微信机器人，密钥加密）与已发送记录（按通道 + 去重键去重） |
| 其他 | `users` | 预留多用户（业务表均带 `user_id`） |

关系：`quant_versions.strategy_id → quant_strategies`；`quant_strategies.follow_account_id → accounts`（跟踪账户）；`agents.quant_strategy_id → quant_strategies`（量化选手）。

---

## 8. 调度与后台任务

| 时间（市场当地时间，工作日） | 任务 |
|---|---|
| A股 15:45、16:30、18:00、20:30（港股、美股类似，各 4 次） | 运行中赛道的日终流程（幂等，多次尝试应对数据延迟），包括实时跟踪赛道 |
| A股 19:30 | 策略工坊每晚任务：更新各股票池行情与财报 → 刷新跟踪中策略与组合的「可实盘」检查 → 依次优化开启「自动优化」的策略 |
| A股 16:20、17:00、18:40、21:00 | 调仓提醒检查（`app/notify`）：跟踪中的组合 / 策略有下一交易日订单时推送到微信（同一天只发一次）；21:00 那次对没有完成决策的账户推送异常提醒 |
| 启动后 20 秒 | 补跑错过的日终流程 |
| 启动时 | 把上次未完成的研究标记为中断 |
| 手动 | 数据准备、创建初始策略库、研究、历史回放、复盘、一致性评估、进化实验（均为后台任务，界面轮询进度） |

**写锁约定**：SQLite 同一时间只有一个写事务。长时间计算（回测、模型调用、网络请求）必须在事务之外完成，再用短事务写入；忙等待 30 秒。行情服务的批量更新逐个标的提交，不在网络请求期间持有写锁。

---

## 9. 接口清单

| 前缀 | 主要接口 |
|---|---|
| `/api/quant` | `GET factors`、`GET seeds`、`GET data` / `POST data/prepare`；`GET/POST strategies`、`POST strategies/seed`、`GET/PATCH strategies/{id}`；`GET versions/{id}`、`GET versions/{id}/trades?window=`、`POST strategies/{id}/versions`（手动编辑）、`POST versions/{id}/promote`；`POST strategies/{id}/research`、`GET runs/{id}`、`POST runs/{id}/cancel`；`GET strategies/{id}/signal?capital=`、`POST strategies/{id}/follow`、`GET strategies/{id}/tracking`；`GET factor-research?window=dev|holdout`；`GET/POST portfolios`、`GET/PATCH/DELETE portfolios/{id}`、`GET portfolios/{id}/signal`、`POST portfolios/{id}/follow`、`GET portfolios/{id}/tracking`；`GET accounts/{id}/check`（对账与健康度）、`GET strategies|portfolios/{id}/capacity`、`POST strategies|portfolios/{id}/replay-check`、`GET strategies|portfolios/{id}/replay-checks`；`GET strategies|portfolios/{id}/readiness?refresh=`；`GET/POST real-accounts`、`PATCH/DELETE real-accounts/{id}`、`GET/POST real-accounts/{id}/fills`、`DELETE real-accounts/{id}/fills/{fid}`、`POST real-accounts/{id}/import`（commit=false 预览）、`GET real-accounts/{id}/report` |
| `/api/tracks` | 赛道增删查、报名 / 移除选手、启动 / 暂停 / 恢复 / 停止 / 立即运行、排行榜、净值、轮次与快照、事件、导出、HTML 报告、一致性评估、复制 |
| `/api/accounts`、`/api/decisions`、`/api/reflections`、`/api/compare` | 账户持仓 / 订单 / 成交 / 净值 / 流水 / K 线；决策回放；策略笔记与复盘；对比分析 |
| `/api/experiments` | 离线进化实验 |
| `/api/providers`、`/api/agents`、`/api/meta` | 模型供应商（测试连接、模型列表）、选手、模板 / 规则集 / 市场 / 股票池 / 风格库 |
| `/api/system` | 健康检查、数据源状态、事件、调度任务 |
| `/api/notify` | 推送通道增删改（密钥只写不读）、发送测试、立即检查、推送记录 |
| `/api/quant`（v1.10） | `GET/PUT costs`（交易成本）、`POST reevaluate`（重新回测所有在用策略）、`GET data-quality`、`POST data-quality/scan`、`POST data-quality/ignore`、`POST data-quality/repair` |
| `/api/auth` | `GET status`、`POST setup`（首次设置密码）、`POST login`、`POST logout`、`POST password`（修改密码）、`POST logout-all` |

完整的请求与响应格式见运行中的 `http://127.0.0.1:18080/docs`。

---

## 10. 配置

| 文件 | 内容 |
|---|---|
| `config/config.yaml`（不进版本库，样例见 `config.example.yaml`；每一项见 [CONFIG.md](CONFIG.md)） | 数据目录、托管前端、对外地址、直连域名、技能开关、规则默认值等 |
| `config/universes/*.yaml` | 股票池（策略工坊默认 `cn_csi300_hist`：沪深300 全时点，含历史上被剔除和已退市的成分股 + 核心 ETF；`cn_csi800_hist` 为中证800 全时点） |
| `config/rulesets/*.yaml` | 交易规则集（只做多、两融、港美股） |
| `config/strategies.yaml` | 7 种风格的初始策略规格与风格说明 |
| `config/research/default.yaml` | AI 研究员系统提示与规格字段说明 |
| `config/prompts/`、`config/reflection/`、`config/personas.yaml` | AI 对比模块的决策模板、复盘模板、风格库 |

---

## 11. 关键设计约束

1. **无前视**：信号只用当日收盘及以前的数据；财报按最早可用日期；AI 研究员看不到验证与留出区间的细节。
2. **不执行 AI 代码**：AI 只能输出受限的 JSON 规格，程序校验后执行。
3. **同一套指标**：策略回测与赛道账户使用 `analytics/metrics.py`，结果可直接对比。
4. **账务精确**：交易引擎使用 Decimal、资金以流水为准、每日对账；回测内核使用 float 只用于研究。
5. **安全**：接口需要登录（见 5.9）；API Key 在界面输入并加密存储；`data/`、`config/config.yaml`、`.env` 不进版本库。
6. **数据修复可重放**：数据源口径修正后可用 `uv run python -m app.market.repair` 重新下载并覆盖缓存，用 `service.reevaluate` 重新回测所有版本。

---

## 12. 开发与测试

```bash
cd backend && uv run pytest -q         # 后端测试（约 480 个，3～4 分钟）
cd backend && uv run ruff check .
cd frontend && npm run lint && npm run format:check && npm run type-check && npm test
```

- 测试不访问网络：行情源与模型均用伪造对象替换；策略工坊测试用合成行情矩阵。
- 数据库迁移：修改 ORM 后 `uv run alembic revision --autogenerate -m "说明"`，启动时自动升级；迁移前先备份 `data/stockarena.db`。
