import { http } from './http'

export type NotifyKind = 'serverchan' | 'pushplus' | 'wecom_bot'
export type NotifyEvent = 'orders' | 'errors' | 'health' | 'skills' | 'watch'

export interface NotifyChannel {
  id: number
  name: string
  kind: NotifyKind
  kind_name: string
  secret_masked: string
  enabled: boolean
  events: NotifyEvent[]
  last_status: string
  last_error: string
  last_sent_at: string | null
}

export interface NotifyChannelIn {
  name: string
  kind: NotifyKind
  /** 新建时必填；修改时留空表示不变 */
  secret?: string
  enabled: boolean
  events: NotifyEvent[]
}

export interface NotificationItem {
  id: number
  channel_id: number
  event: string
  title: string
  body: string
  status: 'ok' | 'failed'
  error: string
  created_at: string
}

export const notifyApi = {
  channels: async () => (await http.get<NotifyChannel[]>('/api/notify/channels')).data,
  create: async (body: NotifyChannelIn) => (await http.post<NotifyChannel>('/api/notify/channels', body)).data,
  update: async (id: number, body: NotifyChannelIn) =>
    (await http.patch<NotifyChannel>(`/api/notify/channels/${id}`, body)).data,
  remove: async (id: number) => (await http.delete(`/api/notify/channels/${id}`)).data,
  test: async (id: number) => (await http.post(`/api/notify/channels/${id}/test`, null, { timeout: 30000 })).data,
  check: async () =>
    (
      await http.post<{ results: { channel_id: number; status: string; title: string }[] }>('/api/notify/check', null, {
        timeout: 60000,
      })
    ).data,
  history: async () => (await http.get<NotificationItem[]>('/api/notify/history')).data,
}
