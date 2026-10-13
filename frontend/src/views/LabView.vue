<script setup lang="ts">
/**
 * 策略库（PRD 3.8）：按风格展示由 AI 持续优化的策略，查看冠军版本的样本内外表现，一键优化或进入详情。
 */
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { errorMessage } from '@/api/http'
import { providersApi, type Provider } from '@/api/arena'
import { quantApi, RUN_STATUS, type DataStatus, type Seed, type Strategy, type TaskStatus } from '@/api/quant'
import { metaApi, type Universe } from '@/api/arena'
import LabCurve from '@/components/lab/LabCurve.vue'
import NewStrategyDialog from '@/components/lab/NewStrategyDialog.vue'
import { useAuthStore } from '@/stores/auth'
import type { FactorInfo } from '@/api/quant'
import { READY_TYPE } from '@/utils/readiness'
import { formatPercent } from '@/utils/format'

const router = useRouter()
const auth = useAuthStore()
const strategies = ref<Strategy[]>([])
const seedTask = ref<TaskStatus | null>(null)
const dataStatus = ref<DataStatus | null>(null)
const providers = ref<Provider[]>([])
const seeds = ref<Seed[]>([])
const factors = ref<FactorInfo[]>([])
const industries = ref<string[]>([])
const universes = ref<Universe[]>([])
const newOpen = ref(false)
const loading = ref(true)
/** 模型供应商是否已读取（读取完且一个都没有时，提示「不配 AI 也能用」） */
const providersLoaded = ref(false)
const noAiHidden = ref(false)
try {
  noAiHidden.value = localStorage.getItem('sa-hide-no-ai') === '1'
} catch {
  /* 忽略 */
}
function hideNoAi() {
  noAiHidden.value = true
  try {
    localStorage.setItem('sa-hide-no-ai', '1')
  } catch {
    /* 忽略 */
  }
}
let timer: number | undefined

const busy = computed(
  () =>
    seedTask.value?.running ||
    dataStatus.value?.task?.running ||
    dataStatus.value?.quick?.task?.running ||
    strategies.value.some((s) => s.last_run && ['pending', 'running'].includes(s.last_run.status)),
)

async function load() {
  try {
    const [list, ds] = await Promise.all([quantApi.list(), quantApi.dataStatus()])
    strategies.value = list.strategies
    seedTask.value = list.seed_task
    dataStatus.value = ds
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    loading.value = false
  }
  window.clearTimeout(timer)
  timer = window.setTimeout(load, busy.value ? 5000 : 30000)
}

onMounted(async () => {
  load()
  try {
    let opts: { industries: string[] }
    ;[seeds.value, factors.value, opts, universes.value] = await Promise.all([
      quantApi.seeds(),
      quantApi.factors(),
      quantApi.specOptions(),
      metaApi.universes(),
    ])
    industries.value = opts.industries
  } catch (err) {
    ElMessage.error(errorMessage(err))
  }
  // 模型供应商单独加载：读取失败（如密钥无法解密）时不影响新建策略的其他选项
  try {
    providers.value = await providersApi.list()
    seedForm.provider_id = providers.value[0]?.id ?? null
    seedForm.model = providers.value[0]?.default_model ?? ''
    providersLoaded.value = true
  } catch (err) {
    ElMessage.error(`读取模型供应商失败：${errorMessage(err)}`)
  }
})
onBeforeUnmount(() => window.clearTimeout(timer))

// ---------------------------------------------------------------- 数据
const coverage = computed(() => dataStatus.value?.coverage)
const dataReady = computed(() => !!coverage.value?.ready)
/** 快速体验数据（完整数据没好之前可以先用它新建策略、试算） */
const quick = computed(() => dataStatus.value?.quick)
const quickReady = computed(() => !dataReady.value && !!quick.value?.coverage?.ready && !quick.value?.task?.running)
const quickRunning = computed(() => !!quick.value?.task?.running)
const fullRunning = computed(() => !!dataStatus.value?.task?.running)
const canCreate = computed(() => dataReady.value || quickReady.value)

async function prepare() {
  try {
    await quantApi.prepare()
    ElMessage.success('已开始准备完整数据（约 20～30 分钟）')
    load()
  } catch (err) {
    ElMessage.error(errorMessage(err))
  }
}

async function quickStart() {
  try {
    await quantApi.quickStart()
    ElMessage.success('已开始准备快速体验数据（约几分钟），完成后自动在后台准备完整数据')
    load()
  } catch (err) {
    ElMessage.error(errorMessage(err))
  }
}

// ---------------------------------------------------------------- 创建风格库
const seedDialog = ref(false)
const seedForm = reactive({ provider_id: null as number | null, model: '', styles: [] as string[] })
const existingStyles = computed(() => new Set(strategies.value.map((s) => s.style)))
const missingSeeds = computed(() => seeds.value.filter((s) => !existingStyles.value.has(s.style)))

function openSeed() {
  seedForm.styles = missingSeeds.value.map((s) => s.style)
  seedDialog.value = true
}

function onProvider(id: number) {
  seedForm.model = providers.value.find((p) => p.id === id)?.default_model ?? ''
}

async function createSeeds() {
  try {
    await quantApi.seed({ provider_id: seedForm.provider_id, model: seedForm.model || null, styles: seedForm.styles })
    seedDialog.value = false
    ElMessage.success('正在回测初始策略，完成后自动显示')
    load()
  } catch (err) {
    ElMessage.error(errorMessage(err))
  }
}

// ---------------------------------------------------------------- 优化
async function optimize(s: Strategy) {
  try {
    await quantApi.research(s.id)
    ElMessage.success(`「${s.name}」开始优化`)
    load()
  } catch (err) {
    ElMessage.error(errorMessage(err))
  }
}

async function toggleAuto(s: Strategy, value: boolean) {
  try {
    await quantApi.patch(s.id, { auto_optimize: value })
    s.auto_optimize = value
  } catch (err) {
    ElMessage.error(errorMessage(err))
  }
}

const num = (v: number | null | undefined, d = 2) => (v === null || v === undefined ? '-' : v.toFixed(d))
const running = (s: Strategy) => !!s.last_run && ['pending', 'running'].includes(s.last_run.status)
const signClass = (v: number | null | undefined) => (v === null || v === undefined ? '' : v >= 0 ? 'sa-up' : 'sa-down')

// 卡片排序：按开发期夏普从高到低
const sorted = computed(() =>
  [...strategies.value].sort((a, b) => (b.champion?.score ?? -99) - (a.champion?.score ?? -99)),
)
/** 我的策略在前；别人的公共策略只读（可查看、另存） */
const sections = computed(() =>
  [
    { key: 'mine', title: '我的策略', list: sorted.value.filter((s) => s.mine) },
    {
      key: 'public',
      title: '公共策略（由管理员维护、AI 持续优化；可以查看和另存为自己的策略）',
      list: sorted.value.filter((s) => !s.mine),
    },
  ].filter((x) => x.list.length),
)
</script>

<template>
  <div v-loading="loading" class="lab">
    <div class="head">
      <div>
        <h2>策略库</h2>
        <p class="sub">
          AI
          作为策略研究员，在真实历史行情上反复回测、诊断、修改规则化策略；新版本要通过开发期的<strong>显著性检验</strong>才会替换旧版本，留出期从不参与决定、只用来检验。
        </p>
      </div>
      <div class="actions">
        <el-button type="primary" :disabled="!canCreate" @click="newOpen = true">新建策略</el-button>
        <el-button
          v-if="auth.isAdmin && !loading && missingSeeds.length"
          :disabled="!dataReady || seedTask?.running"
          @click="openSeed"
        >
          创建风格策略（{{ missingSeeds.length }}）
        </el-button>
      </div>
    </div>

    <!-- 数据状态 -->
    <el-alert
      v-if="dataStatus && (!dataReady || fullRunning)"
      :type="fullRunning || quickRunning ? 'info' : quickReady ? 'success' : 'warning'"
      :closable="false"
      class="block"
    >
      <template #title>
        <span v-if="quickRunning">正在准备快速体验数据（约几分钟）：{{ quick?.task?.message }}</span>
        <span v-else-if="fullRunning && dataReady">正在更新数据：{{ dataStatus.task?.message }}</span>
        <span v-else-if="fullRunning">正在后台准备完整数据（约 20～30 分钟）：{{ dataStatus.task?.message }}</span>
        <span v-else-if="quickReady">完整数据还没准备好</span>
        <span v-else>
          策略回测需要 {{ dataStatus.default_start.slice(0, 4) }} 年以来的行情与财报。当前数据：{{
            coverage?.with_data ?? 0
          }}
          个标的，多数从 {{ coverage?.median_first ?? '-' }} 开始。
        </span>
      </template>
      <p v-if="quickReady" class="alert-text">
        快速体验数据已就绪：可以先点「新建策略」，股票池选「快速体验」，从风格模板开始试算、创建策略。
        快速体验只用上证50 的当前成分回看历史，有幸存者偏差，回测偏乐观，不能通过「可实盘检查」； 完整的沪深300
        全时点数据准备好后，就可以创建风格策略库。
      </p>
      <p v-else-if="!dataReady && !quickRunning && !fullRunning" class="alert-text">
        <strong>快速开始</strong>：先下载上证50 + 核心 ETF（约 60
        个标的），几分钟后就能试算第一个策略，完整数据随后自动在后台准备。
        <strong>准备完整数据</strong>：直接下载沪深300 全时点股票池（含历史上被剔除、已退市的股票），约 20～30 分钟。
      </p>
      <template v-if="auth.isAdmin && !quickRunning && !fullRunning && !dataReady">
        <el-button v-if="!quickReady" size="small" type="primary" @click="quickStart">快速开始（约几分钟）</el-button>
        <el-button size="small" :type="quickReady ? 'primary' : 'default'" @click="prepare">
          准备完整数据（20～30 分钟）
        </el-button>
      </template>
      <span v-else-if="!dataReady && !quickRunning && !fullRunning" class="muted">请联系管理员准备数据。</span>
    </el-alert>
    <el-alert
      v-if="dataReady && coverage?.pit_ready === false && !fullRunning"
      type="warning"
      :closable="false"
      class="block"
      title="沪深300 的历史成分还没准备好：目前只用今天的成分回看历史，回测有幸存者偏差，这些策略不能通过「可实盘检查」"
    >
      <p class="alert-text">
        通常是连接 Baostock 失败（对方断开连接或网络受限），已经查到的月份会保留，重新准备时只补缺的部分。
      </p>
      <el-button v-if="auth.isAdmin" size="small" type="primary" @click="prepare">重新准备完整数据</el-button>
      <span v-else class="muted">请联系管理员重新准备数据。</span>
    </el-alert>
    <el-alert
      v-if="providersLoaded && !providers.length && canCreate && !noAiHidden"
      type="info"
      class="block"
      title="还没有配置 AI 模型：回测、组合、模拟交易、每日信号都可以照常使用"
      @close="hideNoAi"
    >
      <p class="alert-text">
        想让 AI 每晚优化策略、用一句话生成规则、复核交易、盯自选股，在
        <router-link to="/settings?tab=providers">配置中心 → 模型供应商</router-link>
        添加一个模型即可：便宜的云端模型（如 DeepSeek），或本机的 Ollama / LM Studio 都可以。
      </p>
    </el-alert>
    <el-alert v-if="seedTask?.running" type="info" :closable="false" class="block" :title="seedTask.message" />
    <el-alert
      v-else-if="seedTask?.message && seedTask.finished_at"
      type="success"
      :closable="true"
      class="block"
      :title="seedTask.message"
    />

    <el-empty v-if="!loading && !strategies.length" description="还没有策略">
      <el-button v-if="auth.isAdmin" type="primary" :disabled="!dataReady" @click="openSeed">
        创建 {{ seeds.length }} 种风格策略
      </el-button>
      <el-button v-else type="primary" :disabled="!canCreate" @click="newOpen = true">新建策略</el-button>
    </el-empty>

    <template v-for="sec in sections" :key="sec.key">
      <h3 v-if="sections.length > 1 || sec.key === 'public'" class="section">{{ sec.title }}</h3>
      <div class="grid">
        <el-card v-for="s in sec.list" :key="s.id" shadow="hover" class="card" @click="router.push(`/lab/${s.id}`)">
          <div class="card-head">
            <div class="title">
              <span class="name">{{ s.name }}</span>
              <el-tag v-if="s.champion" size="small" type="success">v{{ s.champion.number }}</el-tag>
              <el-tag v-if="s.is_public" size="small" type="warning" effect="plain">公共</el-tag>
              <el-tooltip v-if="s.readiness" content="「可实盘」检查结果（策略详情 → 可实盘检查）">
                <el-tag size="small" :type="READY_TYPE[s.readiness.status]" effect="plain">{{
                  s.readiness.status
                }}</el-tag>
              </el-tooltip>
            </div>
            <el-tag v-if="s.last_run && running(s)" size="small" :type="RUN_STATUS[s.last_run.status].type">
              {{ s.last_run.progress || RUN_STATUS[s.last_run.status].text }}
            </el-tag>
          </div>
          <LabCurve :curves="s.champion_curves" :height="80" mini />
          <div v-if="s.champion" class="metrics">
            <div class="metric">
              <el-tooltip content="开发期（2018～2024）夏普，升级检验在这一段上逐年进行">
                <span class="label tip">开发期夏普</span>
              </el-tooltip>
              <span class="value">{{ num(s.champion.dev.sharpe ?? s.champion.valid.sharpe) }}</span>
            </div>
            <div class="metric">
              <span class="label">留出期收益</span>
              <span class="value" :class="signClass(s.champion.holdout.cumulative_return)">
                {{ formatPercent(s.champion.holdout.cumulative_return, 1) }}
              </span>
            </div>
            <div class="metric">
              <el-tooltip :content="`按该策略至今 ${s.versions} 次尝试折算后，冠军真实夏普大于 0 的概率（DSR）`">
                <span class="label tip">折算可信度</span>
              </el-tooltip>
              <span class="value">{{ s.dsr === null ? '-' : formatPercent(s.dsr, 0, false) }}</span>
            </div>
            <div class="metric">
              <span class="label">最大回撤</span>
              <span class="value">
                {{
                  formatPercent(
                    Math.max(
                      s.champion.dev.max_drawdown ?? s.champion.train.max_drawdown ?? 0,
                      s.champion.holdout.max_drawdown ?? 0,
                    ),
                    1,
                    false,
                  )
                }}
              </span>
            </div>
          </div>
          <div class="foot" @click.stop>
            <span class="muted">{{ s.versions }} 个版本 · 升级 {{ Math.max(s.promotions - 1, 0) }} 次</span>
            <span v-if="!s.mine" class="muted">只读 · 可另存</span>
            <div v-else class="foot-actions">
              <el-tooltip content="每个交易日晚上自动优化">
                <el-switch
                  :model-value="s.auto_optimize"
                  size="small"
                  active-text="自动"
                  @change="(v: string | number | boolean) => toggleAuto(s, !!v)"
                />
              </el-tooltip>
              <el-button size="small" :disabled="running(s) || !s.provider_id" @click="optimize(s)">优化</el-button>
            </div>
          </div>
        </el-card>
      </div>
    </template>

    <p class="note">
      开发期（2018～2024）用于 AI 研究和逐年显著性检验，留出区间（2025 至今）从不参与优化、用来诚实反映样本外表现。
      股票池为全时点：历史上每一天只在当天的真实成分里选股，包括后来被剔除、已经退市的股票，不再有幸存者偏差（成分按月更新，纳入
      / 剔除日期有约一个月误差）。留出区间与实时跟踪仍是最主要的参考。所有结果均为模拟，不构成投资建议。
    </p>

    <NewStrategyDialog
      v-model="newOpen"
      :providers="providers"
      :factors="factors"
      :industries="industries"
      :seeds="seeds"
      :universes="universes"
      :full-ready="dataReady"
      :quick-ready="quickReady"
      @created="(id: number) => router.push(`/lab/${id}`)"
    />

    <el-dialog v-model="seedDialog" title="创建风格策略" width="560px">
      <el-form label-width="96px">
        <el-form-item label="风格">
          <el-checkbox-group v-model="seedForm.styles">
            <el-checkbox v-for="s in missingSeeds" :key="s.style" :value="s.style">{{ s.title }}</el-checkbox>
          </el-checkbox-group>
        </el-form-item>
        <el-form-item label="研究模型">
          <el-select
            v-model="seedForm.provider_id"
            placeholder="可以不选"
            clearable
            style="width: 200px"
            @change="onProvider"
          >
            <el-option v-for="p in providers" :key="p.id" :label="p.name" :value="p.id" />
          </el-select>
          <el-input v-model="seedForm.model" placeholder="模型" style="width: 220px; margin-left: 8px" />
        </el-form-item>
        <el-form-item>
          <div class="muted">
            每种风格先用预设的初始规则回测（训练 2018～2022、验证 2023～2024、留出 2025
            至今），之后由研究模型持续优化。不选模型也能创建、回测和跟踪，只是不会被 AI 优化；可以以后在详情页设置。
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="seedDialog = false">取消</el-button>
        <el-button type="primary" :disabled="!seedForm.styles.length" @click="createSeeds">创建</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 16px;
  margin-bottom: 12px;
}

h2 {
  margin: 0 0 4px;
}

.sub {
  margin: 0;
  color: var(--el-text-color-secondary);
  font-size: 13px;
}

.alert-text {
  margin: 4px 0 8px;
  font-size: 13px;
  line-height: 1.6;
}

.block {
  margin-bottom: 12px;
}

.section {
  margin: 20px 0 10px;
  font-size: 15px;
  font-weight: 600;
}

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 16px;
}

.card {
  cursor: pointer;
}

.card-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 6px;
  gap: 8px;
}

.title {
  display: flex;
  align-items: center;
  gap: 6px;
}

.name {
  font-weight: 600;
  font-size: 16px;
}

.metrics {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 4px;
  margin: 8px 0;
}

.metric {
  display: flex;
  flex-direction: column;
}

.label {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.tip {
  border-bottom: 1px dashed var(--el-text-color-placeholder);
  cursor: help;
}

.value {
  font-size: 15px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}

.foot {
  display: flex;
  justify-content: space-between;
  align-items: center;
  border-top: 1px solid var(--el-border-color-lighter);
  padding-top: 8px;
}

.foot-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.note {
  margin-top: 16px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>
