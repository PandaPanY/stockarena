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

/** 运行状况（运行保障，app/ops）：停机记录、定时任务最近结果、完整性检查、备份与磁盘、模型与推送 */
export interface OpsStatus {
  downtime: {
    from: string
    to: string
    minutes: number
    missed: { id: string; name: string; at: string }[]
  } | null
  jobs: {
    id: string
    name: string
    next_run_time: string | null
    status?: 'ok' | 'error' | 'missed' | null
    started_at?: string | null
    finished_at?: string | null
    seconds?: number | null
    error?: string | null
  }[]
  integrity: { day: string | null; checked_at: string; ok: boolean; problems: string[] } | null
  backup: {
    last?: { at: string; file: string; size: number; seconds: number; extra: string | null; extra_error?: string }
    last_error?: string | null
    last_error_at?: string
    files: { name: string; size: number }[]
    keep: number
    extra_dir: string | null
  }
  disk: { path: string; total: number; free: number; free_share: number }
  heartbeat: {
    host: string | null
    every_min: number
    at?: string
    ok?: boolean
    error?: string | null
    ok_at?: string
  }
  models: { id: number; name: string; fails?: number; last_error?: string; last_ok_at?: string; down_since?: string }[]
  failed_sends: { title: string; error: string; at: string }[]
}

export const opsApi = {
  status: async () => (await http.get<OpsStatus>('/api/system/ops')).data,
  backup: async () => (await http.post('/api/system/ops/backup', null, { timeout: 600000 })).data,
  check: async () =>
    (await http.post<NonNullable<OpsStatus['integrity']>>('/api/system/ops/check', null, { timeout: 120000 })).data,
}
