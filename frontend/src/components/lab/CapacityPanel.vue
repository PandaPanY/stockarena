<script setup lang="ts">
/** 资金容量（PRD 3.9 G4 / G6）：不同资金规模下的收益、成本、冲击成本与持仓偏离。 */
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { errorMessage } from '@/api/http'
import { checkApi, type CapacityResult } from '@/api/quant'
import { formatPercent } from '@/utils/format'

const props = defineProps<{ kind: 'strategy' | 'portfolio'; id: number }>()
const data = ref<CapacityResult | null>(null)
const loading = ref(false)

async function load() {
  loading.value = true
  try {
    data.value = await checkApi.capacity(props.kind, props.id)
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    loading.value = false
  }
}
onMounted(load)

function money(v: number | null) {
  if (v === null) return '-'
  return v >= 1e8 ? `${v / 1e8} 亿` : `${v / 1e4} 万`
}
const pct = (v: number | null, d = 1) => formatPercent(v, d)
</script>

<template>
  <div v-loading="loading">
    <p class="muted">
      用同一个版本在不同资金规模下回测（含冲击成本：额外价格偏离 = 日波动率 × √(成交额 /
      当日成交额)，单笔不超过当日成交额的 10%）。
      资金太小时，股价高的股票买不起一手；资金太大时，冲击成本上升、成交受限。首次计算约需 10～30 秒。
    </p>
    <template v-if="data">
      <div class="summary">
        <div>
          <span class="label">最低资金</span><span class="value">{{ money(data.min_capital) }}</span>
        </div>
        <div>
          <span class="label">可承载资金</span><span class="value">{{ money(data.capacity) }}</span>
        </div>
      </div>
      <p class="muted">{{ data.rule }}</p>
      <el-table :data="data.levels" size="small" border>
        <el-table-column label="资金" width="90">
          <template #default="{ row }">{{ money(row.capital) }}</template>
        </el-table-column>
        <el-table-column label="开发期年化" width="100" align="right">
          <template #default="{ row }">{{ pct(row.dev_annual) }}</template>
        </el-table-column>
        <el-table-column label="开发期夏普" width="100" align="right">
          <template #default="{ row }">{{ row.dev_sharpe?.toFixed(2) ?? '-' }}</template>
        </el-table-column>
        <el-table-column label="留出收益" width="100" align="right">
          <template #default="{ row }">{{ pct(row.holdout_return) }}</template>
        </el-table-column>
        <el-table-column label="年化总成本" width="100" align="right">
          <template #default="{ row }">{{ formatPercent(row.cost_ratio, 2, false) }}</template>
        </el-table-column>
        <el-table-column label="其中冲击成本" width="110" align="right">
          <template #default="{ row }">{{ formatPercent(row.impact_ratio, 2, false) }}</template>
        </el-table-column>
        <el-table-column label="平均仓位" width="90" align="right">
          <template #default="{ row }">{{ formatPercent(row.exposure, 0, false) }}</template>
        </el-table-column>
        <el-table-column label="持仓偏离" width="90" align="right">
          <template #default="{ row }">{{ formatPercent(row.deviation, 1, false) }}</template>
        </el-table-column>
        <el-table-column label="额外偏离" width="90" align="right">
          <template #default="{ row }">{{ formatPercent(row.extra_deviation, 1, false) }}</template>
        </el-table-column>
        <el-table-column label="买不起一手" width="100" align="right" prop="lot_blocked" />
        <el-table-column label="受成交额限制" width="110" align="right" prop="capped" />
      </el-table>
    </template>
  </div>
</template>

<style scoped>
.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 1.8;
}

.summary {
  display: flex;
  gap: 32px;
  margin: 8px 0;
}

.summary > div {
  display: flex;
  flex-direction: column;
}

.label {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.value {
  font-size: 18px;
  font-weight: 600;
}
</style>
