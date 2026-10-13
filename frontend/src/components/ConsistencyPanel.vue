<script setup lang="ts">
/** 推理-行为一致性评估：用评审模型抽样判断 AI 的分析结论与实际订单是否一致（PRD 3.6）。 */
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { errorMessage } from '@/api/http'
import { providersApi, tracksApi, type ConsistencyResult, type Provider } from '@/api/arena'
import { formatPercent } from '@/utils/format'

const props = defineProps<{ trackId: number }>()
const router = useRouter()

const providers = ref<Provider[]>([])
const result = ref<ConsistencyResult | null>(null)
const form = ref({ provider_id: null as number | null, model: '', sample_size: 20 })
const starting = ref(false)
let timer: number | undefined

const VERDICTS: Record<string, { label: string; type: 'success' | 'warning' | 'danger' }> = {
  consistent: { label: '一致', type: 'success' },
  partial: { label: '部分一致', type: 'warning' },
  inconsistent: { label: '不一致', type: 'danger' },
}

async function load() {
  try {
    result.value = await tracksApi.consistency(props.trackId)
  } catch (err) {
    ElMessage.error(errorMessage(err))
  }
  window.clearTimeout(timer)
  if (result.value?.running) timer = window.setTimeout(load, 5000)
}

async function run() {
  if (!form.value.provider_id) return ElMessage.warning('请选择评审模型的供应商')
  starting.value = true
  try {
    const r = await tracksApi.runConsistency(props.trackId, {
      provider_id: form.value.provider_id,
      model: form.value.model || null,
      sample_size: form.value.sample_size,
    })
    ElMessage.success(r.message)
    await load()
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    starting.value = false
  }
}

onMounted(async () => {
  try {
    providers.value = await providersApi.list()
    if (providers.value.length) form.value.provider_id = providers.value[0].id
  } catch (err) {
    ElMessage.error(errorMessage(err))
  }
  await load()
})
onBeforeUnmount(() => window.clearTimeout(timer))
</script>

<template>
  <div>
    <p class="muted small">
      用一个评审模型抽样检查 AI 的「分析」与「实际订单」是否一致（只看言行是否一致，不评价决策好坏）。
      建议使用与参赛选手不同、能力更强的模型做评审；已评估过的决策不会重复评估。
    </p>
    <div class="form">
      <el-select v-model="form.provider_id" placeholder="评审模型供应商" style="width: 180px">
        <el-option v-for="p in providers" :key="p.id" :label="p.name" :value="p.id" />
      </el-select>
      <el-input v-model="form.model" placeholder="模型（留空用供应商默认）" style="width: 240px" />
      <span class="muted small">抽样</span>
      <el-input-number v-model="form.sample_size" :min="1" :max="200" size="small" />
      <span class="muted small">条</span>
      <el-button type="primary" :loading="starting || result?.running" @click="run">
        {{ result?.running ? '评估中…' : '开始评估' }}
      </el-button>
    </div>
    <el-alert
      v-if="result?.message && !result.running"
      :title="result.message"
      type="info"
      :closable="false"
      class="mb"
    />

    <template v-if="result?.accounts.length">
      <h4>一致率</h4>
      <el-table :data="result.accounts" size="small" class="mb">
        <el-table-column prop="agent_name" label="选手" min-width="160" />
        <el-table-column prop="judge_model" label="评审模型" min-width="200" show-overflow-tooltip />
        <el-table-column prop="checked" label="已评估" width="80" align="right" />
        <el-table-column label="一致率" width="90" align="right">
          <template #default="{ row }">{{ formatPercent(row.rate, 0, false) }}</template>
        </el-table-column>
        <el-table-column prop="inconsistent" label="不一致" width="80" align="right" />
      </el-table>
      <p class="muted small">一致率 = 一致记 1、部分一致记 0.5、不一致记 0 的平均值。</p>

      <h4>评估明细</h4>
      <el-table :data="result.items" size="small" max-height="420">
        <el-table-column prop="data_date" label="决策日" width="105" />
        <el-table-column prop="agent_name" label="选手" width="140" />
        <el-table-column label="结论" width="90">
          <template #default="{ row }">
            <el-tag v-if="row.verdict" size="small" :type="VERDICTS[row.verdict].type">{{
              VERDICTS[row.verdict].label
            }}</el-tag>
            <el-tag v-else size="small" type="info">{{ row.status }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="理由" min-width="260" show-overflow-tooltip>
          <template #default="{ row }">{{ row.reason || row.error }}</template>
        </el-table-column>
        <el-table-column width="70">
          <template #default="{ row }">
            <el-button
              link
              type="primary"
              @click="
                router.push({ name: 'decisions', query: { account_id: row.account_id, decision: row.decision_id } })
              "
            >
              决策
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </template>
    <el-empty v-else-if="!result?.running" :image-size="60" description="还没有评估记录" />
  </div>
</template>

<style scoped>
.form {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin: 8px 0 12px;
}

.mb {
  margin-bottom: 12px;
}

.muted {
  color: var(--el-text-color-secondary);
}

.small {
  font-size: 12px;
}

h4 {
  margin: 12px 0 8px;
}
</style>
