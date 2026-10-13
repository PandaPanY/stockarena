<script setup lang="ts">
/**
 * 策略回测曲线：累计收益（策略 vs 基准），用底色区分训练 / 验证 / 留出区间。
 * mini 模式用于策略卡片上的缩略图（无坐标轴、无图例）。
 */
import { computed } from 'vue'
import VChart from 'vue-echarts'
import type { Curves } from '@/api/quant'
import { useChartTheme } from '@/composables/useChartTheme'

const props = withDefaults(
  defineProps<{ curves: Curves | null; height?: number; mini?: boolean; name?: string; benchName?: string }>(),
  {
    height: 360,
    mini: false,
    name: '策略',
    benchName: '基准',
  },
)

const theme = useChartTheme()
const pct = (v: number) => `${v > 0 ? '+' : ''}${(v * 100).toFixed(1)}%`

const option = computed(() => {
  const c = props.curves
  const t = theme.value
  if (!c || !c.dates.length) return {}
  const nav = c.nav.map((v) => v - 1)
  const bench = c.bench.map((v) => v - 1)
  const last = c.dates[c.dates.length - 1]
  // 区间底色：训练（无底色）、验证、留出
  const areas: [Record<string, unknown>, Record<string, unknown>][] = []
  if (c.train_end && c.valid_end) {
    areas.push([
      { name: props.mini ? '' : '验证', xAxis: c.dates.find((d) => d > c.train_end!) ?? last },
      { xAxis: c.dates.filter((d) => d <= c.valid_end!).pop() ?? last },
    ])
    if (last > c.valid_end) {
      areas.push([
        { name: props.mini ? '' : '留出', xAxis: c.dates.find((d) => d > c.valid_end!) ?? last },
        { xAxis: last },
      ])
    }
  }
  const strategyColor = t.seriesColor(0)
  return {
    animation: false,
    grid: props.mini ? { left: 2, right: 2, top: 4, bottom: 2 } : { left: 56, right: 24, top: 36, bottom: 56 },
    tooltip: props.mini
      ? { show: false }
      : {
          trigger: 'axis',
          valueFormatter: (v: number) => (v === null || v === undefined ? '-' : pct(v)),
        },
    legend: props.mini ? { show: false } : { top: 4, textStyle: { color: t.textSecondary } },
    xAxis: {
      type: 'category',
      data: c.dates,
      show: !props.mini,
      boundaryGap: false,
      axisLine: { lineStyle: { color: t.axis } },
      axisLabel: { color: t.muted },
    },
    yAxis: {
      type: 'value',
      show: !props.mini,
      scale: true,
      axisLabel: { color: t.muted, formatter: (v: number) => `${(v * 100).toFixed(0)}%` },
      splitLine: { lineStyle: { color: t.grid } },
    },
    dataZoom: props.mini ? [] : [{ type: 'inside' }, { type: 'slider', height: 18, bottom: 8 }],
    series: [
      {
        name: props.name,
        type: 'line',
        data: nav,
        showSymbol: false,
        lineStyle: { width: props.mini ? 1.5 : 2, color: strategyColor },
        itemStyle: { color: strategyColor },
        markArea: {
          silent: true,
          itemStyle: { color: t.mode === 'dark' ? 'rgba(255,255,255,0.05)' : 'rgba(0,0,0,0.04)' },
          label: { color: t.muted, fontSize: 11 },
          data: areas,
        },
      },
      {
        name: props.benchName,
        type: 'line',
        data: bench,
        showSymbol: false,
        lineStyle: { width: 1, color: t.benchmark, opacity: 0.7 },
        itemStyle: { color: t.benchmark },
        z: 1,
      },
    ],
  }
})
</script>

<template>
  <v-chart v-if="curves" :option="option" :style="{ height: `${height}px` }" autoresize />
  <el-empty v-else description="暂无曲线" :image-size="48" />
</template>
