import { http } from './http'

export interface HomeIndex {
  symbol: string
  name: string
  price: number | null
  change_pct: number | null
  time: string | null
  amount: number | null
  /** 最近约 60 个交易日的收盘价 */
  spark: number[]
}

export interface HomeMarket {
  market: string
  name: string
  indices: HomeIndex[]
  breadth?: {
    up: number | null
    down: number | null
    flat: number | null
    limit_up: number | null
    limit_down: number | null
    suspended: number | null
    activity: string
    time: string
  } | null
  /** 沪深两市成交额（元） */
  turnover?: number | null
}

export interface HomeStar {
  symbol: string
  name: string
  market: string
  price: number | null
  change_pct: number | null
  time: string | null
}

export interface HomeAccount {
  account_id: number
  label: string
  benchmark: string | null
  benchmark_name: string
  date?: string
  nav?: number
  since?: string
  day?: number | null
  bench_day?: number | null
  total?: number | null
  bench_total?: number | null
  /** 盘中按持仓与实时价估算的当天收益（收盘后以净值为准） */
  intraday?: { est: number | null; bench: number | null; time: string | null }
}

export interface HomePortfolio {
  kind: 'portfolio' | 'strategy'
  id: number
  name: string
  accounts: HomeAccount[]
}

export interface HomeOverview {
  markets: HomeMarket[]
  stars: HomeStar[]
  portfolios: HomePortfolio[]
  today: string
}

export const homeApi = {
  overview: async () => (await http.get<HomeOverview>('/api/home', { timeout: 60000 })).data,
  setStars: async (symbols: string[]) => (await http.put<{ symbols: string[] }>('/api/home/stars', { symbols })).data,
}
