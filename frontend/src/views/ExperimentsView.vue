<script setup lang="ts">
/**
 * 离线进化实验（PRD 3.7 第 3 层）：训练区间反复「回放 → 复盘 → 改策略」，每一代的策略在之后的验证区间检验。
 */
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import VChart from 'vue-echarts'
import { errorMessage } from '@/api/http'
import {
  agentsApi,
  experimentsApi,
  metaApi,
  type Agent,
  type Experiment,
  type RuleSet,
  type Universe,
} from '@/api/arena'
import { useChartTheme } from '@/composables/useChartTheme'
import { formatPercent } from '@/utils/format'

const router = useRouter()
const theme = useChartTheme()

const list = ref<Experiment[]>([])
const selectedId = ref<number | null>(null)
const agents = ref<Agent[]>([])
const universes = ref<Universe[]>([])
const rulesets = ref<RuleSet[]>([])
let timer: number | undefined

const STATUS: Record<
  Experiment['status'],
  { label: string; type: 'info' | 'primary' | 'success' | 'danger' | 'warning' }
> = {
  pending: { label: '等待', type: 'info' },
  running: { label: '运行中', type: 'primary' },
  done: { label: '已完成', type: 'success' },
  failed: { label: '失败', type: 'danger' },
  cancelled: { label: '已取消', type: 'warning' },
}

const selected = computed(() => list.value.find((e) => e.id === selectedId.value) ?? list.value[0] ?? null)

async function load() {
  try {
    list.value = await experimentsApi.list()
  } catch (err) {
    ElMessage.error(errorMessage(err))
  }
  window.clearTimeout(timer)
  if (list.value.some((e) => e.status === 'running' || e.status === 'pending')) timer = window.setTimeout(load, 10000)
}

// ---------------------------------------------------------------- 创建
const dialog = ref(false)
const saving = ref(false)
const form = reactive({
  name: '',
  agent_id: null as number | null,
  market: 'CN',
  universe_name: 'cn_sse50_etf',
  rule_set_id: null as number | null,
  initial_cash: 1000000,
  output_mode: 'targets' as 'targets' | 'orders',
  train: [] as string[],
  valid: [] as string[],
  generations: 3,
  reflect_every: 5,
  anonymize: true,
  description: '',
})
const marketUniverses = computed(() => universes.value.filter((u) => u.market === form.market))
const marketRulesets = computed(() => rulesets.value.filter((r) => r.market === form.market))
watch(
  () => form.market,
  () => {
    form.universe_name = marketUniverses.value[0]?.name ?? ''
    form.rule_set_id = marketRulesets.value[0]?.id ?? null
  },
)

async function openCreate() {
  try {
    const d = await experimentsApi.defaults()
    Object.assign(form, {
      name: `进化实验 ${new Date().toLocaleDateString('zh-CN')}`,
      train: [d.train_start, d.train_end],
      valid: [d.valid_start, d.valid_end],
    })
    if (!form.agent_id) form.agent_id = agents.value[0]?.id ?? null
    if (!form.rule_set_id) form.rule_set_id = marketRulesets.value[0]?.id ?? null
    dialog.value = true
  } catch (err) {
    ElMessage.error(errorMessage(err))
  }
}

async function create() {
  if (!form.agent_id || !form.rule_set_id || form.train.length !== 2 || form.valid.length !== 2) {
    return ElMessage.warning('请填写选手、规则集、训练区间与验证区间')
  }
  saving.value = true
  try {
    const e = await experimentsApi.create({
      name: form.name,
      agent_id: form.agent_id,
      universe_name: form.universe_name,
      rule_set_id: form.rule_set_id,
      initial_cash: String(form.initial_cash),
      output_mode: form.output_mode,
      train_start: form.train[0],
      train_end: form.train[1],
      valid_start: form.valid[0],
      valid_end: form.valid[1],
      generations: form.generations,
      reflect_every: form.reflect_every,
      anonymize: form.anonymize,
      description: form.description,
    })
    ElMessage.success('实验已开始，在后台依次回放，耗时较长')
    dialog.value = false
    selectedId.value = e.id
    await load()
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    saving.value = false
  }
}

async function cancel(e: Experiment) {
  await ElMessageBox.confirm('取消后正在回放的赛道会停止，已完成的结果保留。确定？', '取消实验', { type: 'warning' })
  try {
    await experimentsApi.cancel(e.id)
    await load()
  } catch (err) {
    ElMessage.error(errorMessage(err))
  }
}

// ---------------------------------------------------------------- 结果
const rows = computed(() =>
  (selected.value?.results ?? []).map((r) => ({
    ...r,
    label: r.generation === 0 ? '第 0 代（初始策略，对照）' : `第 ${r.generation} 代`,
  })),
)

const chartOption = computed(() => {
  const t = theme.value
  const res = selected.value?.results ?? []
  const gens = res.map((r) => (r.generation === 0 ? '初始' : `第${r.generation}代`))
  const series = [
    { name: '验证区间收益', data: res.map((r) => r.valid?.return ?? null), color: t.seriesColor(0) },
    { name: '训练区间收益', data: res.map((r) => r.train?.return ?? null), color: t.seriesColor(1) },
  ]
  return {
    backgroundColor: 'transparent',
    legend: { top: 0, textStyle: { color: t.textSecondary } },
    grid: { left: 56, right: 24, top: 36, bottom: 28 },
    tooltip: {
      trigger: 'axis',
      backgroundColor: t.surface,
      borderColor: t.grid,
      textStyle: { color: t.textPrimary },
      valueFormatter: (v: number | null) => (v === null ? '-' : formatPercent(v)),
    },
    xAxis: { type: 'category', data: gens, axisLabel: { color: t.muted }, axisLine: { lineStyle: { color: t.axis } } },
    yAxis: {
      type: 'value',
      axisLabel: { color: t.muted, formatter: (v: number) => formatPercent(v, 1) },
      splitLine: { lineStyle: { color: t.grid } },
    },
    series: series.map((s) => ({
      type: 'line',
      name: s.name,
      data: s.data,
      connectNulls: true,
      symbolSize: 7,
      lineStyle: { color: s.color, width: 2 },
      itemStyle: { color: s.color },
    })),
  }
})

const strategyDialog = ref<string | null>(null)

onMounted(async () => {
  try {
    ;[agents.value, universes.value, rulesets.value] = await Promise.all([
      agentsApi.list().then((a) => a.filter((x) => x.kind === 'llm')),
      metaApi.universes(),
      metaApi.rulesets(),
    ])
  } catch (err) {
    ElMessage.error(errorMessage(err))
  }
  await load()
})
onBeforeUnmount(() => window.clearTimeout(timer))
</script>

<template>
  <div>
    <div class="head">
      <h2 class="sa-page-title">进化实验</h2>
      <el-button type="primary" icon="Plus" @click="openCreate">新建实验</el-button>
    </div>
    <p class="muted small">
      在训练区间反复「回放 → 复盘 → 改写策略」，每一代的策略都在之后的验证区间（策略固定、不复盘）检验，并以第 0
      代（初始策略）作为对照。
      验证区间的收益才是判断进化是否有效的依据；训练区间反复做同一段行情，收益会偏高。每一代都会占用模型较长时间。
    </p>

    <el-empty v-if="!list.length" description="还没有实验" />
    <div v-else class="layout">
      <el-card shadow="never" class="side">
        <div
          v-for="e in list"
          :key="e.id"
          class="item"
          :class="{ active: selected?.id === e.id }"
          @click="selectedId = e.id"
        >
          <div class="item-head">
            <b>#{{ e.id }} {{ e.name }}</b>
            <el-tag size="small" :type="STATUS[e.status].type">{{ STATUS[e.status].label }}</el-tag>
          </div>
          <div class="muted small">{{ e.agent_name }} · {{ e.generations }} 代 · {{ e.progress }}</div>
        </div>
      </el-card>

      <div v-if="selected" class="main">
        <el-card shadow="never" class="mb">
          <template #header>
            <div class="card-header">
              <span>#{{ selected.id }} {{ selected.name }}</span>
              <el-button
                v-if="selected.status === 'running' || selected.status === 'pending'"
                size="small"
                type="danger"
                plain
                @click="cancel(selected)"
                >取消</el-button
              >
            </div>
          </template>
          <el-descriptions :column="3" size="small">
            <el-descriptions-item label="选手">{{ selected.agent_name }}</el-descriptions-item>
            <el-descriptions-item label="股票池 / 规则"
              >{{ selected.universe_name }} · {{ selected.rule_set_title }}</el-descriptions-item
            >
            <el-descriptions-item label="本金">{{
              Number(selected.initial_cash).toLocaleString()
            }}</el-descriptions-item>
            <el-descriptions-item label="训练区间"
              >{{ selected.train_start }} ～ {{ selected.train_end }}</el-descriptions-item
            >
            <el-descriptions-item label="验证区间"
              >{{ selected.valid_start }} ～ {{ selected.valid_end }}</el-descriptions-item
            >
            <el-descriptions-item label="复盘"
              >每 {{ selected.reflect_every }} 个交易日{{
                selected.anonymize ? ' · 代码匿名化' : ''
              }}</el-descriptions-item
            >
          </el-descriptions>
          <p class="muted small">进度：{{ selected.progress || '-' }}</p>
        </el-card>

        <el-card v-if="selected.results.length" shadow="never" class="mb" header="各代收益">
          <v-chart :option="chartOption" autoresize style="height: 260px" />
        </el-card>

        <el-card shadow="never" header="结果">
          <el-table :data="rows" size="small" empty-text="还没有完成的代">
            <el-table-column prop="label" label="代" min-width="160" />
            <el-table-column label="训练收益" width="100" align="right">
              <template #default="{ row }">{{ row.train ? formatPercent(row.train.return) : '—' }}</template>
            </el-table-column>
            <el-table-column label="训练复盘" width="80" align="right">
              <template #default="{ row }">{{ row.train?.reflections ?? '—' }}</template>
            </el-table-column>
            <el-table-column label="验证收益" width="100" align="right">
              <template #default="{ row }"
                ><b>{{ formatPercent(row.valid?.return) }}</b></template
              >
            </el-table-column>
            <el-table-column label="验证超额" width="100" align="right">
              <template #default="{ row }">{{ formatPercent(row.valid?.excess) }}</template>
            </el-table-column>
            <el-table-column label="验证回撤" width="90" align="right">
              <template #default="{ row }">{{ formatPercent(row.valid?.max_drawdown, 2, false) }}</template>
            </el-table-column>
            <el-table-column label="验证成交" width="80" align="right">
              <template #default="{ row }">{{ row.valid?.trades ?? '-' }}</template>
            </el-table-column>
            <el-table-column width="210" align="right">
              <template #default="{ row }">
                <el-button link type="primary" :disabled="!row.strategy" @click="strategyDialog = row.strategy || ''"
                  >策略</el-button
                >
                <el-button
                  v-if="row.train"
                  link
                  @click="router.push({ name: 'track', params: { id: row.train.track_id } })"
                  >训练赛道</el-button
                >
                <el-button link @click="router.push({ name: 'track', params: { id: row.valid.track_id } })"
                  >验证赛道</el-button
                >
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </div>
    </div>

    <el-dialog v-model="dialog" title="新建进化实验" width="640px">
      <el-form label-width="100px" @submit.prevent>
        <el-form-item label="名称"><el-input v-model="form.name" maxlength="128" /></el-form-item>
        <el-form-item label="AI 选手">
          <el-select v-model="form.agent_id" style="width: 100%">
            <el-option v-for="a in agents" :key="a.id" :label="a.name" :value="a.id" />
          </el-select>
          <div class="muted small">
            实验在每个赛道单独开启复盘，不改变选手自己的设置；第 0 代使用选手的「初始策略」（可为空）。
          </div>
        </el-form-item>
        <el-form-item label="市场">
          <el-radio-group v-model="form.market">
            <el-radio-button value="CN">A股</el-radio-button>
            <el-radio-button value="HK">港股</el-radio-button>
            <el-radio-button value="US">美股</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="股票池">
          <el-select v-model="form.universe_name" style="width: 100%">
            <el-option v-for="u in marketUniverses" :key="u.name" :label="u.title" :value="u.name" />
          </el-select>
        </el-form-item>
        <el-form-item label="规则集">
          <el-select v-model="form.rule_set_id" style="width: 100%">
            <el-option v-for="r in marketRulesets" :key="r.id" :label="r.title" :value="r.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="本金"
          ><el-input-number v-model="form.initial_cash" :min="10000" :step="100000"
        /></el-form-item>
        <el-form-item label="训练区间">
          <el-date-picker v-model="form.train" type="daterange" value-format="YYYY-MM-DD" />
        </el-form-item>
        <el-form-item label="验证区间">
          <el-date-picker v-model="form.valid" type="daterange" value-format="YYYY-MM-DD" />
          <div class="muted small">验证区间必须在训练区间之后，且不能与训练区间重叠。</div>
        </el-form-item>
        <el-form-item label="代数 / 复盘">
          <el-input-number v-model="form.generations" :min="1" :max="10" size="small" />
          <span class="muted small gap">代，每</span>
          <el-input-number v-model="form.reflect_every" :min="1" :max="60" size="small" />
          <span class="muted small gap">个交易日复盘</span>
        </el-form-item>
        <el-form-item label="代码匿名化">
          <el-switch v-model="form.anonymize" />
          <span class="muted small gap">降低模型凭记忆认出历史行情的可能（推荐）</span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialog = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="create">开始实验</el-button>
      </template>
    </el-dialog>

    <el-dialog :model-value="strategyDialog !== null" title="策略笔记" width="640px" @close="strategyDialog = null">
      <pre class="block">{{ strategyDialog || '（空）' }}</pre>
    </el-dialog>
  </div>
</template>

<style scoped>
.head {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.layout {
  display: grid;
  grid-template-columns: 280px minmax(0, 1fr);
  gap: 16px;
}

@media (max-width: 900px) {
  .layout {
    grid-template-columns: minmax(0, 1fr);
  }
}

.item {
  padding: 8px;
  border-radius: 6px;
  cursor: pointer;
}

.item.active,
.item:hover {
  background: var(--el-fill-color-light);
}

.item-head {
  display: flex;
  justify-content: space-between;
  gap: 6px;
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.mb {
  margin-bottom: 16px;
}

.muted {
  color: var(--el-text-color-secondary);
}

.small {
  font-size: 12px;
}

.gap {
  margin: 0 6px;
}

.block {
  white-space: pre-wrap;
  background: var(--el-fill-color-light);
  padding: 12px;
  border-radius: 6px;
  font-size: 13px;
  line-height: 1.7;
  max-height: 60vh;
  overflow: auto;
}
</style>
