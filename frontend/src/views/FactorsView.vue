<script setup lang="ts">
/**
 * 因子研究（PRD 3.9 G2）：用股票池中全部可选股票检验每个因子的选股能力。
 * 与 AI 研究员看到的「因子成绩单」相同（AI 只看开发期）。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { errorMessage } from '@/api/http'
import { metaApi, type Universe } from '@/api/arena'
import { factorApi, quantApi, type FactorResearch, type Strategy } from '@/api/quant'
import { formatPercent } from '@/utils/format'

const windowKey = ref<'dev' | 'holdout'>('dev')
const strategyId = ref<number | undefined>(undefined)
const universe = ref<string>('cn_csi300_hist')
const universes = ref<Universe[]>([])
const strategies = ref<Strategy[]>([])
const data = ref<FactorResearch | null>(null)
const loading = ref(false)

async function load() {
  loading.value = true
  try {
    data.value = await factorApi.scorecard(windowKey.value, strategyId.value, universe.value)
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  load()
  try {
    strategies.value = (await quantApi.list()).strategies
    universes.value = (await metaApi.universes()).filter((u) => u.point_in_time)
  } catch {
    /* 策略列表与股票池只用于筛选，失败不影响主表 */
  }
})
watch([windowKey, strategyId, universe], load)

const years = computed(() => {
  const set = new Set<string>()
  for (const f of data.value?.factors ?? []) Object.keys(f.years).forEach((y) => set.add(y))
  return [...set].sort()
})

function icColor(v: number | undefined) {
  if (v === undefined) return {}
  const a = Math.min(Math.abs(v) / 0.1, 1)
  return { background: v >= 0 ? `rgba(208, 59, 59, ${0.12 + a * 0.5})` : `rgba(12, 138, 12, ${0.12 + a * 0.5})` }
}
const strong = (t: number | null) => t !== null && Math.abs(t) >= 2
</script>

<template>
  <div v-loading="loading">
    <h2 class="sa-page-title">因子研究</h2>
    <p class="muted">
      每隔 {{ data?.step ?? 20 }} 个交易日，在股票池全部可选股票（非 ST、上市满 120 日、日均成交额 ≥ 0.5
      亿）中计算因子值与之后 {{ data?.horizon ?? 20 }} 个交易日收益的排名相关系数（IC），以及按因子分 5 组的收益。
      <strong>|t| ≥ 2</strong> 才算统计上可靠；逐年 IC 正负翻转说明因子在不同行情中不稳定。
      五分组收益受股票池幸存者偏差（使用当前成分股名单）影响整体偏高，应比较组间差异而非绝对水平。
    </p>
    <div class="bar">
      <el-radio-group v-model="windowKey" size="small">
        <el-radio-button value="dev">开发期（AI 可见）</el-radio-button>
        <el-radio-button value="holdout">留出期（仅供参考）</el-radio-button>
      </el-radio-group>
      <el-select v-model="universe" size="small" style="width: 200px" :disabled="!!strategyId">
        <el-option v-for="u in universes" :key="u.name" :label="u.title" :value="u.name" />
      </el-select>
      <el-select
        v-model="strategyId"
        clearable
        placeholder="加入某个策略冠军用到的参数"
        size="small"
        style="width: 240px"
      >
        <el-option v-for="s in strategies" :key="s.id" :label="s.name" :value="s.id" />
      </el-select>
      <span v-if="data" class="muted">{{ data.start }} ～ {{ data.end }}</span>
    </div>

    <el-table v-if="data" :data="data.factors" size="small" border>
      <el-table-column label="因子" min-width="150" fixed>
        <template #default="{ row }">
          {{ row.label }} <el-tag v-if="row.fundamental" size="small" type="info">财报</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="平均 IC" width="80" align="right">
        <template #default="{ row }">{{ row.ic.toFixed(3) }}</template>
      </el-table-column>
      <el-table-column label="ICIR" width="70" align="right" prop="icir" />
      <el-table-column label="t 值" width="70" align="right">
        <template #default="{ row }"
          ><strong v-if="strong(row.t)">{{ row.t }}</strong
          ><span v-else>{{ row.t }}</span></template
        >
      </el-table-column>
      <el-table-column label="同向比例" width="80" align="right">
        <template #default="{ row }">{{ formatPercent(row.positive, 0, false) }}</template>
      </el-table-column>
      <el-table-column label="方向" width="80">
        <template #default="{ row }">{{ row.direction === 'high' ? '越高越好' : '越低越好' }}</template>
      </el-table-column>
      <el-table-column v-for="y in years" :key="y" :label="y" width="64" align="center">
        <template #default="{ row }">
          <span class="ic" :style="icColor(row.years[y])">{{
            row.years[y] === undefined ? '' : row.years[y].toFixed(2)
          }}</span>
        </template>
      </el-table-column>
      <el-table-column label="五分组年化（低 → 高）" min-width="230">
        <template #default="{ row }">
          <span v-for="(q, i) in row.quintiles" :key="i" class="q" :class="q >= 0 ? 'sa-up' : 'sa-down'">
            {{ formatPercent(q, 0) }}
          </span>
        </template>
      </el-table-column>
      <el-table-column label="多空年化" width="90" align="right">
        <template #default="{ row }">{{ formatPercent(row.long_short, 1) }}</template>
      </el-table-column>
    </el-table>
  </div>
</template>

<style scoped>
.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 1.8;
}

.bar {
  display: flex;
  gap: 12px;
  align-items: center;
  margin: 8px 0;
  flex-wrap: wrap;
}

.ic {
  display: inline-block;
  width: 48px;
  border-radius: 3px;
  font-variant-numeric: tabular-nums;
}

.q {
  display: inline-block;
  width: 44px;
  text-align: right;
  font-variant-numeric: tabular-nums;
}
</style>
