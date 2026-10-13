import { http } from './http'

export interface AuthUser {
  id: number
  name: string
  /** admin：管理员；user：普通用户 */
  role: 'admin' | 'user'
  disabled: boolean
  created_at: string | null
}

export interface AuthStatus {
  /** 服务端是否开启访问保护 */
  enabled: boolean
  /** 管理员是否已设置登录密码（没有时进入首次设置） */
  configured: boolean
  authenticated: boolean
  /** 是否开放注册（管理员在「用户管理」里设置） */
  registration_open: boolean
  /** 可选功能是否开启（技能默认关闭，见 docs/SKILLS.md） */
  features: { skills: boolean }
  /** 源代码地址（AGPL-3.0：页面底部提供源代码链接） */
  source_url?: string
  user: AuthUser | null
}

export const authApi = {
  status: async () => (await http.get<AuthStatus>('/api/auth/status')).data,
  /** 首次设置管理员密码：setup_code 在服务启动日志或数据目录下的 setup-code.txt 中 */
  setup: async (password: string, setup_code: string) =>
    (await http.post('/api/auth/setup', { password, setup_code })).data,
  login: async (username: string, password: string) =>
    (await http.post('/api/auth/login', { username, password })).data,
  register: async (username: string, password: string) =>
    (await http.post('/api/auth/register', { username, password })).data,
  logout: async () => (await http.post('/api/auth/logout')).data,
  logoutAll: async () => (await http.post('/api/auth/logout-all')).data,
  changePassword: async (old_password: string, new_password: string) =>
    (await http.post('/api/auth/password', { old_password, new_password })).data,
  // 用户管理（管理员）
  users: async () => (await http.get<AuthUser[]>('/api/auth/users')).data,
  patchUser: async (id: number, body: { disabled?: boolean }) =>
    (await http.patch<AuthUser>(`/api/auth/users/${id}`, body)).data,
  registration: async () => (await http.get<{ open: boolean }>('/api/auth/registration')).data,
  setRegistration: async (open: boolean) =>
    (await http.put<{ open: boolean }>('/api/auth/registration', { open })).data,
  resetPassword: async (id: number, new_password: string) =>
    (await http.post(`/api/auth/users/${id}/password`, { new_password })).data,
}
