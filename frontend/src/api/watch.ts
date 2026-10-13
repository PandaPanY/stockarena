import { http } from './http'

export interface WatchEvent {
  id: number
  item_id: number
  symbol: string
  name: string
  date: string
  /** 1 一般关注 / 2 重要 / 3 重大 */
  level: number
  /** good 利好 / bad 利空 / neutral 中性 */
  direction: string
  title: string
  summary: string
  impact: string
  sources: { kind: string; date: string; title: string; url: string }[]
  model: string
  notified: boolean
  read: boolean
  created_at: string
}

export interface WatchItem {
  id: number
  symbol: string
  name: string
  market: string
  weight: number
  note: string
  monitor: boolean
  last_check_at: string | null
  last_result: {
    at?: string
    reason?: string
    new?: number
    ai?: boolean
    events?: number
    error?: string
    seconds?: number
    skill_calls?: number
    errors?: string[]
  }
  quote?: { price: number | null; change_pct: number | null; time: string | null }
  last_event?: WatchEvent | null
  unread?: number
  /** 组合最近一次调仓中选中它的成员策略 */
  picks?: string[]
  /** 持有它的跟踪账户 */
  held?: string[]
  events?: WatchEvent[]
}

export interface WatchSettings {
  enabled: boolean
  provider_id: number | null
  model: string | null
  /** 达到这个级别才推送微信 */
  push_level: number
}

export interface WatchTask {
  running?: boolean
  started_at?: string
  reason?: string
  result?: { checked: number; skipped: number; events: number; errors: string[] }
  error?: string
}

export interface WatchOverview {
  items: WatchItem[]
  settings: WatchSettings
  task: WatchTask
  limits: { max_items: number; weight_min: number; weight_max: number }
}

export const watchApi = {
  overview: async () => (await http.get<WatchOverview>('/api/watch', { timeout: 60000 })).data,
  add: async (symbol: string, weight = 1, note = '') =>
    (await http.post<WatchItem>('/api/watch', { symbol, weight, note }, { timeout: 60000 })).data,
  update: async (id: number, body: Partial<Pick<WatchItem, 'weight' | 'note' | 'monitor'>>) =>
    (await http.patch<WatchItem>(`/api/watch/${id}`, body)).data,
  remove: async (id: number) => (await http.delete(`/api/watch/${id}`)).data,
  bySymbol: async (symbol: string) =>
    (await http.get<WatchItem | null>(`/api/watch/by-symbol/${encodeURIComponent(symbol)}`)).data,
  events: async (params: { item_id?: number; limit?: number } = {}) =>
    (await http.get<WatchEvent[]>('/api/watch/events', { params })).data,
  markRead: async (ids?: number[]) => (await http.post('/api/watch/events/read', { ids: ids ?? null })).data,
  check: async (item_ids?: number[]) =>
    (await http.post<WatchTask>('/api/watch/check', { item_ids: item_ids ?? null })).data,
  checkStatus: async () => (await http.get<WatchTask>('/api/watch/check')).data,
  saveSettings: async (body: Partial<WatchSettings>) =>
    (await http.put<WatchSettings>('/api/watch/settings', body)).data,
}

export const LEVEL_TEXT: Record<number, string> = { 1: '一般关注', 2: '重要', 3: '重大' }
export const LEVEL_TYPE: Record<number, 'info' | 'warning' | 'danger'> = { 1: 'info', 2: 'warning', 3: 'danger' }
export const DIRECTION_TEXT: Record<string, string> = { good: '利好', bad: '利空', neutral: '中性' }
