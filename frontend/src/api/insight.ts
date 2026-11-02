import { http } from './http'

/** 正在实时跟踪的策略 / 组合 */
export interface FollowedRef {
  kind: 'strategy' | 'portfolio'
  id: number
  name: string
  account_id: number
  readiness: string | null
  health: string | null
}

/** AI 写的每周点评（weekly）或偏差分析（deviation） */
export interface AiNote {
  id: number
  kind: 'weekly' | 'deviation'
  ref_kind: 'strategy' | 'portfolio'
  ref_id: number
  ref_name: string
  account_id: number | null
  period_start: string | null
  period_end: string | null
  content: string
  status: 'ok' | 'error'
  error: string | null
  model: string | null
  tokens_in: number
  tokens_out: number
  created_at: string
  /** 交给模型的数据（只在单篇详情里返回） */
  material?: Record<string, unknown>
}

export interface InsightSettings {
  provider_id: number | null
  model: string | null
  weekly: boolean
  deviation: boolean
  /** 是否单独选了模型（否则沿用自选股监控的模型） */
  own_model: boolean
}

export interface InsightOverview {
  followed: FollowedRef[]
  settings: InsightSettings
  notes: AiNote[]
  running: boolean
}

export interface ChatTurn {
  role: 'user' | 'assistant'
  content: string
}

export interface AskResult {
  status: string
  answer: string
  error: string | null
  trace: { tool: string; args: Record<string, unknown> }[]
  model: string
  tokens_in: number
  tokens_out: number
  seconds: number
}

export const insightApi = {
  overview: async () => (await http.get<InsightOverview>('/api/insight')).data,
  note: async (id: number) => (await http.get<AiNote>(`/api/insight/notes/${id}`)).data,
  run: async (body: { kind: 'weekly' | 'deviation'; ref_kind?: string; ref_id?: number }) =>
    (await http.post<{ started: boolean; message: string }>('/api/insight/notes/run', body)).data,
  /** 模型可能要查好几次数据，本地模型一两分钟 */
  ask: async (question: string, history: ChatTurn[]) =>
    (await http.post<AskResult>('/api/insight/ask', { question, history }, { timeout: 300000 })).data,
  saveSettings: async (body: Partial<InsightSettings> & { use_default_model?: boolean }) =>
    (await http.put<InsightSettings>('/api/insight/settings', body)).data,
}

/** 问答助手调用的查询工具的中文名 */
export const TOOL_TEXT: Record<string, string> = {
  list_followed: '查看跟踪列表',
  get_performance: '查询收益',
  get_holdings: '查询持仓',
  get_orders: '查询订单',
  get_contributions: '查询收益贡献',
  explain_symbol: '查询入选原因',
  get_members: '查询成员与健康度',
  get_notes: '查看 AI 点评',
}
