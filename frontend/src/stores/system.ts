import { defineStore } from 'pinia'
import { ref } from 'vue'
import { errorMessage } from '@/api/http'
import { fetchHealth, type HealthResponse } from '@/api/system'

/** 后端健康状态，供顶栏状态指示与系统页共用 */
export const useSystemStore = defineStore('system', () => {
  const health = ref<HealthResponse | null>(null)
  const error = ref<string | null>(null)
  const loading = ref(false)
  let timer: ReturnType<typeof setInterval> | undefined

  async function refresh() {
    loading.value = true
    try {
      health.value = await fetchHealth()
      error.value = null
    } catch (err) {
      health.value = null
      error.value = errorMessage(err)
    } finally {
      loading.value = false
    }
  }

  function startPolling(intervalMs = 30000) {
    stopPolling()
    void refresh()
    timer = setInterval(refresh, intervalMs)
  }

  function stopPolling() {
    if (timer) clearInterval(timer)
    timer = undefined
  }

  return { health, error, loading, refresh, startPolling, stopPolling }
})
