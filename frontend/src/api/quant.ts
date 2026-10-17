import { http } from './http'

// ---------------- 策略工坊（PRD 3.8） ----------------

export interface WindowBrief {
  annualized_return: number | null
  cumulative_return: number | null
  sharpe: number | null
  calmar: number | null
  max_drawdown: number | null
  excess_return: number | null
  turnover: number | null
  days: number | null
}

export interface YearRow {
  year: number
  return: number | null
  benchmark: number | null
  excess: number | null
  max_drawdown: number | null
  partial: boolean
}

export interface WindowReport extends WindowBrief {
  start?: string
  end?: string
  benchmark_return?: number | null
  volatility?: number | null
  sortino?: number | null
  cost_ratio?: number | null
  trades?: number
  avg_exposure?: number | null
  avg_holdings?: number | null
  win_rate?: number | null
  closed?: number
  yearly?: YearRow[]
  segments?: { period: string; return: number | null; benchmark: number | null; days: number }[]
  tuning?: { changes: string[]; evals: number; base_score: number | null; score: number | null; note?: string }
  monthly?: [string, number][]
  vs_champion?: PairedTest
  diagnostics?: {
    drawdowns: { start: string; trough: string; recovered: string | null; depth: number }[]
    best: { symbol: string; name: string; pnl: number }[]
    worst: { symbol: string; name: string; pnl: number }[]
    blocked: number
  }
}

/** 升级检验时与当时冠军的配对比较（开发期，按相同波动换算） */
export interface PairedTest {
  months: number
  excess_annual: number
  te_annual: number
  t: number | null
  p: number
  year_wins: number
  years: number
  by_year: Record<string, number>
  champion_id: number | null
  n_tests: number
}

export interface Robustness {
  samples?: number
  median_sharpe?: number | null
  min_sharpe?: number | null
  max_sharpe?: number | null
  ratio?: number | null
}

export type VersionStatus = 'champion' | 'candidate' | 'rejected' | 'retired' | 'invalid'

export interface VersionBrief {
  id: number
  number: number
  status: VersionStatus
  source: 'initial' | 'ai' | 'manual'
  title: string
  rationale: string
  decision: string
  score: number | null
  parent_id: number | null
  run_id: number | null
  created_at: string
  promoted_at: string | null
  train: WindowBrief
  valid: WindowBrief
  dev: WindowBrief & { vs_champion: PairedTest | null }
  holdout: WindowBrief
  robustness: Robustness
}

export interface Curves {
  dates: string[]
  nav: number[]
  bench: number[]
  train_end?: string
  valid_end?: string
}

export interface VersionDetail extends VersionBrief {
  spec: Record<string, unknown>
  rules: string[]
  train_full: WindowReport
  valid_full: WindowReport
  dev_full: WindowReport
  holdout_full: WindowReport
  curves: Curves
}

export interface RoundLog {
  round: number
  status: string
  error?: string
  analysis?: string
  tokens_in: number
  tokens_out: number
  cost: number
  latency_ms: number
  promoted?: number | null
  lessons?: string[]
  candidates?: {
    version_id?: number
    number?: number
    title: string
    status: string
    dev_sharpe?: number | null
    p?: number | null
    excess?: number | null
    year_wins?: string | null
    tuning?: string[]
    reason: string
  }[]
}

export interface ResearchRun {
  id: number
  strategy_id: number
  trigger: 'manual' | 'schedule'
  status: 'pending' | 'running' | 'done' | 'failed' | 'cancelled'
  rounds: number
  rounds_done: number
  promoted: number
  progress: string
  summary: string
  error: string
  log: RoundLog[]
  created_at: string
  finished_at: string | null
}

export interface LabSettings {
  train_start: string
  train_end: string
  valid_start: string
  valid_end: string
  holdout_start: string
  initial_cash: number
  max_drawdown: number
  robust_keep: number
  max_turnover: number
  candidates: number
  rounds: number
  fold_win_rate: number
  p_max: number
  min_holdings: number
  tune: boolean
  tune_budget: number
}

export interface Strategy {
  id: number
  name: string
  style: string
  description: string
  market: string
  universe_name: string
  benchmark: string
  provider_id: number | null
  provider_name: string | null
  model: string | null
  settings: Partial<LabSettings>
  auto_optimize: boolean
  status: 'active' | 'archived'
  follow_account_id: number | null
  readiness: ReadinessBrief | null
  champion: VersionBrief | null
  champion_curves: Curves | null
  versions: number
  promotions: number
  dsr: number | null
  last_run: ResearchRun | null
  created_at: string
  /** 另存来源；不是另存的策略为 null */
  origin: StrategyOrigin | null
  /** 公共策略：所有用户可查看、另存；只有所有者（管理员）能修改 */
  is_public: boolean
  /** 是否是自己的策略（别人的公共策略只读） */
  mine: boolean
}

export interface StrategyOrigin {
  /** new：新建前在对话框里试算过（只有 prior_tests、trial_scores）；没有 kind 的是另存 */
  kind?: 'new'
  strategy_id?: number
  strategy_name?: string
  version_id?: number
  version_number?: number
  version_status?: VersionStatus
  universe_name?: string
  /** 来源中已与该版本比较过的候选数（新策略的升级检验继承这个数） */
  prior_tests: number
  max_version_id?: number
  base_version_id: number
  at: string
}

/** 一句话创建策略：AI 按描述生成的规则（只生成，不回测） */
export interface StrategyDraft {
  name: string
  description: string
  spec: Record<string, unknown>
  rules: string[]
  /** 描述里没有实现的条件及原因 */
  unsupported: string[]
  notes: string
  tokens_in: number
  tokens_out: number
  cost: number
}

/** 试算结果：只有开发期，不保存、不含留出期 */
export interface PreviewResult {
  window: { start: string; end: string }
  dev: WindowBrief & {
    volatility: number | null
    cost_ratio: number | null
    avg_holdings: number | null
    win_rate: number | null
    trades: number | null
    yearly?: YearRow[]
  }
  curves: Curves
  rules: string[]
  spec: Record<string, unknown>
  warnings: string[]
}

export interface ForkIn {
  version_id?: number | null
  name: string
  description?: string | null
  copy_notes: boolean
  provider_id: number | null
  model: string | null
  auto_optimize: boolean
}

/** 研究过程是否有效（只用于评估，不参与挑选） */
export interface ResearchQuality {
  versions: number
  months: number
  pbo: number | null
  rank_corr: number | null
  rank_n: number
  tests_since_champion: number
  walk_forward: WalkForward | null
}

/** 滚动选择检验：每年只用此前数据挑「当时最好」的版本，看下一年表现 */
export interface WalkForward {
  n: number
  versions: number
  beat_seed: number
  beat_avg: number
  mean_percentile: number
  chosen_total: number
  seed_total: number
  avg_total: number
  years: {
    year: number
    months: number
    holdout: boolean
    chosen: number
    chosen_return: number
    seed_return: number
    avg_return: number
    best_return: number
    percentile: number
  }[]
}

export interface StrategyDetail extends Strategy {
  quality: ResearchQuality
  champion_detail: VersionDetail | null
  version_list: VersionBrief[]
  runs: ResearchRun[]
  settings_full: LabSettings
  research_notes: { text: string; source: 'ai' | 'manual'; at?: string; run_id?: number }[]
}

export interface TaskStatus {
  running: boolean
  message: string
  started_at?: string
  finished_at?: string
}

export interface DataStatus {
  universe: string
  coverage: {
    symbols?: number
    with_data?: number
    benchmark?: string
    bench_first?: string | null
    bench_last?: string | null
    median_first?: string | null
    fundamentals?: number
    stocks?: number
    ready?: boolean
    /** 全时点股票池的历史成分是否已生成（false：Baostock 获取失败，只用今天的成分回看历史） */
    pit_ready?: boolean | null
    error?: string
  }
  task: TaskStatus | null
  default_start: string
  /** 完整数据还没准备好时：快速体验股票池的数据情况（见「快速开始」） */
  quick?: { universe: string; coverage: DataStatus['coverage']; task: TaskStatus | null }
}

export interface FactorInfo {
  name: string
  title: string
  description: string
  fundamental: boolean
  timing: boolean
  params: Record<string, { default: number; min: number; max: number; title: string; integer: boolean }>
}

export interface Seed {
  style: string
  title: string
  description: string
  spec: Record<string, unknown>
}

export interface StrategyPatch {
  name?: string
  description?: string
  provider_id?: number | null
  model?: string | null
  settings?: Partial<LabSettings>
  auto_optimize?: boolean
  status?: 'active' | 'archived'
  research_notes?: string[]
  /** 只有管理员可以设置 */
  is_public?: boolean
}

export const quantApi = {
  factors: async () => (await http.get<FactorInfo[]>('/api/quant/factors')).data,
  seeds: async () => (await http.get<Seed[]>('/api/quant/seeds')).data,
  dataStatus: async (universe?: string) =>
    (await http.get<DataStatus>('/api/quant/data', { params: universe ? { universe } : {} })).data,
  prepare: async (universe?: string) => (await http.post('/api/quant/data/prepare', universe ? { universe } : {})).data,
  /** 快速开始：几分钟准备好快速体验股票池，随后自动在后台准备完整数据 */
  quickStart: async () => (await http.post('/api/quant/data/quick-start')).data,
  list: async () =>
    (await http.get<{ strategies: Strategy[]; seed_task: TaskStatus | null }>('/api/quant/strategies')).data,
  seed: async (body: { provider_id: number | null; model: string | null; styles?: string[] }) =>
    (await http.post('/api/quant/strategies/seed', body)).data,
  specOptions: async () => (await http.get<{ industries: string[] }>('/api/quant/spec-options')).data,
  create: async (body: {
    name: string
    style: string
    description: string
    spec: Record<string, unknown>
    universe_name?: string
    provider_id: number | null
    model: string | null
    auto_optimize?: boolean
    settings?: Partial<LabSettings>
    /** 新建前每次试算的开发期夏普，计入测试次数 */
    trial_scores?: (number | null)[]
  }) => (await http.post<Strategy>('/api/quant/strategies', body, { timeout: 180000 })).data,
  draft: async (body: {
    text: string
    provider_id: number
    model?: string | null
    base_spec?: Record<string, unknown>
  }) => (await http.post<StrategyDraft>('/api/quant/strategies/draft', body, { timeout: 330000 })).data,
  fork: async (id: number, body: ForkIn) =>
    (await http.post<Strategy>(`/api/quant/strategies/${id}/fork`, body, { timeout: 180000 })).data,
  preview: async (body: { spec: Record<string, unknown>; universe_name?: string; strategy_id?: number }) =>
    (await http.post<PreviewResult>('/api/quant/preview', body, { timeout: 120000 })).data,
  get: async (id: number) => (await http.get<StrategyDetail>(`/api/quant/strategies/${id}`)).data,
  patch: async (id: number, body: StrategyPatch) =>
    (await http.patch<Strategy>(`/api/quant/strategies/${id}`, body)).data,
  version: async (id: number) => (await http.get<VersionDetail>(`/api/quant/versions/${id}`)).data,
  manualVersion: async (id: number, body: { spec: Record<string, unknown>; title: string; rationale: string }) =>
    (await http.post<VersionDetail>(`/api/quant/strategies/${id}/versions`, body, { timeout: 180000 })).data,
  promote: async (versionId: number) => (await http.post(`/api/quant/versions/${versionId}/promote`)).data,
  research: async (id: number, rounds?: number) =>
    (await http.post<ResearchRun>(`/api/quant/strategies/${id}/research`, { rounds })).data,
  run: async (id: number) => (await http.get<ResearchRun>(`/api/quant/runs/${id}`)).data,
  cancelRun: async (id: number) => (await http.post(`/api/quant/runs/${id}/cancel`)).data,
  timeline: async (id: number) => (await http.get<Timeline>(`/api/quant/strategies/${id}/timeline`)).data,
}

/** AI 优化时间线（只含开发期结果） */
export interface TimelineMetrics {
  sharpe?: number | null
  annualized_return?: number | null
  max_drawdown?: number | null
  turnover?: number | null
  p?: number | null
  n_tests?: number | null
  excess_annual?: number | null
  year_wins?: string | null
}

export interface TimelineCandidate {
  number?: number
  title: string
  source?: string
  status: string
  /** 升级为冠军 / 未采纳 / 与已试过的版本相同，跳过 … */
  outcome: string
  rationale?: string
  decision?: string
  parent_number?: number | null
  /** 与当时冠军相比：去掉的规则、加上的规则 */
  diff?: { removed: string[]; added: string[] }
  metrics?: TimelineMetrics
  parent_sharpe?: number | null
  created_at?: string
}

export interface TimelineEvent {
  type: 'round' | 'manual' | 'version'
  at: string
  run_id?: number
  trigger?: string
  round?: number
  status?: string
  analysis?: string
  lessons?: string[]
  error?: string
  candidates: TimelineCandidate[]
  promoted?: number | null
}

export interface Timeline {
  start: {
    number: number | null
    title: string
    rules: string[]
    metrics: TimelineMetrics
    created_at: string | null
    forked_from: { strategy_id: number; strategy_name: string; version_number: number; prior_tests: number } | null
    trials_before_create: number | null
  }
  events: TimelineEvent[]
  summary: {
    versions: number
    ai_candidates: number
    ai_promoted: number
    manual: number
    rounds: number
    champion: number | null
  }
}

export const STATUS_TEXT: Record<VersionStatus, string> = {
  champion: '冠军',
  candidate: '候选',
  rejected: '淘汰',
  retired: '曾为冠军',
  invalid: '无效',
}

export const STATUS_TYPE: Record<VersionStatus, 'success' | 'info' | 'danger' | 'warning' | 'primary'> = {
  champion: 'success',
  candidate: 'primary',
  rejected: 'info',
  retired: 'warning',
  invalid: 'danger',
}

export const SOURCE_TEXT: Record<string, string> = { initial: '初始', ai: 'AI', manual: '手动' }

export const RUN_STATUS: Record<string, { text: string; type: 'success' | 'info' | 'danger' | 'warning' | 'primary' }> =
  {
    pending: { text: '排队中', type: 'info' },
    running: { text: '进行中', type: 'primary' },
    done: { text: '完成', type: 'success' },
    failed: { text: '失败', type: 'danger' },
    cancelled: { text: '已取消', type: 'warning' },
  }

// ---------------- 每日信号与实时跟踪 ----------------

export interface SignalHolding {
  symbol: string
  name: string
  weight: number
  close: number
  shares?: number
  amount?: number
}

export interface Signal {
  date: string
  version: number
  version_title: string
  timing_on: boolean
  eligible: number
  holdings: SignalHolding[]
  cash_weight: number
  every_days: number
  rules: string[]
}

export interface TrackOrder {
  id: number
  symbol: string
  name: string
  side: string
  qty: number
  filled_qty: number
  status: string
  reason: string | null
  note: string | null
}

export interface Tracking {
  account_id: number
  track_id: number
  track_status: string
  start_date: string | null
  initial_cash: number
  nav: { date: string; nav: number; bench: number | null }[]
  positions: {
    symbol: string
    name: string
    qty: number
    avg_cost: string
    close: string
    market_value: string
    pnl: string
    pnl_pct: number | null
    weight: number | null
  }[]
  last_decision: {
    data_date: string
    fill_date: string
    analysis: string | null
    parsed: { rebalance?: boolean; quant_version?: number; since_rebalance?: number } | null
  } | null
  orders: TrackOrder[]
  open_orders: TrackOrder[]
}

export const signalApi = {
  signal: async (id: number, capital?: number) =>
    (
      await http.get<Signal>(`/api/quant/strategies/${id}/signal`, {
        params: capital ? { capital } : {},
        timeout: 120000,
      })
    ).data,
  follow: async (id: number, initialCash = 1000000) =>
    (
      await http.post<{ account_id: number; track_id: number }>(
        `/api/quant/strategies/${id}/follow`,
        {
          initial_cash: initialCash,
        },
        { timeout: 60000 },
      )
    ).data,
  tracking: async (id: number) => (await http.get<Tracking | null>(`/api/quant/strategies/${id}/tracking`)).data,
}

// ---------------- 交易明细 ----------------

export interface PickFactor {
  name: string
  direction: 'high' | 'low'
  value: number | null
  pct: number | null
}

export interface Pick {
  symbol: string
  name: string
  rank: number | null
  of: number
  score: number | null
  weight: number | null
  factors: PickFactor[]
  defensive?: boolean
}

export interface RebalanceRecord {
  date: string
  exec: string
  kind: 'rebalance' | 'stop'
  eligible?: number
  timing?: string | null
  timing_on?: boolean
  picks?: Pick[]
  drops: { symbol: string; name: string; reason: string }[]
  kept?: number
  new?: number
}

export interface EpisodeRow {
  symbol: string
  name: string
  type: string
  entry: string
  exit: string | null
  days: number
  cost: number
  proceeds: number
  pnl: number
  ret: number
  reason: string
  open: boolean
}

export interface SymbolRow {
  symbol: string
  name: string
  type: string
  episodes: number
  wins: number
  pnl: number
  days: number
  best: number | null
  worst: number | null
}

export interface TradeRow {
  date: string
  symbol: string
  name: string
  side: 'buy' | 'sell'
  qty: number
  price: number
  amount: number
  fee: number
  note: string
}

export type DetailWindow = 'train' | 'valid' | 'dev' | 'holdout' | 'all'

export interface TradeDetails {
  summary: {
    window: DetailWindow
    start: string
    end: string
    trades: number
    buys: number
    sells: number
    fees: number
    closed: number
    open: number
    win_rate: number | null
    avg_win: number | null
    avg_loss: number | null
    avg_days: number | null
    pnl: number
    symbols: number
    rebalances: number
    stops: number
    exit_reasons: { reason: string; count: number; pnl: number; avg_ret: number }[]
  }
  rebalances: RebalanceRecord[]
  episodes: EpisodeRow[]
  symbols: SymbolRow[]
  trades: TradeRow[]
  trades_total: number
}

export const detailsApi = {
  trades: async (versionId: number, window: DetailWindow) =>
    (await http.get<TradeDetails>(`/api/quant/versions/${versionId}/trades`, { params: { window }, timeout: 120000 }))
      .data,
}

// ---------------- 因子成绩单 ----------------

export interface FactorScore {
  name: string
  params: Record<string, number>
  label: string
  title: string
  fundamental: boolean
  periods: number
  ic: number
  icir: number | null
  t: number | null
  positive: number
  direction: 'high' | 'low'
  years: Record<string, number>
  quintiles: number[]
  long_short: number | null
}

export interface FactorResearch {
  universe: string
  window: 'dev' | 'holdout'
  start: string
  end: string
  horizon: number
  step: number
  factors: FactorScore[]
}

export const factorApi = {
  scorecard: async (window: 'dev' | 'holdout', strategyId?: number, universe?: string) =>
    (
      await http.get<FactorResearch>('/api/quant/factor-research', {
        params: { window, ...(strategyId ? { strategy_id: strategyId } : {}), ...(universe ? { universe } : {}) },
        timeout: 120000,
      })
    ).data,
}

// ---------------- 多风格组合 ----------------

export interface PortfolioMember {
  strategy_id: number
  weight: number
  name: string
}

export interface Portfolio {
  id: number
  name: string
  description: string
  members: PortfolioMember[]
  follow_account_id: number | null
  readiness: ReadinessBrief | null
  settings: { alloc?: AllocSettings }
  created_at: string
}

/** 组合层资金配置（PRD 3.9 方向 B） */
export interface AllocSettings {
  method: 'fixed' | 'inverse_vol' | 'erc'
  lookback: number
  every: number
  vol_target: number | null
  min_exposure: number
}

export interface PortfolioReport {
  alloc?: AllocSettings & {
    text: string
    names: string[]
    log: { date: string; weights: number[]; scale: number; note: string }[]
  }
  train: WindowReport
  valid: WindowReport
  dev: WindowReport
  holdout: WindowReport
  curves: Curves
  members: {
    strategy_id: number
    name: string
    style: string
    version: number
    weight: number
    dev: { annualized_return: number | null; sharpe: number | null; max_drawdown: number | null }
    holdout: { cumulative_return: number | null; sharpe: number | null; max_drawdown: number | null }
    curve: number[]
  }[]
  correlation: { names: string[]; matrix: number[][] }
  avg_correlation: number | null
}

export interface PortfolioSignal {
  date: string
  members: { name: string; weight: number; version: number; timing_on: boolean; holdings: number }[]
  holdings: (SignalHolding & { from: string[] })[]
  cash_weight: number
}

export interface PortfolioIn {
  name: string
  members: { strategy_id: number; weight: number }[]
  description?: string
  settings?: { alloc: AllocSettings }
}

export const portfolioApi = {
  list: async () => (await http.get<Portfolio[]>('/api/quant/portfolios')).data,
  create: async (body: PortfolioIn) => (await http.post<Portfolio>('/api/quant/portfolios', body)).data,
  update: async (id: number, body: PortfolioIn) =>
    (await http.patch<Portfolio>(`/api/quant/portfolios/${id}`, body)).data,
  remove: async (id: number) => (await http.delete(`/api/quant/portfolios/${id}`)).data,
  get: async (id: number) =>
    (await http.get<Portfolio & { report: PortfolioReport }>(`/api/quant/portfolios/${id}`, { timeout: 120000 })).data,
  signal: async (id: number, capital?: number) =>
    (
      await http.get<PortfolioSignal>(`/api/quant/portfolios/${id}/signal`, {
        params: capital ? { capital } : {},
        timeout: 120000,
      })
    ).data,
  follow: async (id: number, initialCash = 1000000) =>
    (
      await http.post<{ account_id: number; track_id: number }>(
        `/api/quant/portfolios/${id}/follow`,
        {
          initial_cash: initialCash,
        },
        { timeout: 60000 },
      )
    ).data,
  tracking: async (id: number) => (await http.get<Tracking | null>(`/api/quant/portfolios/${id}/tracking`)).data,
}

// ---------------- v1.7：对账、健康度、资金容量、回放校验 ----------------

export interface ReconcileResult {
  status: '一致' | '有偏差' | '偏差较大' | '数据不足' | '不适用'
  message: string
  track_id?: number
  mode?: string
  start?: string
  end?: string
  days?: number
  members?: { name: string; weight: number }[]
  version_changed?: boolean
  live_return?: number
  backtest_return?: number
  gap?: number
  max_gap?: number
  tracking_error?: number
  correlation?: number | null
  trades_live?: number
  trades_backtest?: number
  price_dev_bps?: number | null
  matched_fills?: number
  holdings_overlap?: number
  weight_diffs?: { symbol: string; name: string; live: number; backtest: number }[]
  curve?: { dates: string[]; live: number[]; backtest: number[] }
}

export interface HealthResult {
  status: '正常' | '关注' | '提醒' | '数据不足'
  days: number
  message: string
  checks: { item: string; value: number | null; detail: string; level: '正常' | '关注' | '提醒' }[]
}

export interface CapacityResult {
  levels: {
    capital: number
    dev_annual: number | null
    dev_sharpe: number | null
    holdout_return: number | null
    cost_ratio: number | null
    impact_ratio: number | null
    fill: number | null
    deviation: number
    extra_deviation: number
    exposure: number
    lot_blocked: number
    capped: number
  }[]
  min_capital: number | null
  capacity: number | null
  rule: string
  version?: number
}

export interface ReplayCheck {
  track_id: number
  account_id: number
  status: string
  start_date: string | null
  replay_until: string | null
  progress: { running: boolean; message: string } | null
  created_at: string
}

export type Kind = 'strategy' | 'portfolio'
const base = (kind: Kind, id: number) => `/api/quant/${kind === 'strategy' ? 'strategies' : 'portfolios'}/${id}`

export const checkApi = {
  account: async (accountId: number) =>
    (
      await http.get<{ reconcile: ReconcileResult; health: HealthResult }>(`/api/quant/accounts/${accountId}/check`, {
        timeout: 120000,
      })
    ).data,
  capacity: async (kind: Kind, id: number) =>
    (await http.get<CapacityResult>(`${base(kind, id)}/capacity`, { timeout: 300000 })).data,
  replayCheck: async (kind: Kind, id: number, start?: string) =>
    (
      await http.post<{ account_id: number; track_id: number }>(
        `${base(kind, id)}/replay-check`,
        start ? { start } : {},
      )
    ).data,
  replayChecks: async (kind: Kind, id: number) =>
    (await http.get<ReplayCheck[]>(`${base(kind, id)}/replay-checks`)).data,
}

// ---------------------------------------------------------------- 「可实盘」检查（PRD 3.9）

export type ReadinessStatus = '可实盘' | '待观察' | '未通过'

export interface ReadinessBrief {
  status: ReadinessStatus
  checked_at: string | null
}

export interface ReadinessItem {
  key: string
  name: string
  level: 'pass' | 'fail' | 'pending' | 'info'
  detail: string
  value: number | null
}

export interface Readiness {
  status: ReadinessStatus
  label?: string
  message: string
  items: ReadinessItem[]
  checked_at?: string
}

export const readinessApi = {
  get: async (kind: Kind, id: number, refresh = false) =>
    (await http.get<Readiness>(`${base(kind, id)}/readiness`, { params: { refresh }, timeout: 300000 })).data,
}

// ---------------------------------------------------------------- 实盘对照（PRD 3.9 G8）

export interface CashFlow {
  date: string
  amount: number
  note?: string
}

export interface RealAccount {
  id: number
  name: string
  kind: Kind
  ref_id: number
  ref_name: string
  initial_cash: number
  start_date: string
  cash_flows: CashFlow[]
  note: string
  fills: number
  sim_account_id: number | null
  created_at: string
}

export interface RealAccountIn {
  name: string
  kind: Kind
  ref_id: number
  initial_cash: number
  start_date: string
  cash_flows: CashFlow[]
  note: string
}

export interface RealFill {
  id: number
  trade_date: string
  symbol: string
  name: string
  side: 'buy' | 'sell'
  qty: number
  price: number
  amount: number
  fee: number
  source: 'manual' | 'import'
  note: string
}

export interface RealFillIn {
  trade_date: string
  symbol: string
  side: 'buy' | 'sell'
  qty: number
  price: number
  fee: number
  name?: string
}

export interface ImportResult {
  rows: Omit<RealFill, 'id' | 'amount' | 'source' | 'note'>[]
  skipped: { line: number; reason: string }[]
  columns: Record<string, string | string[]>
  added?: number
  duplicates?: number
}

export interface ExecutionPair {
  date: string
  sim_date: string
  symbol: string
  name: string
  side: 'buy' | 'sell'
  lag: number
  real_qty: number
  sim_qty: number
  real_price: number
  sim_price: number
  dev_bps: number
  cost: number
}

export interface ExecutionTrade {
  date: string
  symbol: string
  name: string
  side: 'buy' | 'sell'
  qty: number
  amount: number
}

export interface RealReport {
  status: '一致' | '有偏差' | '偏差较大' | '数据不足' | '无模拟' | '错误'
  message: string
  name?: string
  ref_name?: string
  warnings?: string[]
  fills?: number
  real_return?: number
  sim_return?: number
  gap?: number
  gap_annual?: number
  tracking_error?: number
  correlation?: number | null
  days?: number
  compare_start?: string
  end?: string
  nav_last?: number
  fees_real?: number
  curve?: { dates: string[]; real: number[]; sim: number[] }
  execution?: {
    matched: number
    real_trades: number
    sim_trades: number
    avg_dev_bps: number | null
    price_cost: number
    avg_lag: number | null
    fees_sim: number
    pairs: ExecutionPair[]
    missed: ExecutionTrade[]
    missed_count: number
    extra: ExecutionTrade[]
    extra_count: number
  }
  holdings?: {
    overlap: number
    real_exposure: number
    sim_exposure: number
    rows: { symbol: string; name: string; real: number; sim: number }[]
  }
}

export const realApi = {
  list: async () => (await http.get<RealAccount[]>('/api/quant/real-accounts')).data,
  create: async (body: RealAccountIn) => (await http.post<RealAccount>('/api/quant/real-accounts', body)).data,
  update: async (id: number, body: RealAccountIn) =>
    (await http.patch<RealAccount>(`/api/quant/real-accounts/${id}`, body)).data,
  remove: async (id: number) => (await http.delete(`/api/quant/real-accounts/${id}`)).data,
  fills: async (id: number) => (await http.get<RealFill[]>(`/api/quant/real-accounts/${id}/fills`)).data,
  addFill: async (id: number, body: RealFillIn) => (await http.post(`/api/quant/real-accounts/${id}/fills`, body)).data,
  removeFill: async (id: number, fid: number) =>
    (await http.delete(`/api/quant/real-accounts/${id}/fills/${fid}`)).data,
  importFills: async (id: number, contentBase64: string, commit: boolean) =>
    (
      await http.post<ImportResult>(`/api/quant/real-accounts/${id}/import`, {
        content_base64: contentBase64,
        commit,
      })
    ).data,
  report: async (id: number) =>
    (await http.get<RealReport>(`/api/quant/real-accounts/${id}/report`, { timeout: 300000 })).data,
}

// ---------------- 交易成本与数据质量 ----------------

export interface TradeCosts {
  commission_rate: number
  commission_min: number
  dividend_tax: boolean
}

export interface CostsStatus {
  costs: TradeCosts
  describe: string
  /** 所有在用策略的回测是否已按当前设置重新计算 */
  up_to_date: boolean
  task: TaskStatus | null
  synced_tracks?: number
}

export type IssueLevel = 'error' | 'warn' | 'info'

export interface DataIssue {
  key: string
  symbol: string
  name: string
  date: string
  kind: 'jump' | 'ohlc' | 'factor' | 'gap' | 'stale'
  level: IssueLevel
  detail: string
  ignored: boolean
}

export interface DataQuality {
  checked_at?: string
  universes?: Record<
    string,
    {
      symbols?: number
      days?: number
      start?: string
      end?: string
      counts?: Record<IssueLevel, number>
      total?: number
      issues?: DataIssue[]
      error?: string
    }
  >
  task: TaskStatus | null
  reevaluate_task: TaskStatus | null
}

export const costsApi = {
  get: async () => (await http.get<CostsStatus>('/api/quant/costs')).data,
  save: async (body: TradeCosts) => (await http.put<CostsStatus>('/api/quant/costs', body)).data,
  reevaluate: async () => (await http.post('/api/quant/reevaluate')).data,
}

export const qualityApi = {
  get: async () => (await http.get<DataQuality>('/api/quant/data-quality')).data,
  scan: async () => (await http.post('/api/quant/data-quality/scan')).data,
  ignore: async (key: string, ignore: boolean) =>
    (await http.post('/api/quant/data-quality/ignore', { key, ignore })).data,
  repair: async (symbol: string) =>
    (await http.post<{ message: string }>('/api/quant/data-quality/repair', { symbol }, { timeout: 180000 })).data,
}

// ---------------- AI 交易复核（规则 + AI 复核，两个账户并行） ----------------

export interface ReviewItem {
  symbol: string
  name: string
  decision: 'approve' | 'veto'
  risk: number | null
  reason: string
  date: string
  fill_date: string | null
  /** 执行日开盘至今的收益与相对沪深300 的超额 */
  after?: number
  after_excess?: number
}

export interface ReviewReport {
  enabled: boolean
  settings: { enabled?: boolean; account_id?: number; provider_id?: number; model?: string | null; since?: string }
  curve?: { date: string; rule: number; ai: number }[]
  rule_return?: number
  ai_return?: number
  since?: string
  reviews?: ReviewItem[]
  summary?: {
    reviewed: number
    vetoed: number
    veto_after_excess: number | null
    approve_after_excess: number | null
    cost: number
    /** AI 复核时调用技能（如查询公告全文）的次数 */
    skill_calls: number
  }
}

/** AI 复核有效性评估（预注册 docs/prereg/2026-10-08-ai-review.md）：评估时点之前只有进度 */
export interface ReviewEvaluation {
  applies: boolean
  start?: string
  as_of?: string
  /** 自第 0 个交易日以来的交易日数 */
  elapsed?: number
  looks?: { day: number; date: string; estimated: boolean }[]
  next_look?: number
  events?: number
  vetoes?: number
  result?: {
    look: number
    look_date: string
    events: number
    vetoes: number
    approves: number
    d: number | null
    p_upper: number | null
    p_lower: number | null
    account: { delta: number | null; ai?: number; rule?: number }
    verdict: string
  }
}

export const reviewApi = {
  get: async (pid: number) =>
    (await http.get<ReviewReport>(`/api/quant/portfolios/${pid}/review`, { timeout: 120000 })).data,
  evaluation: async (pid: number) =>
    (await http.get<ReviewEvaluation>(`/api/quant/portfolios/${pid}/review/evaluation`, { timeout: 120000 })).data,
  enable: async (pid: number, provider_id: number, model?: string | null) =>
    (await http.post(`/api/quant/portfolios/${pid}/review`, { provider_id, model: model || null })).data,
  disable: async (pid: number) => (await http.delete(`/api/quant/portfolios/${pid}/review`)).data,
}
/** 自选倾斜账户：规则账户的决策按自选权重调整（只调整已选中的股票） */
export interface TiltReport {
  enabled: boolean
  settings: { enabled?: boolean; account_id?: number; since?: string }
  weights: { symbol: string; name: string; weight: number }[]
  curve?: { date: string; rule: number; tilt: number }[]
  rule_return?: number
  tilt_return?: number
  since?: string
  last_changes?: { date: string; changes: { symbol: string; weight: number; rule: number; tilted: number }[] }
}

export const tiltApi = {
  get: async (pid: number) =>
    (await http.get<TiltReport>(`/api/quant/portfolios/${pid}/tilt`, { timeout: 120000 })).data,
  enable: async (pid: number) => (await http.post(`/api/quant/portfolios/${pid}/tilt`)).data,
  disable: async (pid: number) => (await http.delete(`/api/quant/portfolios/${pid}/tilt`)).data,
}
