<script setup lang="ts">
/**
 * 多选手收益曲线对比（含基准）+ 回撤曲线。
 * 净值统一换算为相对初始本金的累计收益率，在同一纵轴上比较；回撤单独一张图（不使用双纵轴）。
 */
import { computed } from 'vue'
import VChart from 'vue-echarts'
import type { NavSeries } from '@/api/arena'
import { useChartTheme } from '@/composables/useChartTheme'
import { escapeHtml } from '@/utils/html'

const props = withDefaults(defineProps<{ data: NavSeries | null; height?: number; showDrawdown?: boolean }>(), {
  height: 420,
  showDrawdown: true,
})

const theme = useChartTheme()

/** 由累计收益序列计算回撤（缺失值保持为空，保证与日期对齐） */
function drawdown(returns: (number | null)[]): (number | null)[] {
  let peak = -Infinity
  return returns.map((r) => {
    if (r === null) return null
    const nav = r + 1
    peak = Math.max(peak, nav)
    return nav / peak - 1
  })
}

const pct = (v: number, digits = 1) => `${v > 0 ? '+' : ''}${(v * 100).toFixed(digits)}%`

const option = computed(() => {
  const t = theme.value
  const data = props.data
  if (!data) return {}
  const dates = Array.from(
    new Set([...data.benchmark.points.map((p) => p[0]), ...data.accounts.flatMap((a) => a.points.map((p) => p[0]))]),
  ).sort()
  const few = data.accounts.length <= 4
  const showDd = props.showDrawdown

  const accountSeries = data.accounts.map((acc, i) => {
    const map = new Map(acc.points.map((p) => [p[0], p[1]]))
    const values = dates.map((d) => (map.has(d) ? (map.get(d) as number) - 1 : null))
    return { acc, color: t.seriesColor(i), values }
  })
  const benchMap = new Map(data.benchmark.points.map((p) => [p[0], p[1]]))
  const benchValues = dates.map((d) => (benchMap.has(d) ? (benchMap.get(d) as number) - 1 : null))
  const benchName = `基准·${data.benchmark.name}`

  const lineBase = { type: 'line', showSymbol: false, symbolSize: 8, connectNulls: true, emphasis: { focus: 'series' } }
  const series: Record<string, unknown>[] = [
    ...accountSeries.map(({ acc, color, values }) => ({
      ...lineBase,
      name: acc.name,
      data: values,
      lineStyle: { width: 2, color },
      itemStyle: { color },
      // 选手不多时在曲线末端直接标注名称与收益
      endLabel: few
        ? {
            show: true,
            color: t.textSecondary,
            fontSize: 11,
            formatter: (p: { value: number }) => `${acc.name}  ${pct(p.value)}`,
          }
        : { show: false },
    })),
  ]
  // 跨赛道对比时没有统一的基准
  const hasBench = data.benchmark.points.length > 0
  if (hasBench) {
    series.push({
      ...lineBase,
      name: benchName,
      data: benchValues,
      lineStyle: { width: 1.5, color: t.benchmark, opacity: 0.8 },
      itemStyle: { color: t.benchmark },
      z: 1,
    })
  }
  if (showDd) {
    for (const { acc, color, values } of accountSeries) {
      series.push({
        ...lineBase,
        name: acc.name,
        xAxisIndex: 1,
        yAxisIndex: 1,
        data: drawdown(values),
        lineStyle: { width: 1.5, color },
        itemStyle: { color },
      })
    }
  }

  const axisCommon = {
    axisLine: { lineStyle: { color: t.axis } },
    axisTick: { show: false },
    axisLabel: { color: t.muted, fontSize: 11 },
    splitLine: { lineStyle: { color: t.grid } },
  }
  const grids = showDd
    ? [
        { left: 56, right: few ? 150 : 24, top: 64, height: '48%' },
        { left: 56, right: few ? 150 : 24, top: '72%', bottom: 28 },
      ]
    : [{ left: 56, right: few ? 150 : 24, top: 64, bottom: 28 }]

  return {
    backgroundColor: 'transparent',
    animation: false,
    textStyle: { fontFamily: 'system-ui, -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif' },
    legend: {
      top: 0,
      left: 0,
      icon: 'path://M0,4 L16,4 L16,6 L0,6 Z',
      itemWidth: 16,
      itemHeight: 6,
      textStyle: { color: t.textSecondary, fontSize: 12 },
      data: [...accountSeries.map((s) => s.acc.name), ...(hasBench ? [benchName] : [])],
    },
    axisPointer: { link: [{ xAxisIndex: 'all' }], lineStyle: { color: t.muted, width: 1 } },
    tooltip: {
      trigger: 'axis',
      backgroundColor: t.surface,
      borderColor: t.grid,
      textStyle: { color: t.textPrimary, fontSize: 12 },
      formatter: (
        params: {
          seriesName: string
          value: number | null
          color: string
          axisValue: string
          axisIndex?: number
          seriesIndex: number
        }[],
      ) => {
        const rows = params
          .filter((p) => p.value !== null && p.value !== undefined && !Number.isNaN(p.value))
          .filter((p) => p.seriesIndex <= accountSeries.length) // 只列收益曲线（回撤见下方图）
          .sort((a, b) => (b.value as number) - (a.value as number))
          .map(
            (p) =>
              `<div style="display:flex;align-items:center;gap:8px;line-height:20px">` +
              `<span style="display:inline-block;width:12px;height:2px;background:${p.color}"></span>` +
              `<b style="min-width:64px;text-align:right">${pct(p.value as number, 2)}</b>` +
              `<span style="color:${t.textSecondary}">${escapeHtml(p.seriesName)}</span></div>`,
          )
        return `<div style="color:${t.muted};margin-bottom:4px">${params[0]?.axisValue ?? ''}</div>${rows.join('')}`
      },
    },
    grid: grids,
    xAxis: grids.map((_, i) => ({
      type: 'category',
      gridIndex: i,
      data: dates,
      boundaryGap: false,
      ...axisCommon,
      splitLine: { show: false },
      axisLabel: { ...axisCommon.axisLabel, show: i === grids.length - 1 },
    })),
    yAxis: grids.map((_, i) => ({
      type: 'value',
      gridIndex: i,
      scale: i === 0,
      max: i === 1 ? 0 : undefined,
      name: i === 0 ? '累计收益' : '回撤',
      nameTextStyle: { color: t.muted, fontSize: 11, align: 'left' },
      ...axisCommon,
      axisLabel: { ...axisCommon.axisLabel, formatter: (v: number) => `${(v * 100).toFixed(0)}%` },
    })),
    dataZoom: dates.length > 60 ? [{ type: 'inside', xAxisIndex: grids.map((_, i) => i) }] : [],
    series,
  }
})
</script>

<template>
  <div class="nav-chart" :style="{ height: `${height}px` }">
    <el-empty
      v-if="!data || data.accounts.every((a) => a.points.length === 0)"
      description="暂无净值数据（首轮结算后出现）"
      :image-size="60"
    />
    <v-chart v-else :option="option" autoresize />
  </div>
</template>

<style scoped>
.nav-chart {
  width: 100%;
}
</style>
