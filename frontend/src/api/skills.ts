import { http } from './http'

export type SkillStatus = 'pending' | 'installing' | 'installed' | 'failed' | 'rejected' | 'removed'

export interface SkillFile {
  path: string
  size: number
  kind: 'doc' | 'script' | 'binary' | 'other'
  sha256: string
}

export interface Skill {
  id: number
  name: string
  display_name: string
  description: string
  version: string
  source_type: 'github' | 'upload' | 'market'
  source_url: string
  status: SkillStatus
  enabled: boolean
  allow_domains: string[]
  commands: string[]
  user_only: string[]
  setup_command: string
  package_sha256: string
  request_id: number | null
  approved_at: string | null
  created_at: string
  warnings: string[]
  summary: { files: number; scripts: number; binaries: number; domains: number }
  manifest?: {
    files: SkillFile[]
    scripts: string[]
    binaries: string[]
    domains: string[]
    meta: Record<string, unknown>
    sha256: string
  }
  install_log?: string
}

export interface SandboxStatus {
  ready: boolean
  docker: boolean
  image?: boolean
  network?: boolean
  proxy?: boolean
  message: string
}

export interface SkillCall {
  id: number
  caller: string
  context: string
  argv: string[]
  exit_code: number | null
  seconds: number
  output: string
  blocked: string[]
  created_at: string
}

export interface RunResult {
  exit_code: number | null
  output: string
  seconds: number
  blocked: string[]
  timed_out: boolean
}

export interface SkillPolicy {
  allow_domains?: string[]
  commands?: string[]
  user_only?: string[]
  setup_command?: string
  enabled?: boolean
}

export const skillsApi = {
  list: async () =>
    (
      await http.get<{
        skills: Skill[]
        sandbox: SandboxStatus
        sandbox_task: { running: boolean; message: string; ok?: boolean } | null
      }>('/api/skills', { timeout: 60000 })
    ).data,
  setupSandbox: async () => (await http.post('/api/skills/sandbox/setup')).data,
  stageGithub: async (url: string) =>
    (await http.post<Skill>('/api/skills/stage', { source: 'github', url }, { timeout: 180000 })).data,
  stageUpload: async (filename: string, content_base64: string) =>
    (await http.post<Skill>('/api/skills/stage', { source: 'upload', filename, content_base64 }, { timeout: 120000 }))
      .data,
  get: async (id: number) => (await http.get<Skill>(`/api/skills/${id}`)).data,
  file: async (id: number, path: string) =>
    (await http.get<{ path: string; content: string }>(`/api/skills/${id}/file`, { params: { path } })).data,
  update: async (id: number, body: SkillPolicy) => (await http.patch<Skill>(`/api/skills/${id}`, body)).data,
  approve: async (id: number) => (await http.post<Skill>(`/api/skills/${id}/approve`)).data,
  reject: async (id: number) => (await http.post<Skill>(`/api/skills/${id}/reject`)).data,
  remove: async (id: number) => (await http.delete(`/api/skills/${id}`)).data,
  run: async (id: number, argv: string[]) =>
    (await http.post<RunResult>(`/api/skills/${id}/run`, { argv }, { timeout: 330000 })).data,
  /** 关闭技能终端：结束该技能的终端会话容器 */
  endSession: async (id: number) => (await http.delete(`/api/skills/${id}/session`)).data,
  calls: async (id: number) => (await http.get<SkillCall[]>(`/api/skills/${id}/calls`)).data,
}

/** 把一行命令拆成参数列表（支持引号），不经过 shell */
export function splitArgs(line: string): string[] {
  const out: string[] = []
  const re = /"([^"]*)"|'([^']*)'|(\S+)/g
  let m: RegExpExecArray | null
  while ((m = re.exec(line))) out.push(m[1] ?? m[2] ?? m[3])
  return out
}

// ---------------- 技能市场（第 3 步） ----------------

export interface SkillMarket {
  id: number
  name: string
  kind: 'github' | 'skillsmp'
  url: string
  enabled: boolean
  skills: number
  indexed_at: string | null
  last_error: string
}

export interface MarketResult {
  market_id: number
  market: string
  name: string
  description: string
  url: string
  author?: string
  stars?: number
  language?: string
  staged: boolean
}

export const marketsApi = {
  list: async () => (await http.get<SkillMarket[]>('/api/skills/markets')).data,
  add: async (body: { name: string; kind: string; url: string; enabled: boolean }) =>
    (await http.post<SkillMarket>('/api/skills/markets', body)).data,
  update: async (id: number, body: { name: string; kind: string; url: string; enabled: boolean }) =>
    (await http.patch<SkillMarket>(`/api/skills/markets/${id}`, body)).data,
  remove: async (id: number) => (await http.delete(`/api/skills/markets/${id}`)).data,
  refresh: async (id: number) =>
    (await http.post<SkillMarket>(`/api/skills/markets/${id}/refresh`, null, { timeout: 180000 })).data,
  search: async (q: string, market_id?: number | null) =>
    (
      await http.get<MarketResult[]>('/api/skills/market-search', {
        params: { q, ...(market_id ? { market_id } : {}) },
        timeout: 180000,
      })
    ).data,
  stage: async (url: string, market_id: number) =>
    (await http.post<Skill>('/api/skills/markets/stage', { url, market_id }, { timeout: 180000 })).data,
}

// ---------------- AI 安装申请（第 4 步） ----------------

export interface SkillRequest {
  id: number
  requester: string
  reason: string
  query: string
  source_url: string
  market_id: number | null
  skill_id: number | null
  status: 'pending' | 'approved' | 'rejected' | 'failed'
  skill_name: string | null
  created_at: string
  decided_at: string | null
}

export const requestsApi = {
  list: async () =>
    (await http.get<{ settings: { ai_requests: boolean }; requests: SkillRequest[] }>('/api/skills/requests')).data,
  setAiRequests: async (ai_requests: boolean) => (await http.put('/api/skills/settings', { ai_requests })).data,
  reject: async (id: number) => (await http.post<SkillRequest>(`/api/skills/requests/${id}/reject`)).data,
}
