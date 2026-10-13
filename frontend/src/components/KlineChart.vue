<script setup lang="ts">
/** K 线 + 买卖点。A股习惯红涨绿跌；买卖点同时用形状和文字（B 买入 / S 卖出 / 空 卖空 / 平 平空）区分，不只依赖颜色。 */
import { computed } from 'vue'
import VChart from 'vue-echarts'
import type { FillItem, KBar } from '@/api/arena'
import { useChartTheme } from '@/composables/useChartTheme'
import { escapeHtml } from '@/utils/html'
import { sideClass, sideText } from '@/utils/orders'

const props = withDefaults(defineProps<{ bars: KBar[]; fills: FillItem[]; height?: number }>(), { height: 460 })
const emit = defineEmits<{ (e: 'select-fill', fill: FillItem): void }>()
const theme = useChartTheme()

function ma(closes: number[], n: number): (number | null)[] {
  return closes.map((_, i) => {
    if (i < n - 1) return null
    let sum = 0
    for (let k = i - n + 1; k <= i; k++) sum += closes[k]
    return +(sum / n).toFixed(3)
  })
}

const MARKER_LABELS: Record<string, string> = { buy: 'B', sell: 'S', short: '空', cover: '平' }

const option = computed(() => {
  const t = theme.value
  const dates = props.bars.map((b) => b[0])
  const closes = props.bars.map((b) => b[2])
  const byDate = new Map(props.bars.map((b) => [b[0], b]))
  const markers = props.fills
    .filter((f) => byDate.has(f.trade_date))
    .map((f) => {
      const bar = byDate.get(f.trade_date) as KBar
      // 买入 / 平空在 K 线下方（红色向上三角），卖出 / 卖空在上方（绿色向下三角）
      const buy = f.side === 'buy' || f.side === 'cover'
      return {
        value: [f.trade_date, buy ? bar[3] * 0.985 : bar[4] * 1.015],
        fill: f,
        symbol: buy ? 'triangle' : 'path://M0,0 L10,0 L5,10 Z',
        symbolSize: 12,
        itemStyle: { color: buy ? t.up : t.down, borderColor: t.surface, borderWidth: 2 },
        label: {
          show: true,
          position: buy ? 'bottom' : 'top',
          formatter: MARKER_LABELS[f.side] ?? f.side,
          color: t.textSecondary,
          fontSize: 11,
          fontWeight: 'bold',
        },
      }
    })
  const axisCommon = {
    axisLine: { lineStyle: { color: t.axis } },
    axisTick: { show: false },
    axisLabel: { color: t.muted, fontSize: 11 },
    splitLine: { lineStyle: { color: t.grid } },
  }
  return {
    backgroundColor: 'transparent',
    animation: false,
    legend: { top: 4, left: 0, textStyle: { color: t.textSecondary, fontSize: 12 }, data: ['MA5', 'MA20'] },
    axisPointer: { link: [{ xAxisIndex: 'all' }], lineStyle: { color: t.muted, width: 1 } },
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross', label: { backgroundColor: t.textSecondary } },
      backgroundColor: t.surface,
      borderColor: t.grid,
      textStyle: { color: t.textPrimary, fontSize: 12 },
      formatter: (params: { seriesType: string; axisValue: string; data: unknown; seriesName: string }[]) => {
        const date = params[0]?.axisValue ?? ''
        const bar = byDate.get(date)
        const lines = [`<div style="color:${t.muted};margin-bottom:4px">${date}</div>`]
        if (bar) {
          const [, o, c, l, h, v] = bar
          lines.push(`开 <b>${o}</b> 收 <b>${c}</b> 低 <b>${l}</b> 高 <b>${h}</b>`)
          lines.push(`<div style="color:${t.textSecondary}">成交量 ${(v / 1e4).toFixed(0)} 万</div>`)
        }
        for (const f of props.fills.filter((x) => x.trade_date === date)) {
          lines.push(
            `<div style="margin-top:6px"><b style="color:${sideClass(f.side) === 'sa-up' ? t.up : t.down}">` +
              `${sideText(f.side)} ${f.qty} 股 @ ${f.price}</b></div>` +
              (f.reason
                ? `<div style="max-width:280px;white-space:normal;color:${t.textSecondary}">${escapeHtml(f.reason)}</div>`
                : ''),
          )
        }
        return lines.join('')
      },
    },
    grid: [
      { left: 56, right: 24, top: 36, height: '62%' },
      { left: 56, right: 24, top: '80%', bottom: 24 },
    ],
    xAxis: [
      {
        type: 'category',
        data: dates,
        gridIndex: 0,
        ...axisCommon,
        splitLine: { show: false },
        axisLabel: { show: false },
      },
      { type: 'category', data: dates, gridIndex: 1, ...axisCommon, splitLine: { show: false } },
    ],
    yAxis: [
      { type: 'value', scale: true, gridIndex: 0, ...axisCommon },
      { type: 'value', gridIndex: 1, ...axisCommon, axisLabel: { show: false }, splitLine: { show: false } },
    ],
    dataZoom: [{ type: 'inside', xAxisIndex: [0, 1], start: dates.length > 120 ? 40 : 0 }],
    series: [
      {
        type: 'candlestick',
        name: 'K线',
        data: props.bars.map((b) => [b[1], b[2], b[3], b[4]]),
        itemStyle: { color: t.up, color0: t.down, borderColor: t.up, borderColor0: t.down },
      },
      {
        type: 'line',
        name: 'MA5',
        data: ma(closes, 5),
        showSymbol: false,
        lineStyle: { width: 1.5, color: t.seriesColor(0) },
        itemStyle: { color: t.seriesColor(0) },
      },
      {
        type: 'line',
        name: 'MA20',
        data: ma(closes, 20),
        showSymbol: false,
        lineStyle: { width: 1.5, color: t.seriesColor(1) },
        itemStyle: { color: t.seriesColor(1) },
      },
      { type: 'scatter', name: '成交', data: markers, z: 10, cursor: 'pointer' },
      {
        type: 'bar',
        name: '成交量',
        xAxisIndex: 1,
        yAxisIndex: 1,
        barMaxWidth: 8,
        data: props.bars.map((b) => ({
          value: b[5],
          itemStyle: { color: b[2] >= b[1] ? t.up : t.down, opacity: 0.6 },
        })),
      },
    ],
  }
})

function onClick(e: { seriesType?: string; data?: unknown }) {
  const fill = (e.data as { fill?: FillItem } | undefined)?.fill
  if (e.seriesType === 'scatter' && fill) emit('select-fill', fill)
}
</script>

<template>
  <div :style="{ height: `${height}px` }">
    <el-empty v-if="bars.length === 0" description="暂无行情" :image-size="60" />
    <v-chart v-else :option="option" autoresize @click="onClick" />
  </div>
</template>
