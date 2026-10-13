import axios, { AxiosError } from 'axios'

/** 统一的后端请求实例：开发环境经 Vite 代理转发到后端 */
export const http = axios.create({
  baseURL: '/',
  timeout: 15000,
})

type UnauthorizedHandler = (code: 'login_required' | 'setup_required') => void
let onUnauthorized: UnauthorizedHandler | null = null

/** 登录失效（接口返回 401 且带 code）时的处理，由路由注册：跳转到登录页 */
export function setUnauthorizedHandler(handler: UnauthorizedHandler) {
  onUnauthorized = handler
}

http.interceptors.response.use(undefined, (err) => {
  if (err instanceof AxiosError && err.response?.status === 401) {
    const code = (err.response.data as { code?: string } | undefined)?.code
    if (code === 'login_required' || code === 'setup_required') onUnauthorized?.(code)
  }
  return Promise.reject(err)
})

/** 把后端或网络错误转换为可直接展示的中文信息 */
export function errorMessage(err: unknown): string {
  if (err instanceof AxiosError) {
    if (err.response) {
      const detail = (err.response.data as { detail?: unknown } | undefined)?.detail
      const text = typeof detail === 'string' ? detail : JSON.stringify(detail ?? err.response.statusText)
      return `请求失败（${err.response.status}）：${text}`
    }
    if (err.code === 'ECONNABORTED') return '请求超时，请检查后端是否运行'
    return '无法连接后端服务，请确认后端已启动'
  }
  return err instanceof Error ? err.message : String(err)
}
