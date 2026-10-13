<script setup lang="ts">
/**
 * 股票详情的 K 线：蜡烛图 + 均线（MA5/10/20/60）+ 成交量 + MACD(12,26,9)。
 * A股习惯红涨绿跌；默认显示最近约 120 根，可滚轮缩放、拖动。
 */
import { computed } from 'vue'
import VChart from 'vue-echarts'
import type { KBar } from '@/api/arena'
import { useChartTheme } from '@/composables/useChartTheme'

const props = withDefaults(defineProps<{ bars: KBar[]; height?: number; liveDate?: string | null }>(), {
  height: 560,
  liveDate: null,
})
const theme = useChartTheme()

function ma(closes: number[], n: number): (number | null)[] {
  let sum = 0
  return closes.map((c, i) => {
    sum += c
    if (i >= n) sum -= closes[i - n]
    return i >= n - 1 ? +(sum / n).toFixed(3) : null
  })
}

function ema(values: number[], n: number): number[] {
  const k = 2 / (n + 1)
  const out: number[] = []
  values.forEach((v, i) => out.push(i === 0 ? v : v * k + out[i - 1] * (1 - k)))
  return out
}

/** MACD：DIF = EMA12 − EMA26，DEA = DIF 的 EMA9，柱 = 2 × (DIF − DEA) */
function macd(closes: number[]) {
  const e12 = ema(closes, 12)
  const e26 = ema(closes, 26)
  const dif = closes.map((_, i) => e12[i] - e26[i])
  const dea = ema(dif, 9)
  return { dif, dea, hist: dif.map((d, i) => 2 * (d - dea[i])) }
}

const MAS = [5, 10, 20, 60]

const option = computed(() => {
  const t = theme.value
  const dates = props.bars.map((b) => b[0])
  const closes = props.bars.map((b) => b[2])
  const m = macd(closes)
  const r3 = (x: number) => +x.toFixed(3)
  const axisCommon = {
    axisLine: { lineStyle: { color: t.axis } },
    axisTick: { show: false },
    axisLabel: { color: t.muted, fontSize: 11 },
    splitLine: { lineStyle: { color: t.grid } },
  }
  const start = dates.length > 120 ? Math.round((1 - 120 / dates.length) * 100) : 0
  return {
    backgroundColor: 'transparent',
    animation: false,
    legend: {
      top: 0,
      left: 0,
      textStyle: { color: t.textSecondary, fontSize: 12 },
      data: [...MAS.map((n) => `MA${n}`), 'DIF', 'DEA'],
    },
    axisPointer: { link: [{ xAxisIndex: 'all' }], lineStyle: { color: t.muted, width: 1 } },
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross', label: { backgroundColor: t.textSecondary } },
      backgroundColor: t.surface,
      borderColor: t.grid,
      textStyle: { color: t.textPrimary, fontSize: 12 },
      formatter: (params: { dataIndex: number }[]) => {
        const i = params[0]?.dataIndex ?? 0
        const b = props.bars[i]
        if (!b) return ''
        const [d, o, c, l, h, v] = b
        const prev = i > 0 ? props.bars[i - 1][2] : null
        const chg = prev ? ((c / prev - 1) * 100).toFixed(2) : '-'
        const color = prev && c < prev ? t.down : t.up
        const live = d === props.liveDate ? `<span style="color:${t.muted}">（盘中实时）</span>` : ''
        return [
          `<div style="color:${t.muted};margin-bottom:4px">${d}${live}</div>`,
          `开 <b>${o}</b> 收 <b style="color:${color}">${c}</b>（${chg}%）`,
          `低 <b>${l}</b> 高 <b>${h}</b>`,
          `<div style="color:${t.textSecondary}">成交量 ${v >= 1e8 ? (v / 1e8).toFixed(2) + ' 亿' : (v / 1e4).toFixed(0) + ' 万'}</div>`,
          `<div style="color:${t.textSecondary}">MACD ${r3(m.hist[i])}（DIF ${r3(m.dif[i])} DEA ${r3(m.dea[i])}）</div>`,
        ].join('')
      },
    },
    grid: [
      { left: 60, right: 16, top: 28, height: '52%' },
      { left: 60, right: 16, top: '64%', height: '13%' },
      { left: 60, right: 16, top: '81%', bottom: 24 },
    ],
    xAxis: [0, 1, 2].map((g) => ({
      type: 'category',
      data: dates,
      gridIndex: g,
      ...axisCommon,
      splitLine: { show: false },
      axisLabel: { show: g === 2, color: t.muted, fontSize: 11 },
    })),
    yAxis: [
      { type: 'value', scale: true, gridIndex: 0, ...axisCommon },
      { type: 'value', gridIndex: 1, ...axisCommon, axisLabel: { show: false }, splitLine: { show: false } },
      { type: 'value', scale: true, gridIndex: 2, ...axisCommon, axisLabel: { show: false } },
    ],
    dataZoom: [
      { type: 'inside', xAxisIndex: [0, 1, 2], start, end: 100 },
      { type: 'slider', xAxisIndex: [0, 1, 2], start, end: 100, height: 14, bottom: 2, showDetail: false },
    ],
    series: [
      {
        type: 'candlestick',
        name: 'K线',
        data: props.bars.map((b) => [b[1], b[2], b[3], b[4]]),
        itemStyle: { color: t.up, color0: t.down, borderColor: t.up, borderColor0: t.down },
      },
      ...MAS.map((n, k) => ({
        type: 'line',
        name: `MA${n}`,
        data: ma(closes, n),
        showSymbol: false,
        lineStyle: { width: 1.2, color: t.seriesColor(k) },
        itemStyle: { color: t.seriesColor(k) },
      })),
      {
        type: 'bar',
        name: '成交量',
        xAxisIndex: 1,
        yAxisIndex: 1,
        barMaxWidth: 8,
        data: props.bars.map((b, i) => ({
          value: b[5],
          itemStyle: { color: b[2] >= (i > 0 ? props.bars[i - 1][2] : b[1]) ? t.up : t.down, opacity: 0.6 },
        })),
      },
      {
        type: 'bar',
        name: 'MACD',
        xAxisIndex: 2,
        yAxisIndex: 2,
        barMaxWidth: 6,
        data: m.hist.map((x) => ({ value: r3(x), itemStyle: { color: x >= 0 ? t.up : t.down } })),
      },
      {
        type: 'line',
        name: 'DIF',
        xAxisIndex: 2,
        yAxisIndex: 2,
        data: m.dif.map(r3),
        showSymbol: false,
        lineStyle: { width: 1, color: t.seriesColor(4) },
        itemStyle: { color: t.seriesColor(4) },
      },
      {
        type: 'line',
        name: 'DEA',
        xAxisIndex: 2,
        yAxisIndex: 2,
        data: m.dea.map(r3),
        showSymbol: false,
        lineStyle: { width: 1, color: t.seriesColor(6) },
        itemStyle: { color: t.seriesColor(6) },
      },
    ],
  }
})
</script>

<template>
  <div :style="{ height: `${height}px` }">
    <el-empty v-if="bars.length === 0" description="暂无行情" :image-size="60" />
    <v-chart v-else :option="option" autoresize />
  </div>
</template>
