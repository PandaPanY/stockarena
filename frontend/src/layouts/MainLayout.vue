<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import AccountMenu from '@/components/AccountMenu.vue'
import StockSearch from '@/components/StockSearch.vue'
import { navItems } from '@/router/nav'
import { useSystemStore } from '@/stores/system'
import { useAuthStore } from '@/stores/auth'

const route = useRoute()
const system = useSystemStore()
const auth = useAuthStore()
/** 管理员专属的菜单（系统状态）只对管理员显示 */
const visibleNav = computed(() => navItems.filter((i) => !i.admin || auth.isAdmin))

const activeMenu = computed(() => {
  if (route.path.startsWith('/tracks') || route.path.startsWith('/accounts')) return '/tracks'
  if (route.path.startsWith('/lab')) return '/lab'
  return route.path
})

const backendStatus = computed(() => {
  if (system.error) return { type: 'danger' as const, text: '后端未连接' }
  if (!system.health) return { type: 'info' as const, text: '检测中' }
  return system.health.status === 'ok'
    ? { type: 'success' as const, text: '后端正常' }
    : { type: 'warning' as const, text: '后端部分异常' }
})

// ---- 深色模式：默认跟随系统，手动切换后记住选择 ----
const THEME_KEY = 'sa-theme'
const isDark = ref(false)

function readStoredTheme(): string | null {
  try {
    return localStorage.getItem(THEME_KEY)
  } catch {
    return null
  }
}

function applyTheme(dark: boolean, persist: boolean) {
  isDark.value = dark
  document.documentElement.classList.toggle('dark', dark)
  if (persist) {
    try {
      localStorage.setItem(THEME_KEY, dark ? 'dark' : 'light')
    } catch {
      /* 存储不可用时只在本次会话生效 */
    }
  }
}

// ---- 免责声明：首次打开时弹出一次 ----
const DISCLAIMER_KEY = 'sa-disclaimer-ack'
const showDisclaimer = ref(false)

function acknowledgeDisclaimer() {
  showDisclaimer.value = false
  try {
    localStorage.setItem(DISCLAIMER_KEY, '1')
  } catch {
    /* 忽略 */
  }
}

onMounted(() => {
  const stored = readStoredTheme()
  const prefersDark = window.matchMedia?.('(prefers-color-scheme: dark)').matches ?? false
  applyTheme(stored ? stored === 'dark' : prefersDark, false)

  try {
    showDisclaimer.value = localStorage.getItem(DISCLAIMER_KEY) !== '1'
  } catch {
    showDisclaimer.value = true
  }

  system.startPolling()
})

onBeforeUnmount(() => system.stopPolling())
</script>

<template>
  <el-container class="layout">
    <el-aside width="200px" class="aside">
      <div class="brand">
        <el-icon :size="22"><TrendCharts /></el-icon>
        <span>StockArena</span>
      </div>
      <el-menu :default-active="activeMenu" router class="menu">
        <template v-for="item in visibleNav" :key="item.path">
          <el-sub-menu v-if="item.children" :index="item.path">
            <template #title>
              <el-icon><component :is="item.icon" /></el-icon>
              <span>{{ item.title }}</span>
            </template>
            <el-menu-item v-for="child in item.children" :key="child.path" :index="child.path">
              <el-icon><component :is="child.icon" /></el-icon>
              <span>{{ child.title }}</span>
            </el-menu-item>
          </el-sub-menu>
          <el-menu-item v-else :index="item.path">
            <el-icon><component :is="item.icon" /></el-icon>
            <span>{{ item.title }}</span>
          </el-menu-item>
        </template>
      </el-menu>
    </el-aside>

    <el-container>
      <el-header class="header">
        <span class="header-title">AI 策略工坊</span>
        <div class="header-actions">
          <StockSearch />
          <el-tag :type="backendStatus.type" effect="light" round>{{ backendStatus.text }}</el-tag>
          <el-button circle text icon="QuestionFilled" title="使用手册" @click="$router.push('/help')" />
          <el-button
            circle
            text
            :icon="isDark ? 'Sunny' : 'Moon'"
            :title="isDark ? '切换为浅色' : '切换为深色'"
            @click="applyTheme(!isDark, true)"
          />
          <AccountMenu />
        </div>
      </el-header>

      <el-main class="main">
        <router-view />
      </el-main>

      <el-footer height="36px" class="footer">
        本工具仅用于模拟交易与策略研究，不构成任何投资建议。
        <a v-if="auth.status?.source_url" :href="auth.status.source_url" target="_blank" rel="noopener" class="source">
          源代码（AGPL-3.0）
        </a>
      </el-footer>
    </el-container>

    <el-dialog
      v-model="showDisclaimer"
      title="使用说明"
      width="460px"
      :close-on-click-modal="false"
      :show-close="false"
    >
      <p>
        本工具使用真实行情数据，由 AI 在<strong>模拟回测与虚拟账户</strong>中研究和优化交易策略，并可对比不同 AI
        的交易能力。
      </p>
      <p>模拟结果不代表真实交易表现，<strong>不构成任何投资建议</strong>。</p>
      <p>
        行情与财务数据来自第三方公开接口，可能延迟、缺失或出错，请遵守各数据源的使用条款。AI
        模型由你自己配置，输出可能出错，调用费用由你承担。
      </p>
      <p>平台不连接券商、不会替你下单；是否交易、如何交易由你自己决定。</p>
      <template #footer>
        <el-button type="primary" @click="acknowledgeDisclaimer">我已了解</el-button>
      </template>
    </el-dialog>
  </el-container>
</template>

<style scoped>
.layout {
  height: 100%;
}

.aside {
  border-right: 1px solid var(--el-border-color-light);
  background: var(--el-bg-color);
  display: flex;
  flex-direction: column;
}

.brand {
  height: 60px;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 0 20px;
  font-size: 18px;
  font-weight: 700;
  color: var(--el-color-primary);
}

.menu {
  border-right: none;
  flex: 1;
}

.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  border-bottom: 1px solid var(--el-border-color-light);
  background: var(--el-bg-color);
}

.header-title {
  font-size: 15px;
  color: var(--el-text-color-regular);
}

.header-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.main {
  padding: 20px;
}

.footer .source {
  margin-left: 12px;
  color: inherit;
}

.footer {
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  border-top: 1px solid var(--el-border-color-lighter);
}
</style>
