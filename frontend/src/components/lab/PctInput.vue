<script setup lang="ts">
/** 百分比输入：内部按小数保存（0.05），界面按百分比显示（5%）。 */
import { computed } from 'vue'

const model = defineModel<number | null>({ required: true })
const props = withDefaults(defineProps<{ min?: number; max?: number; step?: number; precision?: number }>(), {
  min: 0,
  max: 100,
  step: 1,
  precision: 1,
})

const shown = computed({
  get: () =>
    model.value === null ? undefined : Math.round(model.value * 100 * 10 ** props.precision) / 10 ** props.precision,
  set: (v: number | undefined) => {
    model.value =
      v === undefined || v === null ? null : Math.round(v * 10 ** (props.precision + 2)) / 10 ** (props.precision + 4)
  },
})
</script>

<template>
  <span class="pct">
    <el-input-number
      v-model="shown"
      :min="min"
      :max="max"
      :step="step"
      :precision="precision"
      size="small"
      controls-position="right"
      style="width: 110px"
    />
    <span class="unit">%</span>
  </span>
</template>

<style scoped>
.pct {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.unit {
  color: var(--el-text-color-secondary);
}
</style>
