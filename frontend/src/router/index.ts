import { createRouter, createWebHistory } from 'vue-router'
import MainLayout from '@/layouts/MainLayout.vue'
import { setUnauthorizedHandler } from '@/api/http'
import { useAuthStore } from '@/stores/auth'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', name: 'login', component: () => import('@/views/LoginView.vue') },
    {
      path: '/',
      component: MainLayout,
      children: [
        { path: '', name: 'home', component: () => import('@/views/HomeView.vue') },
        { path: 'lab', name: 'lab', component: () => import('@/views/LabView.vue') },
        { path: 'watch', name: 'watch', component: () => import('@/views/WatchView.vue') },
        { path: 'lab/:id', name: 'strategy', component: () => import('@/views/StrategyDetailView.vue') },
        { path: 'portfolios', name: 'portfolios', component: () => import('@/views/PortfoliosView.vue') },
        { path: 'factors', name: 'factors', component: () => import('@/views/FactorsView.vue') },
        { path: 'signals', name: 'signals', component: () => import('@/views/SignalsView.vue') },
        { path: 'real', name: 'real', component: () => import('@/views/RealView.vue') },
        { path: 'arena', name: 'dashboard', component: () => import('@/views/DashboardView.vue') },
        { path: 'tracks', name: 'tracks', component: () => import('@/views/TracksView.vue') },
        { path: 'tracks/:id', name: 'track', component: () => import('@/views/TrackDetailView.vue') },
        { path: 'accounts/:id', name: 'account', component: () => import('@/views/AccountView.vue') },
        { path: 'decisions', name: 'decisions', component: () => import('@/views/DecisionsView.vue') },
        { path: 'compare', name: 'compare', component: () => import('@/views/CompareView.vue') },
        { path: 'experiments', name: 'experiments', component: () => import('@/views/ExperimentsView.vue') },
        { path: 'settings', name: 'settings', component: () => import('@/views/SettingsView.vue') },
        { path: 'system', name: 'system', component: () => import('@/views/SystemView.vue'), meta: { admin: true } },
        { path: 'help', name: 'help', component: () => import('@/views/HelpView.vue') },
        { path: 'stock/:symbol', name: 'stock', component: () => import('@/views/StockView.vue') },
      ],
    },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})

// 未登录时进入任何页面都先到登录页，登录后回到原页面
router.beforeEach(async (to) => {
  if (to.name === 'login') return true
  try {
    const st = await useAuthStore().load()
    if (st.enabled && !st.authenticated) return { name: 'login', query: { redirect: to.fullPath } }
    if (to.meta.admin && !useAuthStore().isAdmin) return '/' // 管理员专属页面
  } catch {
    // 后端暂时连不上时照常进入页面，由页面显示连接错误
  }
  return true
})

setUnauthorizedHandler((code) => {
  useAuthStore().markLoggedOut(code === 'login_required')
  const current = router.currentRoute.value
  if (current.name !== 'login') void router.replace({ name: 'login', query: { redirect: current.fullPath } })
})

export default router
