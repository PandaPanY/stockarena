<script setup lang="ts">
/** 小走势图：只有折线，颜色按区间涨跌（红涨绿跌） */
import { computed } from 'vue'
import VChart from 'vue-echarts'
import { useChartTheme } from '@/composables/useChartTheme'

const props = withDefaults(defineProps<{ values: number[]; width?: number; height?: number }>(), {
  width: 96,
  height: 28,
})
const theme = useChartTheme()

const option = computed(() => {
  const v = props.values
  const up = v.length > 1 && v[v.length - 1] >= v[0]
  const color = up ? theme.value.up : theme.value.down
  return {
    animation: false,
    grid: { left: 0, right: 0, top: 2, bottom: 2 },
    xAxis: { type: 'category', show: false, data: v.map((_, i) => i) },
    yAxis: { type: 'value', show: false, scale: true },
    series: [{ type: 'line', data: v, showSymbol: false, lineStyle: { width: 1.2, color }, silent: true }],
  }
})
</script>

<template>
  <div :style="{ width: `${width}px`, height: `${height}px` }">
    <v-chart v-if="values.length > 1" :option="option" autoresize />
  </div>
</template>
