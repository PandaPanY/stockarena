import { http } from './http'
import type { KBar } from './arena'

export interface StockHit {
  symbol: string
  name: string
  market: string
  /** stock / etf / lof / index */
  type: string
}

export interface StockQuote {
  name: string
  price: number
  prev_close: number
  change_pct: number | null
  open: number | null
  high: number | null
  low: number | null
  volume: number | null
  /** 行情时间（交易所当地时间，格式因市场而异） */
  time: string
}

export interface StockDetail {
  symbol: string
  name: string
  market: string
  type: string
  board: string
  currency: string
  industry: string | null
  is_st: boolean
  lot_size: number
  /** 本次按需补齐日线的结果 */
  update: string
  quote: StockQuote | null
  last: { date: string; close: number; change_pct: number | null } | null
  valuation: { pe_ttm?: number | null; pb?: number | null; market_cap?: number | null; dividend_yield?: number | null }
  fundamentals: (Record<string, number | null | string> & { period: string; available_from: string })[]
  relations: {
    holdings: { account_id: number; label: string; qty: number; avg_cost: number; opened: string | null }[]
    picks: { portfolio: string; date: string; members: string[]; target: number | null }[]
    reviews: { portfolio: string; date: string; decision: string; risk: number | null; reason: string }[]
  }
}

export interface FactorRanks {
  universe?: string
  date?: string
  peers?: number
  groups?: {
    title: string
    items: { name: string; title: string; description: string; value: number | null; pct: number }[]
  }[]
}

export interface StockNews {
  announcements: { date: string; title: string; url: string }[]
  news: { time: string; title: string; source: string; url: string }[]
  errors: string[]
}

const enc = encodeURIComponent

export const stocksApi = {
  search: async (q: string) => (await http.get<StockHit[]>('/api/stocks/search', { params: { q } })).data,
  detail: async (symbol: string) =>
    (await http.get<StockDetail>(`/api/stocks/${enc(symbol)}`, { timeout: 90000 })).data,
  bars: async (symbol: string, freq = 'd', adjust = 'qfq') =>
    (await http.get<KBar[]>(`/api/stocks/${enc(symbol)}/bars`, { params: { freq, adjust }, timeout: 60000 })).data,
  quote: async (symbol: string) =>
    (await http.get<StockQuote | Record<string, never>>(`/api/stocks/${enc(symbol)}/quote`)).data,
  news: async (symbol: string) =>
    (await http.get<StockNews>(`/api/stocks/${enc(symbol)}/news`, { timeout: 60000 })).data,
  ranks: async (symbol: string) =>
    (await http.get<FactorRanks>(`/api/stocks/${enc(symbol)}/ranks`, { timeout: 120000 })).data,
}

/** 市场与类型的显示名 */
export const MARKET_NAMES: Record<string, string> = { CN: 'A股', HK: '港股', US: '美股' }
export const TYPE_NAMES: Record<string, string> = { stock: '股票', etf: 'ETF', lof: 'LOF', index: '指数' }
