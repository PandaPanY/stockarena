import { http } from './http'

// ---------------- 模型供应商 ----------------
export type Protocol = 'openai_compatible' | 'anthropic' | 'gemini'

export interface Provider {
  id: number
  name: string
  protocol: Protocol
  base_url: string | null
  default_model: string
  timeout_s: number
  price_in_per_m: string
  price_out_per_m: string
  supports_json_mode: boolean
  knowledge_cutoff: string | null
  has_api_key: boolean
  api_key_masked: string
  /** API Key 无法用当前主密钥解密时的说明（需要重新填写） */
  api_key_error?: string | null
}

export interface ProviderInput {
  name: string
  protocol: Protocol
  base_url: string | null
  api_key: string | null
  default_model: string
  timeout_s: number
  price_in_per_m: string
  price_out_per_m: string
  supports_json_mode: boolean
  knowledge_cutoff: string | null
}

export interface ProviderPreset {
  name: string
  protocol: Protocol
  base_url: string
  default_model: string
  price_in_per_m?: string
  price_out_per_m?: string
}

export interface ProviderTestResult {
  ok: boolean
  error?: string
  reply?: string
  model?: string
  latency_ms: number
  tokens_in?: number
  tokens_out?: number
  has_reasoning?: boolean
}

export const providersApi = {
  presets: async () => (await http.get<ProviderPreset[]>('/api/providers/presets')).data,
  list: async () => (await http.get<Provider[]>('/api/providers')).data,
  create: async (body: ProviderInput) => (await http.post<Provider>('/api/providers', body)).data,
  update: async (id: number, body: ProviderInput) => (await http.put<Provider>(`/api/providers/${id}`, body)).data,
  remove: async (id: number) => (await http.delete(`/api/providers/${id}`)).data,
  test: async (id: number, model?: string) =>
    (await http.post<ProviderTestResult>(`/api/providers/${id}/test`, { model }, { timeout: 180000 })).data,
  models: async (id: number) =>
    (await http.get<{ models: string[]; error?: string }>(`/api/providers/${id}/models`, { timeout: 30000 })).data,
}

// ---------------- 选手 ----------------
export interface Agent {
  id: number
  name: string
  kind: 'llm' | 'baseline'
  baseline_type: string | null
  provider_id: number | null
  provider_name: string | null
  model: string | null
  params: Record<string, unknown>
  prompt_template_id: number | null
  prompt_template: string | null
  persona: string
  memory_rounds: number
  description: string
  archived: boolean
  track_count: number
  reflect_enabled: boolean
  reflect_every: number
  initial_strategy: string
}

export interface AgentInput {
  name: string
  provider_id: number
  model: string | null
  params: Record<string, unknown>
  prompt_template_id: number | null
  persona: string
  memory_rounds: number
  description: string
  reflect_enabled: boolean
  reflect_every: number
  initial_strategy: string
}

export const agentsApi = {
  list: async () => (await http.get<Agent[]>('/api/agents')).data,
  create: async (body: AgentInput) => (await http.post<Agent>('/api/agents', body)).data,
  update: async (id: number, body: AgentInput) => (await http.put<Agent>(`/api/agents/${id}`, body)).data,
  archive: async (id: number) => (await http.delete(`/api/agents/${id}`)).data,
}

// ---------------- 元数据 ----------------
export interface PromptTemplate {
  id: number
  name: string
  version: number
  title: string
  description: string
}

export interface RuleSet {
  id: number
  name: string
  title: string
  market: string
  description: string
  params: Record<string, string | number | boolean>
  param_sources: Record<string, 'system' | 'global' | 'ruleset'>
  param_descriptions: Record<string, string | null>
}

export interface Universe {
  name: string
  title: string
  market: string
  description: string
  benchmark: string | null
  benchmark_name: string | null
  index: string | null
  include: string[]
  member_count: number
  point_in_time: string | null
  /** 全时点股票池（含历史上被剔除、已退市的成分股） */
  history: boolean
  /** 快速体验股票池（当前成分回看历史，有幸存者偏差，只用于试用） */
  quick?: boolean
}

export interface MarketInfo {
  code: 'CN' | 'HK' | 'US'
  name: string
  currency: string
  timezone: string
  default_capital: number
  run_times: string[]
}

export interface Persona {
  name: string
  title: string
  description: string
  persona: string
}

export const metaApi = {
  personas: async () => (await http.get<Persona[]>('/api/meta/personas')).data,
  markets: async () => (await http.get<MarketInfo[]>('/api/meta/markets')).data,
  templates: async () => (await http.get<PromptTemplate[]>('/api/meta/prompt-templates')).data,
  template: async (id: number) =>
    (await http.get<PromptTemplate & { system: string; user: string }>(`/api/meta/prompt-templates/${id}`)).data,
  rulesets: async () => (await http.get<RuleSet[]>('/api/meta/rulesets')).data,
  universes: async () => (await http.get<Universe[]>('/api/meta/universes')).data,
}

// ---------------- 赛道 ----------------
export type TrackStatus = 'draft' | 'running' | 'paused' | 'finished'

export interface Metrics {
  nav: number
  days: number
  reliable: boolean
  cumulative_return?: number | null
  annualized_return?: number | null
  volatility?: number | null
  max_drawdown?: number | null
  max_drawdown_days?: number
  sharpe?: number | null
  sortino?: number | null
  calmar?: number | null
  benchmark_return?: number | null
  excess_return?: number | null
  alpha?: number | null
  beta?: number | null
  information_ratio?: number | null
  trades: number
  closed_trades: number
  win_rate: number | null
  profit_factor: number | null
  turnover: number | null
  total_fees: number
  fee_ratio: number | null
  realized_pnl: number
  avg_holding_days?: number | null
  avg_leverage?: number | null
  max_leverage?: number | null
  max_weight?: number | null
  hhi?: number | null
  margin_calls?: number
  forced_liquidations?: number
  radar?: RadarScores
  ai: {
    llm: boolean
    rounds: number
    valid_rate?: number
    avg_latency_s?: number | null
    p95_latency_s?: number | null
    tokens_in?: number
    tokens_out?: number
    cost?: number
    reject_rate?: number | null
    cost_benefit?: number | null
    consistency_rate?: number | null
    consistency_checked?: number
  }
}

/** 五维雷达评分（0～100），null 表示样本不足无法评分 */
export interface RadarScores {
  return: number | null
  risk: number | null
  stability: number | null
  efficiency: number | null
  cost: number | null
}

export interface LeaderboardRow extends Metrics {
  rank: number
  account_id: number
  agent_id: number
  agent_name: string
  kind: 'llm' | 'baseline'
  baseline_type: string | null
  model: string | null
  reflect_enabled: boolean
  strategy_version: number | null
}

export interface TrackAccount {
  id: number
  agent_id: number
  agent_name: string
  agent_kind: 'llm' | 'baseline'
}

export interface Track {
  id: number
  name: string
  market: string
  currency: string
  universe_name: string
  symbol_count: number
  benchmark_symbol: string
  benchmark_name: string
  rule_set_id: number
  rule_set_title: string | null
  initial_cash: string
  output_mode: 'targets' | 'orders'
  decision_timeout_s: number
  status: TrackStatus
  status_label: string
  mode: 'live' | 'replay'
  replay_until: string | null
  replay_pending: boolean
  frequency: 'daily' | 'intraday'
  interval_min: number
  event_move_pct: number | null
  event_max_per_day: number
  decision_mode: 'snapshot' | 'tools'
  max_tool_calls: number
  data_options: DataOptions
  anonymize: boolean
  lookahead: LookaheadRisk[]
  start_date: string | null
  end_date: string | null
  last_settled_date: string | null
  description: string
  account_count: number
  busy: boolean
  last_run: { running: boolean; message: string; started_at: string; finished_at?: string } | null
  created_at: string
  accounts?: TrackAccount[]
  rule_params?: Record<string, string | number | boolean>
  symbols?: string[]
  leader?: LeaderboardRow | null
  ranking?: { name: string; kind: string; cumulative_return: number | null }[]
}

export interface TrackInput {
  name: string
  universe_name: string
  rule_set_id: number
  initial_cash: string
  output_mode: 'targets' | 'orders'
  decision_timeout_s: number
  end_date: string | null
  description: string
  agent_ids: number[]
  mode: 'live' | 'replay'
  replay_start: string | null
  frequency: 'daily' | 'intraday'
  interval_min: number
  event_move_pct: string | null
  event_max_per_day: number
  decision_mode: 'snapshot' | 'tools'
  max_tool_calls: number
  data_options: DataOptions
  anonymize: boolean
}

/** 历史回放的前视偏差风险：high 模型知识截止晚于回放开始；unknown 未填写知识截止日期；low 早于回放开始 */
export interface LookaheadRisk {
  account_id: number
  agent_name: string
  model: string | null
  cutoff: string | null
  level: 'high' | 'unknown' | 'low'
  replay_start: string
  replay_until: string | null
  anonymized: boolean
}

/** 快照模式附带的数据 */
export interface DataOptions {
  news?: boolean
  fundamentals?: boolean
}

/** [日期, 归一化净值, 原始值] */
export type NavPoint = [string, number, number]

export interface NavSeries {
  benchmark: { symbol: string; name: string; points: NavPoint[] }
  accounts: { account_id: number; name: string; kind: string; points: NavPoint[] }[]
}

export interface Round {
  id: number
  data_date: string
  fill_date: string | null
  trigger: string
  status: string
  note: string
  snapshot_hash: string | null
  snapshot_at: string | null
  started_at: string
  finished_at: string | null
}

export interface EventItem {
  id: number
  track_id?: number | null
  trade_date: string | null
  type: string
  level: 'info' | 'warning' | 'error'
  message: string
  account_id?: number | null
  created_at: string
}

export interface ConsistencyResult {
  running: boolean
  message: string | null
  accounts: {
    account_id: number
    agent_name: string
    judge_model: string
    checked: number
    rate: number | null
    inconsistent: number
  }[]
  items: {
    id: number
    decision_id: number
    account_id: number
    agent_name: string
    data_date: string
    judge_model: string
    status: string
    verdict: 'consistent' | 'partial' | 'inconsistent' | null
    score: number | null
    reason: string | null
    error: string | null
    analysis: string
  }[]
}

export const tracksApi = {
  consistency: async (id: number) => (await http.get<ConsistencyResult>(`/api/tracks/${id}/consistency`)).data,
  runConsistency: async (id: number, body: { provider_id: number; model: string | null; sample_size: number }) =>
    (await http.post<{ ok: boolean; message: string }>(`/api/tracks/${id}/consistency`, body)).data,
  clone: async (id: number) => (await http.post<Track>(`/api/tracks/${id}/clone`)).data,
  list: async () => (await http.get<Track[]>('/api/tracks')).data,
  get: async (id: number) => (await http.get<Track>(`/api/tracks/${id}`)).data,
  create: async (body: TrackInput) => (await http.post<Track>('/api/tracks', body)).data,
  remove: async (id: number) => (await http.delete(`/api/tracks/${id}`)).data,
  enroll: async (id: number, agentId: number) =>
    (await http.post<Track>(`/api/tracks/${id}/accounts`, { agent_id: agentId })).data,
  withdraw: async (id: number, accountId: number) =>
    (await http.delete<Track>(`/api/tracks/${id}/accounts/${accountId}`)).data,
  start: async (id: number) => (await http.post<Track>(`/api/tracks/${id}/start`, null, { timeout: 180000 })).data,
  action: async (id: number, action: 'pause' | 'resume' | 'stop') =>
    (await http.post<Track>(`/api/tracks/${id}/${action}`)).data,
  run: async (id: number, forceRound = false) =>
    (await http.post(`/api/tracks/${id}/run`, { force_round: forceRound })).data,
  leaderboard: async (id: number) => (await http.get<LeaderboardRow[]>(`/api/tracks/${id}/leaderboard`)).data,
  nav: async (id: number) => (await http.get<NavSeries>(`/api/tracks/${id}/nav`)).data,
  rounds: async (id: number) => (await http.get<Round[]>(`/api/tracks/${id}/rounds`)).data,
  snapshot: async (id: number, roundId: number) =>
    (
      await http.get<{ table: string | null; snapshot_hash: string; data_date: string }>(
        `/api/tracks/${id}/rounds/${roundId}/snapshot`,
      )
    ).data,
  events: async (id: number) => (await http.get<EventItem[]>(`/api/tracks/${id}/events`)).data,
}

// ---------------- 账户 ----------------
export interface AccountDetail {
  id: number
  track_id: number
  track_name: string
  currency: string
  initial_cash: string
  cash: string
  agent: {
    id: number
    name: string
    kind: string
    model: string | null
    baseline_type: string | null
    description: string
  }
  metrics: Metrics
  margin: MarginInfo | null
}

/** 做空 / 融资赛道的杠杆与保证金状态 */
export interface MarginInfo {
  allow_short: boolean
  allow_margin: boolean
  leverage_cap: number
  method: 'equity_ratio' | 'collateral_ratio'
  maintenance: number
  liquidation: number
  long_value: number
  short_value: number
  debt: number
  leverage: number | null
  ratio: number | null
  margin_call_since: string | null
}

export interface PositionItem {
  symbol: string
  name: string
  qty: number
  short: boolean
  type: string | null
  avg_cost: string
  close: string
  market_value: string
  pnl: string
  pnl_pct: number | null
  weight: number | null
  price_date: string | null
}

export interface OrderItem {
  id: number
  decision_id: number | null
  symbol: string
  side: Side
  qty: number
  filled_qty: number
  status: 'pending' | 'filled' | 'partially_filled' | 'rejected' | 'expired' | 'cancelled'
  reason: string
  note: string
  placed_date: string
  target_date: string | null
  order_type: 'market' | 'limit' | 'stop' | 'take_profit'
  limit_price: string | null
  stop_price: string | null
  tif: 'day' | 'gtc'
  expires_on: string | null
  oco_group: number | null
  attach_stop: string | null
  attach_take_profit: string | null
  active_from: string | null
  forced?: boolean
  fills?: FillItem[]
}

export interface FillItem {
  id: number
  order_id: number
  trade_date: string
  symbol: string
  side: Side
  qty: number
  price: string
  amount: string
  fees: Record<string, string>
  total_fee: string
  realized_pnl: string | null
  reason?: string
}

export interface NavRow {
  date: string
  nav: string
  cash: string
  market_value: string
  benchmark_close: string | null
}

export interface LedgerRow {
  id: number
  trade_date: string
  type: string
  amount: string
  balance_after: string
  symbol: string | null
  note: string
}

/** [日期, 开, 收, 低, 高, 量] */
export type KBar = [string, number, number, number, number, number]

/** buy 买入 / sell 卖出 / short 卖空 / cover 平空 */
export type Side = 'buy' | 'sell' | 'short' | 'cover'

export const accountsApi = {
  get: async (id: number) => (await http.get<AccountDetail>(`/api/accounts/${id}`)).data,
  positions: async (id: number) => (await http.get<PositionItem[]>(`/api/accounts/${id}/positions`)).data,
  orders: async (id: number) => (await http.get<OrderItem[]>(`/api/accounts/${id}/orders`)).data,
  fills: async (id: number) => (await http.get<FillItem[]>(`/api/accounts/${id}/fills`)).data,
  nav: async (id: number) => (await http.get<NavRow[]>(`/api/accounts/${id}/nav`)).data,
  ledger: async (id: number) => (await http.get<LedgerRow[]>(`/api/accounts/${id}/ledger`)).data,
  symbols: async (id: number) => (await http.get<string[]>(`/api/accounts/${id}/symbols`)).data,
  kline: async (id: number, symbol: string) =>
    (
      await http.get<{ symbol: string; name: string; bars: KBar[]; fills: FillItem[] }>(`/api/accounts/${id}/kline`, {
        params: { symbol },
      })
    ).data,
  strategy: async (id: number) => (await http.get<StrategyInfo>(`/api/accounts/${id}/strategy`)).data,
  editStrategy: async (id: number, content: string) =>
    (await http.put<StrategyVersion>(`/api/accounts/${id}/strategy`, { content })).data,
  rollbackStrategy: async (id: number, version: number) =>
    (await http.post<StrategyVersion>(`/api/accounts/${id}/strategy/rollback`, { version })).data,
  reflect: async (id: number) =>
    (await http.post<{ ok: boolean; message: string }>(`/api/accounts/${id}/reflect`)).data,
  reflection: async (id: number) => (await http.get<ReflectionDetail>(`/api/reflections/${id}`)).data,
}

// ---------------- 对比 ----------------
export interface CompareAccount {
  account_id: number
  agent_name: string
  kind: 'llm' | 'baseline'
  model: string | null
  track_id: number
  track_name: string
  currency: string
  initial_cash: number
  metrics: Metrics
  /** 运行模式与前视风险：实时模拟没有前视偏差；回放按模型知识截止日期判断 */
  mode: 'live' | 'replay'
  lookahead: 'high' | 'unknown' | 'low' | null
  composition: { stock_long: number; etf_long: number; short: number; cash: number }
  /** [日期, 净值 / 初始本金, 净值] */
  nav: [string, number, number][]
}

export interface RepeatStat {
  mean: number
  std: number
  min: number
  max: number
}

export interface CompareResult {
  accounts: CompareAccount[]
  correlation: (number | null)[][]
  holding_overlap: (number | null)[][]
  traded_overlap: (number | null)[][]
  /** 同一选手在多个赛道的重复实验统计 */
  repeats: {
    agent_id: number
    agent_name: string
    runs: number
    tracks: string[]
    cumulative_return?: RepeatStat
    excess_return?: RepeatStat
    max_drawdown?: RepeatStat
  }[]
}

export const compareApi = {
  get: async (ids: number[]) =>
    (await http.get<CompareResult>('/api/compare', { params: { account_ids: ids.join(',') } })).data,
}

// ---------------- 策略笔记与复盘 ----------------
export interface StrategyVersion {
  id: number
  version: number
  content: string
  source: 'initial' | 'reflection' | 'manual' | 'rollback'
  changes: string
  data_date: string | null
  reflection_id: number | null
  created_at: string
}

export interface ReflectionStats {
  days: number
  period_return: string
  bench_period_return: string
  period_excess: string
  total_return: string
  bench_total_return: string
  total_excess: string
  period_drawdown: string
  trades: number
  closed: number
  wins: number
  realized: string
  fees: string
  rejected: number
}

export interface ReflectionSummary {
  id: number
  data_date: string
  period_start: string | null
  trigger: 'schedule' | 'manual'
  status: 'ok' | 'invalid' | 'timeout' | 'error'
  model: string | null
  base_version: number | null
  new_version: number | null
  stats: Partial<ReflectionStats>
  review: string | null
  changes: string | null
  error: string | null
  latency_ms: number
  tokens_in: number
  tokens_out: number
  cost: string
  created_at: string
}

export interface ReflectionDetail extends ReflectionSummary {
  prompt_system: string | null
  prompt_user: string | null
  raw_response: string | null
  reasoning: string | null
  attempts: number
  template_version: number | null
}

export interface StrategyInfo {
  kind: 'llm' | 'baseline'
  reflect_enabled: boolean
  reflect_every: number
  max_chars: number
  reflecting: boolean
  last_message: string | null
  versions: StrategyVersion[]
  reflections: ReflectionSummary[]
}

// ---------------- 决策 ----------------
export interface DecisionSummary {
  id: number
  round_id: number
  data_date: string
  fill_date: string
  trigger: string
  account_id: number
  agent_name: string
  agent_kind: 'llm' | 'baseline'
  status: 'ok' | 'invalid' | 'timeout' | 'error' | 'baseline'
  model: string | null
  analysis: string | null
  error: string | null
  orders: number
  accepted: number
  filled: number
  rejected: number
  latency_ms: number
  tokens_in: number
  tokens_out: number
  cost: string
  strategy_version: number | null
  created_at: string
}

export interface DecisionDetail extends DecisionSummary {
  track_id: number
  track_name: string
  template: string | null
  prompt_system: string | null
  prompt_user: string | null
  raw_response: string | null
  reasoning: string | null
  parsed: Record<string, unknown> | null
  notes: string[]
  attempts: number
  snapshot_hash: string | null
  order_items: OrderItem[]
  tool_trace: ToolTraceItem[]
}

/** 工具模式下的一次工具调用 */
export interface ToolTraceItem {
  tool: string
  args: Record<string, unknown>
  result: string
  ms: number
}

export const decisionsApi = {
  list: async (params: { track_id?: number; account_id?: number; status?: string; limit?: number; offset?: number }) =>
    (await http.get<{ items: DecisionSummary[]; more: boolean }>('/api/decisions', { params })).data,
  get: async (id: number) => (await http.get<DecisionDetail>(`/api/decisions/${id}`)).data,
}

// ---------------- 系统 ----------------
export interface DataSourceStatus {
  name: string
  last_success_at: string | null
  last_failure_at: string | null
  last_error: string | null
  consecutive_failures: number
}

export const systemApi = {
  datasources: async () => (await http.get<DataSourceStatus[]>('/api/system/datasources')).data,
  events: async (limit = 50) => (await http.get<EventItem[]>('/api/system/events', { params: { limit } })).data,
}

// ---------------- 进化实验 ----------------
export interface ExperimentPhase {
  track_id: number
  days: number | null
  return: number | null
  benchmark: number | null
  excess: number | null
  max_drawdown: number | null
  trades: number | null
  reflections: number
  strategy_versions: number
  final_strategy: string
}

export interface Experiment {
  id: number
  name: string
  agent_id: number
  agent_name: string | null
  universe_name: string
  rule_set_id: number
  rule_set_title: string | null
  initial_cash: string
  output_mode: string
  train_start: string
  train_end: string
  valid_start: string
  valid_end: string
  generations: number
  reflect_every: number
  anonymize: boolean
  status: 'pending' | 'running' | 'done' | 'failed' | 'cancelled'
  progress: string
  results: { generation: number; strategy: string; train: ExperimentPhase | null; valid: ExperimentPhase }[]
  description: string
  created_at: string
}

export interface ExperimentInput {
  name: string
  agent_id: number
  universe_name: string
  rule_set_id: number
  initial_cash: string
  output_mode: string
  train_start: string
  train_end: string
  valid_start: string
  valid_end: string
  generations: number
  reflect_every: number
  anonymize: boolean
  description: string
}

export const experimentsApi = {
  list: async () => (await http.get<Experiment[]>('/api/experiments')).data,
  defaults: async () =>
    (
      await http.get<{ train_start: string; train_end: string; valid_start: string; valid_end: string }>(
        '/api/experiments/defaults',
      )
    ).data,
  create: async (body: ExperimentInput) => (await http.post<Experiment>('/api/experiments', body)).data,
  cancel: async (id: number) => (await http.post<Experiment>(`/api/experiments/${id}/cancel`)).data,
}
