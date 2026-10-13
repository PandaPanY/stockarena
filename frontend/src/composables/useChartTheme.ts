import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

/**
 * 图表配色（参照 dataviz 参考调色板，已用校验脚本验证色盲区分度）。
 * 类别色按固定顺序分配给选手，颜色跟随选手而不是排名。
 */
const SERIES_LIGHT = ['#2a78d6', '#eb6834', '#1baf7a', '#eda100', '#e87ba4', '#008300', '#4a3aa7', '#e34948']
const SERIES_DARK = ['#3987e5', '#d95926', '#199e70', '#c98500', '#d55181', '#008300', '#9085e9', '#e66767']

const CHROME = {
  light: {
    surface: '#fcfcfb',
    textPrimary: '#0b0b0b',
    textSecondary: '#52514e',
    muted: '#898781',
    grid: '#e1e0d9',
    axis: '#c3c2b7',
    benchmark: '#52514e',
  },
  dark: {
    surface: '#1a1a19',
    textPrimary: '#ffffff',
    textSecondary: '#c3c2b7',
    muted: '#898781',
    grid: '#2c2c2a',
    axis: '#383835',
    benchmark: '#c3c2b7',
  },
}

/** A股习惯：红涨绿跌 */
const MARKET = {
  light: { up: '#d03b3b', down: '#0c8a0c' },
  dark: { up: '#e66767', down: '#0ca30c' },
}

export function useChartTheme() {
  const isDark = ref(document.documentElement.classList.contains('dark'))
  let observer: MutationObserver | undefined

  onMounted(() => {
    observer = new MutationObserver(() => {
      isDark.value = document.documentElement.classList.contains('dark')
    })
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] })
  })
  onBeforeUnmount(() => observer?.disconnect())

  return computed(() => {
    const mode = isDark.value ? 'dark' : 'light'
    const series = isDark.value ? SERIES_DARK : SERIES_LIGHT
    return {
      mode,
      ...CHROME[mode],
      ...MARKET[mode],
      /** 第 i 个选手的颜色；超过 8 个时使用中性灰（避免生成难以区分的新颜色） */
      seriesColor: (i: number) => (i < series.length ? series[i] : CHROME[mode].muted),
    }
  })
}

export type ChartTheme = ReturnType<typeof useChartTheme>['value']
