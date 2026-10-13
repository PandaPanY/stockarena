import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { authApi, type AuthStatus } from '@/api/auth'

/** 登录状态：路由守卫与顶栏共用；接口返回 401 时标记为未登录 */
export const useAuthStore = defineStore('auth', () => {
  const status = ref<AuthStatus | null>(null)
  /** 管理员（未开启访问保护时视为管理员）：公共策略的修改、数据维护、技能、用户管理 */
  const isAdmin = computed(() => !status.value?.enabled || status.value?.user?.role === 'admin')
  const userId = computed(() => status.value?.user?.id ?? null)

  async function load(force = false): Promise<AuthStatus> {
    if (!status.value || force) status.value = await authApi.status()
    return status.value
  }

  function markLoggedOut(configured = true) {
    if (status.value) status.value = { ...status.value, configured, authenticated: false, user: null }
  }

  return { status, isAdmin, userId, load, markLoggedOut }
})
