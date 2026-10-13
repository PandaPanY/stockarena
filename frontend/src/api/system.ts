import { http } from './http'

export interface DatabaseStatus {
  ok: boolean
  dialect: string
  revision: string | null
  error: string | null
}

export interface MarketClock {
  market: 'CN' | 'HK' | 'US'
  timezone: string
  local_time: string
}

export interface HealthResponse {
  status: 'ok' | 'degraded'
  app: string
  version: string
  env: string
  time_utc: string
  database: DatabaseStatus
  scheduler_running: boolean
  markets: MarketClock[]
}

export interface JobInfo {
  id: string
  name: string
  next_run_time: string | null
  trigger: string
}

export async function fetchHealth(): Promise<HealthResponse> {
  return (await http.get<HealthResponse>('/api/system/health')).data
}

export async function fetchJobs(): Promise<JobInfo[]> {
  return (await http.get<JobInfo[]>('/api/system/jobs')).data
}
