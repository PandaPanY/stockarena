/** 侧边栏菜单，icon 为 Element Plus 图标组件名；children 为折叠分组 */
export interface NavItem {
  path: string
  title: string
  icon: string
  children?: NavItem[]
  /** 只对管理员显示 */
  admin?: boolean
}

export const navItems: NavItem[] = [
  { path: '/', title: '首页', icon: 'House' },
  { path: '/watch', title: '自选股', icon: 'Star' },
  { path: '/lab', title: '策略库', icon: 'Collection' },
  { path: '/portfolios', title: '多风格组合', icon: 'PieChart' },
  { path: '/signals', title: '每日信号', icon: 'Bell' },
  { path: '/real', title: '实盘对照', icon: 'Wallet' },
  { path: '/factors', title: '因子研究', icon: 'DataAnalysis' },
  {
    path: 'arena',
    title: 'AI 对比',
    icon: 'Trophy',
    children: [
      { path: '/arena', title: '总览', icon: 'DataBoard' },
      { path: '/tracks', title: '赛道', icon: 'Flag' },
      { path: '/decisions', title: '决策回放', icon: 'ChatLineSquare' },
      { path: '/compare', title: '对比分析', icon: 'Histogram' },
      { path: '/experiments', title: '进化实验', icon: 'MagicStick' },
    ],
  },
  { path: '/settings', title: '配置中心', icon: 'Setting' },
  { path: '/system', title: '系统状态', icon: 'Monitor', admin: true },
  { path: '/help', title: '使用手册', icon: 'Reading' },
]
