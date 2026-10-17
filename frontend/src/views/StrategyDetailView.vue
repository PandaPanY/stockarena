<script setup lang="ts">
/** 策略详情（PRD 3.8）：冠军版本报告、版本历史、AI 研究记录、研究设置与手动编辑。 */
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { ElMessage, ElMessageBox } from 'element-plus'
import { errorMessage } from '@/api/http'
import { providersApi, type Provider } from '@/api/arena'
import {
  quantApi,
  RUN_STATUS,
  SOURCE_TEXT,
  STATUS_TEXT,
  STATUS_TYPE,
  type FactorInfo,
  type StrategyDetail,
  type VersionBrief,
  type VersionDetail,
} from '@/api/quant'
import CapacityPanel from '@/components/lab/CapacityPanel.vue'
import ForkDialog from '@/components/lab/ForkDialog.vue'
import PreviewPanel from '@/components/lab/PreviewPanel.vue'
import SpecEditor from '@/components/lab/SpecEditor.vue'
import { fromForm, toForm, type SpecForm } from '@/utils/spec'
import type { PreviewResult } from '@/api/quant'
import ReadinessPanel from '@/components/lab/ReadinessPanel.vue'
import { READY_TYPE } from '@/utils/readiness'
import TrackingPanel from '@/components/lab/TrackingPanel.vue'
import TimelinePanel from '@/components/lab/TimelinePanel.vue'
import TradeDetails from '@/components/lab/TradeDetails.vue'
import VersionReport from '@/components/lab/VersionReport.vue'
import { formatDateTime, formatPercent } from '@/utils/format'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const id = computed(() => Number(route.params.id))
const detail = ref<StrategyDetail | null>(null)
const tab = ref((route.query.tab as string) || 'champion')
const providers = ref<Provider[]>([])
const factors = ref<FactorInfo[]>([])
const industries = ref<string[]>([])
let timer: number | undefined

const running = computed(() => detail.value?.runs.some((r) => ['pending', 'running'].includes(r.status)) ?? false)

async function load() {
  try {
    detail.value = await quantApi.get(id.value)
  } catch (err) {
    ElMessage.error(errorMessage(err))
  }
  window.clearTimeout(timer)
  if (running.value) timer = window.setTimeout(load, 5000)
}

onMounted(async () => {
  load()
  try {
    let opts: { industries: string[] }
    ;[factors.value, opts] = await Promise.all([quantApi.factors(), quantApi.specOptions()])
    industries.value = opts.industries
  } catch (err) {
    ElMessage.error(errorMessage(err))
  }
  // 模型供应商单独加载：读取失败时不影响规则编辑
  try {
    providers.value = await providersApi.list()
  } catch (err) {
    ElMessage.error(`读取模型供应商失败：${errorMessage(err)}`)
  }
})
onBeforeUnmount(() => window.clearTimeout(timer))
watch(id, load)

const num = (v: number | null | undefined, d = 2) => (v === null || v === undefined ? '-' : v.toFixed(d))

// ---------------------------------------------------------------- 研究
const rounds = ref(5)
async function startResearch() {
  try {
    await quantApi.research(id.value, rounds.value)
    ElMessage.success('开始优化')
    tab.value = 'runs'
    load()
  } catch (err) {
    ElMessage.error(errorMessage(err))
  }
}

async function cancelRun(runId: number) {
  try {
    await quantApi.cancelRun(runId)
    ElMessage.success('将在本轮结束后停止')
    load()
  } catch (err) {
    ElMessage.error(errorMessage(err))
  }
}

// ---------------------------------------------------------------- 版本
const drawer = ref(false)
const drawerTab = ref('report')
const current = ref<VersionDetail | null>(null)
async function openVersion(v: VersionBrief) {
  try {
    current.value = await quantApi.version(v.id)
    drawerTab.value = 'report'
    drawer.value = true
  } catch (err) {
    ElMessage.error(errorMessage(err))
  }
}

async function promote(v: VersionDetail) {
  try {
    await ElMessageBox.confirm(
      `把 v${v.number} 手动设为冠军？${v.status === 'rejected' ? '该版本未通过自动升级检验：' + v.decision : ''}`,
      '设为冠军',
      { type: 'warning' },
    )
    await quantApi.promote(v.id)
    drawer.value = false
    ElMessage.success(`v${v.number} 已设为冠军`)
    load()
  } catch (err) {
    if (err !== 'cancel') ElMessage.error(errorMessage(err))
  }
}

// ---------------------------------------------------------------- 手动编辑
const editor = reactive({
  open: false,
  saving: false,
  previewing: false,
  title: '',
  rationale: '',
  base: null as number | null,
  tab: 'rules',
})
const editorSpec = ref<SpecForm>(toForm({}))
const editorRef = ref<InstanceType<typeof SpecEditor> | null>(null)
const editorPreview = ref<PreviewResult | null>(null)
function openEditor(v: VersionDetail | null) {
  const src = v ?? detail.value?.champion_detail ?? null
  editorSpec.value = toForm(src?.spec ?? {})
  editorPreview.value = null
  Object.assign(editor, { open: true, title: '', rationale: '', base: src?.number ?? null, tab: 'rules' })
}

async function previewEditor() {
  if (!editorRef.value?.sync()) return
  editor.previewing = true
  try {
    editorPreview.value = await quantApi.preview({ spec: fromForm(editorSpec.value), strategy_id: id.value })
    editor.tab = 'preview'
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    editor.previewing = false
  }
}

async function saveEditor() {
  if (!editorRef.value?.sync()) return
  const spec = fromForm(editorSpec.value)
  editor.saving = true
  try {
    const v = await quantApi.manualVersion(id.value, { spec, title: editor.title, rationale: editor.rationale })
    editor.open = false
    ElMessage.success(`已生成 v${v.number}：${v.decision}`)
    await load()
    current.value = v
    drawer.value = true
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    editor.saving = false
  }
}

// ---------------------------------------------------------------- 设置
const settings = reactive({
  open: false,
  provider_id: null as number | null,
  model: '',
  description: '',
  max_drawdown: 25,
  p_max: 0.05,
  min_holdings: 20,
  max_turnover: 20,
  candidates: 3,
  rounds: 5,
  fold_win_rate: 50,
  tune: true,
  tune_budget: 30,
})
function openSettings() {
  const d = detail.value!
  const s = d.settings_full
  Object.assign(settings, {
    open: true,
    provider_id: d.provider_id,
    model: d.model ?? '',
    description: d.description,
    max_drawdown: Math.round(s.max_drawdown * 100),
    p_max: s.p_max,
    min_holdings: s.min_holdings,
    max_turnover: s.max_turnover,
    candidates: s.candidates,
    rounds: s.rounds,
    fold_win_rate: Math.round(s.fold_win_rate * 100),
    tune: s.tune,
    tune_budget: s.tune_budget,
  })
}

async function saveSettings() {
  try {
    await quantApi.patch(id.value, {
      provider_id: settings.provider_id,
      model: settings.model || null,
      description: settings.description,
      settings: {
        max_drawdown: settings.max_drawdown / 100,
        p_max: settings.p_max,
        min_holdings: settings.min_holdings,
        max_turnover: settings.max_turnover,
        candidates: settings.candidates,
        rounds: settings.rounds,
        fold_win_rate: settings.fold_win_rate / 100,
        tune: settings.tune,
        tune_budget: settings.tune_budget,
      },
    })
    settings.open = false
    load()
  } catch (err) {
    ElMessage.error(errorMessage(err))
  }
}

async function toggleAuto(v: string | number | boolean) {
  try {
    await quantApi.patch(id.value, { auto_optimize: !!v })
    load()
  } catch (err) {
    ElMessage.error(errorMessage(err))
  }
}

async function togglePublic() {
  const d = detail.value!
  try {
    if (d.is_public) {
      await ElMessageBox.confirm('取消后其他用户看不到这条策略（他们已另存的副本不受影响）。', '取消公共', {
        type: 'warning',
      })
    }
    await quantApi.patch(id.value, { is_public: !d.is_public })
    ElMessage.success(d.is_public ? '已取消公共' : '已设为公共')
    load()
  } catch (err) {
    if (err !== 'cancel') ElMessage.error(errorMessage(err))
  }
}

async function archive() {
  try {
    await ElMessageBox.confirm('归档后策略不再显示在策略库中，也不会自动优化。', '归档策略', { type: 'warning' })
    await quantApi.patch(id.value, { status: 'archived' })
    router.push('/lab')
  } catch (err) {
    if (err !== 'cancel') ElMessage.error(errorMessage(err))
  }
}

const factorDialog = ref(false)

// ---------------------------------------------------------------- 另存为新策略
const forkOpen = ref(false)
const forkVersion = ref<VersionBrief | null>(null)
function openFork(v: VersionBrief | null) {
  forkVersion.value = v
  forkOpen.value = true
}
function onForked(newId: number) {
  drawer.value = false
  router.push(`/lab/${newId}`)
}

// ---------------------------------------------------------------- 研究笔记
const notes = reactive({ open: false, text: '' })
function openNotes() {
  notes.text = detail.value!.research_notes.map((n) => n.text).join('\n')
  notes.open = true
}

async function saveNotes() {
  try {
    await quantApi.patch(id.value, { research_notes: notes.text.split('\n') })
    notes.open = false
    load()
  } catch (err) {
    ElMessage.error(errorMessage(err))
  }
}

/** 策略体检报告：独立 HTML 页面，在新标签页打开（页面上可另存为文件分享） */
/** 导出策略文件：冠军规则、股票池、检验设置与测试历史，可以分享给别人导入 */
function exportFile() {
  if (detail.value) window.open(quantApi.exportUrl(detail.value.id), '_blank', 'noopener')
}

function openReport() {
  if (detail.value) window.open(`/api/quant/strategies/${detail.value.id}/report`, '_blank', 'noopener')
}
</script>

<template>
  <div v-if="detail" class="detail">
    <div class="head">
      <div>
        <el-button text :icon="'ArrowLeft'" @click="router.push('/lab')">策略库</el-button>
        <h2>
          {{ detail.name }}
          <el-tag v-if="detail.champion" type="success">冠军 v{{ detail.champion.number }}</el-tag>
          <el-tag v-if="detail.is_public" type="warning" effect="plain">公共</el-tag>
        </h2>
        <p class="desc">{{ detail.description }}</p>
        <p class="muted">
          <template v-if="detail.mine">
            研究模型：{{ detail.provider_name ? `${detail.provider_name} / ${detail.model ?? '默认模型'}` : '未设置' }}
            ·
          </template>
          {{ detail.versions }} 个版本 · 股票池 {{ detail.universe_name }} · 基准 {{ detail.benchmark }}
        </p>
        <p v-if="detail.origin?.kind === 'import'" class="muted">
          从策略文件导入（原策略「{{ detail.origin.strategy_name ?? '未知' }}」v{{
            detail.origin.version_number ?? '?'
          }}），继承测试历史：已检验候选 {{ detail.origin.prior_tests }} 个
        </p>
        <p v-else-if="detail.origin?.kind === 'new'" class="muted">
          新建前在对话框里试算过 {{ detail.origin.prior_tests }} 次，计入测试次数（升级检验与 DSR 都会把它们算进去）
        </p>
        <p v-else-if="detail.origin" class="muted">
          另存自
          <router-link :to="`/lab/${detail.origin.strategy_id}`">「{{ detail.origin.strategy_name }}」</router-link>
          v{{ detail.origin.version_number }}（{{ formatDateTime(detail.origin.at) }}），继承已检验的候选
          {{ detail.origin.prior_tests }} 个
        </p>
      </div>
      <div v-if="detail.mine" class="actions">
        <el-switch :model-value="detail.auto_optimize" active-text="每晚自动优化" @change="toggleAuto" />
        <el-input-number v-model="rounds" :min="1" :max="50" size="small" style="width: 100px" />
        <el-button type="primary" :disabled="running || !detail.provider_id" @click="startResearch"
          >优化 {{ rounds }} 轮</el-button
        >
        <el-dropdown trigger="click">
          <el-button :icon="'MoreFilled'" />
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item @click="openSettings">研究设置</el-dropdown-item>
              <el-dropdown-item @click="openEditor(null)">手动编辑规则</el-dropdown-item>
              <el-dropdown-item @click="openFork(null)">另存为新策略</el-dropdown-item>
              <el-dropdown-item @click="factorDialog = true">因子库</el-dropdown-item>
              <el-dropdown-item @click="openReport">策略体检报告</el-dropdown-item>
              <el-dropdown-item @click="exportFile">导出策略文件</el-dropdown-item>
              <el-dropdown-item v-if="auth.isAdmin" divided @click="togglePublic">
                {{ detail.is_public ? '取消公共（其他用户看不到）' : '设为公共（所有用户可查看、另存）' }}
              </el-dropdown-item>
              <el-dropdown-item divided @click="archive">归档策略</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </div>
      <div v-else class="actions">
        <el-button type="primary" @click="openFork(null)">另存为新策略</el-button>
        <el-button @click="factorDialog = true">因子库</el-button>
        <el-button @click="openReport">策略体检报告</el-button>
        <el-button @click="exportFile">导出策略文件</el-button>
      </div>
    </div>
    <el-alert v-if="!detail.mine" type="info" :closable="false" show-icon class="readonly">
      这是管理员维护的公共策略，只能查看。想修改规则、让 AI 优化、实时跟踪或放进组合，请先「另存为新策略」。
    </el-alert>

    <el-tabs v-model="tab">
      <el-tab-pane label="冠军版本" name="champion">
        <VersionReport v-if="detail.champion_detail" :version="detail.champion_detail" :name="detail.name" />
      </el-tab-pane>

      <el-tab-pane label="交易明细" name="trades" lazy>
        <TradeDetails v-if="detail.champion" :version-id="detail.champion.id" />
      </el-tab-pane>

      <el-tab-pane label="资金容量" name="capacity" lazy>
        <CapacityPanel kind="strategy" :id="detail.id" />
      </el-tab-pane>

      <el-tab-pane v-if="detail.mine" label="信号与实时跟踪" name="signal" lazy>
        <TrackingPanel :strategy-id="detail.id" :name="detail.name" />
      </el-tab-pane>

      <el-tab-pane v-if="detail.mine" name="readiness" lazy>
        <template #label>
          可实盘检查
          <el-tag v-if="detail.readiness" size="small" :type="READY_TYPE[detail.readiness.status]" class="tab-tag">
            {{ detail.readiness.status }}
          </el-tag>
        </template>
        <ReadinessPanel kind="strategy" :id="detail.id" />
      </el-tab-pane>

      <el-tab-pane label="优化时间线" name="timeline" lazy>
        <TimelinePanel :strategy-id="detail.id" />
      </el-tab-pane>

      <el-tab-pane :label="`版本历史（${detail.version_list.length}）`" name="versions">
        <el-table :data="detail.version_list" size="small" highlight-current-row @row-click="openVersion">
          <el-table-column label="版本" width="70">
            <template #default="{ row }">v{{ row.number }}</template>
          </el-table-column>
          <el-table-column label="状态" width="90">
            <template #default="{ row }">
              <el-tag size="small" :type="STATUS_TYPE[row.status as keyof typeof STATUS_TYPE]">
                {{ STATUS_TEXT[row.status as keyof typeof STATUS_TEXT] }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="来源" width="60">
            <template #default="{ row }">{{ SOURCE_TEXT[row.source] }}</template>
          </el-table-column>
          <el-table-column prop="title" label="名称" min-width="140" show-overflow-tooltip />
          <el-table-column label="开发期夏普" width="100" align="right">
            <template #default="{ row }">{{ num(row.dev.sharpe) }}</template>
          </el-table-column>
          <el-table-column label="开发期回撤" width="100" align="right">
            <template #default="{ row }">{{ formatPercent(row.dev.max_drawdown, 1, false) }}</template>
          </el-table-column>
          <el-table-column label="相对冠军" width="150" align="right">
            <template #default="{ row }">
              <span v-if="row.dev.vs_champion">
                {{ formatPercent(row.dev.vs_champion.excess_annual, 1) }} · p={{ row.dev.vs_champion.p.toPrecision(2) }}
              </span>
              <span v-else>-</span>
            </template>
          </el-table-column>
          <el-table-column label="留出收益" width="90" align="right">
            <template #default="{ row }">{{ formatPercent(row.holdout.cumulative_return, 1) }}</template>
          </el-table-column>
          <el-table-column prop="decision" label="检验结论" min-width="260" show-overflow-tooltip />
        </el-table>
      </el-tab-pane>

      <el-tab-pane :label="`研究记录（${detail.runs.length}）`" name="runs">
        <el-alert v-if="detail.quality" type="info" :closable="false" class="quality">
          <template #title>
            研究有效性（{{ detail.quality.versions }} 个版本）： 回测过拟合概率 PBO
            <b>{{ detail.quality.pbo === null ? '-' : formatPercent(detail.quality.pbo, 0, false) }}</b
            >； 开发期与留出期夏普的排名相关 <b>{{ detail.quality.rank_corr?.toFixed(2) ?? '-' }}</b
            >； 当前冠军以来已检验 {{ detail.quality.tests_since_champion }} 个候选 （显著性门槛 =
            {{ detail.settings_full.p_max }} ÷ 候选数）
          </template>
          PBO 接近 50% 或排名相关 ≤ 0，说明「开发期挑出来的更好版本」到了新行情里并不更好，继续优化意义不大； PBO
          明显低于 50% 且排名相关为正，说明研究找到的改进能够延续。这两项只用于评估研究过程，不参与任何挑选。
        </el-alert>
        <el-card v-if="detail.quality?.walk_forward" shadow="never" class="notes">
          <template #header>
            滚动选择检验：每年年初只用此前的数据挑「当时最好」的版本（共
            {{ detail.quality.walk_forward.versions }} 个版本）
          </template>
          <p class="muted">
            {{ detail.quality.walk_forward.n }} 年中，挑出的版本有
            {{ detail.quality.walk_forward.beat_seed }} 年好于初始版本、
            {{ detail.quality.walk_forward.beat_avg }} 年好于所有版本平均；在当年所有版本中平均排在
            {{ formatPercent(detail.quality.walk_forward.mean_percentile, 0, false) }} 分位（50%
            相当于随机挑）。累计收益：挑选
            {{ formatPercent(detail.quality.walk_forward.chosen_total, 1) }}，一直用初始版本
            {{ formatPercent(detail.quality.walk_forward.seed_total, 1) }}，所有版本平均
            {{ formatPercent(detail.quality.walk_forward.avg_total, 1) }}。 候选版本都是 AI
            看过开发期之后生成的，这里只检验「按历史表现排名」能否延续到下一年。
          </p>
          <el-table :data="detail.quality.walk_forward.years" size="small">
            <el-table-column label="年份" width="110">
              <template #default="{ row }">
                {{ row.year }}<el-tag v-if="row.holdout" size="small" type="warning" class="ml">留出</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="挑出的版本" width="100">
              <template #default="{ row }">v{{ row.chosen }}</template>
            </el-table-column>
            <el-table-column label="挑出版本收益" align="right">
              <template #default="{ row }">{{ formatPercent(row.chosen_return, 1) }}</template>
            </el-table-column>
            <el-table-column label="初始版本" align="right">
              <template #default="{ row }">{{ formatPercent(row.seed_return, 1) }}</template>
            </el-table-column>
            <el-table-column label="所有版本平均" align="right">
              <template #default="{ row }">{{ formatPercent(row.avg_return, 1) }}</template>
            </el-table-column>
            <el-table-column label="当年最好" align="right">
              <template #default="{ row }">{{ formatPercent(row.best_return, 1) }}</template>
            </el-table-column>
            <el-table-column label="排名分位" align="right">
              <template #default="{ row }">{{ formatPercent(row.percentile, 0, false) }}</template>
            </el-table-column>
          </el-table>
        </el-card>
        <el-card shadow="never" class="notes">
          <template #header>
            <div class="notes-head">
              <span>研究笔记（{{ detail.research_notes.length }}）</span>
              <el-button v-if="detail.mine" size="small" text type="primary" @click="openNotes">编辑</el-button>
            </div>
          </template>
          <p class="muted">
            AI 每轮总结的经验（哪些方向有效、哪些无效），每次优化都会提供给 AI，避免重复尝试。可以手动修改或补充。
          </p>
          <ol v-if="detail.research_notes.length" class="note-list">
            <li v-for="(n, i) in detail.research_notes" :key="i">
              {{ n.text }} <el-tag v-if="n.source === 'manual'" size="small" type="info">手动</el-tag>
              <span v-if="n.at" class="muted">{{ n.at }}</span>
            </li>
          </ol>
          <p v-else class="muted">（暂无）</p>
        </el-card>
        <el-empty v-if="!detail.runs.length" description="还没有优化过。点击右上角「优化」开始。" />
        <el-collapse>
          <el-collapse-item v-for="r in detail.runs" :key="r.id" :name="r.id">
            <template #title>
              <div class="run-title">
                <el-tag size="small" :type="RUN_STATUS[r.status].type">{{ RUN_STATUS[r.status].text }}</el-tag>
                <span
                  >#{{ r.id }} {{ r.trigger === 'schedule' ? '自动' : '手动' }} ·
                  {{ formatDateTime(r.created_at) }}</span
                >
                <span class="muted"
                  >{{ r.progress }}（{{ r.rounds_done }}/{{ r.rounds }} 轮，升级 {{ r.promoted }} 次）</span
                >
                <el-button
                  v-if="detail.mine && ['pending', 'running'].includes(r.status)"
                  size="small"
                  text
                  type="danger"
                  @click.stop="cancelRun(r.id)"
                >
                  取消
                </el-button>
              </div>
            </template>
            <el-alert v-if="r.error" type="error" :title="r.error" :closable="false" />
            <div v-for="lg in r.log" :key="lg.round" class="round">
              <div class="round-head">
                第 {{ lg.round }} 轮
                <span class="muted">
                  {{ (lg.latency_ms / 1000).toFixed(0) }} 秒 · 输入 {{ lg.tokens_in }} / 输出 {{ lg.tokens_out }} token
                </span>
                <el-tag v-if="lg.promoted" size="small" type="success">升级为 v{{ lg.promoted }}</el-tag>
              </div>
              <el-alert v-if="lg.error" type="warning" :title="lg.error" :closable="false" />
              <p v-if="lg.analysis" class="analysis">{{ lg.analysis }}</p>
              <p v-if="lg.lessons?.length" class="analysis"><strong>本轮经验：</strong>{{ lg.lessons.join('；') }}</p>
              <el-table v-if="lg.candidates?.length" :data="lg.candidates" size="small">
                <el-table-column label="候选" min-width="160">
                  <template #default="{ row }">
                    <el-link v-if="row.version_id" @click="openVersion({ id: row.version_id } as VersionBrief)"
                      >v{{ row.number }} {{ row.title }}</el-link
                    >
                    <span v-else>{{ row.title }}</span>
                  </template>
                </el-table-column>
                <el-table-column label="结果" width="80">
                  <template #default="{ row }">
                    {{
                      { champion: '升级', rejected: '淘汰', duplicate: '重复', candidate: '候选' }[
                        row.status as string
                      ] ?? row.status
                    }}
                  </template>
                </el-table-column>
                <el-table-column label="开发期夏普" width="100" align="right">
                  <template #default="{ row }">{{ num(row.dev_sharpe) }}</template>
                </el-table-column>
                <el-table-column label="相对冠军" width="160" align="right">
                  <template #default="{ row }">
                    <span v-if="row.p !== null && row.p !== undefined">
                      {{ formatPercent(row.excess, 1) }} · p={{ row.p.toFixed(2) }} · {{ row.year_wins }}
                    </span>
                    <span v-else>-</span>
                  </template>
                </el-table-column>
                <el-table-column label="程序调参" min-width="160" show-overflow-tooltip>
                  <template #default="{ row }">{{ row.tuning?.length ? row.tuning.join('；') : '-' }}</template>
                </el-table-column>
                <el-table-column prop="reason" label="原因" min-width="260" show-overflow-tooltip />
              </el-table>
            </div>
          </el-collapse-item>
        </el-collapse>
      </el-tab-pane>
    </el-tabs>

    <el-drawer v-model="drawer" size="70%" :title="current ? `v${current.number} ${current.title}` : ''">
      <template v-if="current">
        <div class="drawer-actions">
          <el-tag :type="STATUS_TYPE[current.status]">{{ STATUS_TEXT[current.status] }}</el-tag>
          <el-tag type="info">{{ SOURCE_TEXT[current.source] }}</el-tag>
          <template v-if="detail.mine">
            <el-button v-if="current.status !== 'champion'" size="small" @click="promote(current)">设为冠军</el-button>
            <el-button size="small" @click="openEditor(current)">以此为基础编辑</el-button>
          </template>
          <el-button size="small" @click="openFork(current)">另存为新策略</el-button>
        </div>
        <el-tabs v-model="drawerTab">
          <el-tab-pane label="回测报告" name="report">
            <VersionReport :version="current" />
          </el-tab-pane>
          <el-tab-pane label="交易明细" name="trades" lazy>
            <TradeDetails :version-id="current.id" />
          </el-tab-pane>
        </el-tabs>
        <h4>规格 JSON</h4>
        <pre class="json">{{ JSON.stringify(current.spec, null, 2) }}</pre>
      </template>
    </el-drawer>

    <el-dialog
      v-model="editor.open"
      :title="editor.base ? `手动编辑规则（基于 v${editor.base}）` : '手动编辑规则'"
      width="900px"
      top="4vh"
      :close-on-click-modal="false"
    >
      <el-form label-width="80px" size="small">
        <el-form-item label="名称"><el-input v-model="editor.title" placeholder="手动编辑" /></el-form-item>
        <el-form-item label="说明"><el-input v-model="editor.rationale" placeholder="改了什么、为什么" /></el-form-item>
      </el-form>
      <el-tabs v-model="editor.tab">
        <el-tab-pane label="规则" name="rules">
          <SpecEditor ref="editorRef" v-model="editorSpec" :factors="factors" :industries="industries" />
        </el-tab-pane>
        <el-tab-pane label="试算结果" name="preview" :disabled="!editorPreview">
          <PreviewPanel v-if="editorPreview" :result="editorPreview" />
        </el-tab-pane>
      </el-tabs>
      <p class="muted">
        「试算」只回测开发期、不保存，可以反复调整；「回测并保存」会生成新版本并按升级标准检验，但不会自动替换冠军，
        需要时可在版本详情中手动设为冠军。想保留原策略不动、另起一条，可以用「另存为新策略」。
      </p>
      <template #footer>
        <el-button @click="editor.open = false">取消</el-button>
        <el-button :loading="editor.previewing" @click="previewEditor">试算</el-button>
        <el-button type="primary" :loading="editor.saving" @click="saveEditor">回测并保存</el-button>
      </template>
    </el-dialog>

    <ForkDialog
      v-model="forkOpen"
      :strategy="detail"
      :version="forkVersion"
      :providers="providers"
      @created="onForked"
    />

    <el-dialog v-model="settings.open" title="研究设置" width="560px">
      <el-form label-width="120px">
        <el-form-item label="研究模型">
          <el-select v-model="settings.provider_id" style="width: 180px" clearable>
            <el-option v-for="p in providers" :key="p.id" :label="p.name" :value="p.id" />
          </el-select>
          <el-input v-model="settings.model" placeholder="默认模型" style="width: 200px; margin-left: 8px" />
        </el-form-item>
        <el-form-item label="风格说明">
          <el-input v-model="settings.description" type="textarea" :rows="3" />
        </el-form-item>
        <el-form-item label="回撤上限">
          <el-input-number v-model="settings.max_drawdown" :min="5" :max="60" /> <span class="unit">%</span>
        </el-form-item>
        <el-form-item label="显著性门槛">
          <el-input-number v-model="settings.p_max" :min="0.01" :max="1" :step="0.05" />
          <span class="unit">月度配对检验的 p 值上限（同轮 n 个候选时除以 n）</span>
        </el-form-item>
        <el-form-item label="最少持仓">
          <el-input-number v-model="settings.min_holdings" :min="0" :max="50" />
          <span class="unit">只（股票策略；只选 ETF 的不受限）</span>
        </el-form-item>
        <el-form-item label="换手上限">
          <el-input-number v-model="settings.max_turnover" :min="1" :max="100" /> <span class="unit">倍 / 年</span>
        </el-form-item>
        <el-form-item label="逐年胜率">
          <el-input-number v-model="settings.fold_win_rate" :min="0" :max="100" :step="10" />
          <span class="unit">% 的年份跑赢冠军（开发期）</span>
        </el-form-item>
        <el-form-item label="程序调参">
          <el-switch v-model="settings.tune" />
          <el-input-number
            v-model="settings.tune_budget"
            :min="5"
            :max="200"
            :disabled="!settings.tune"
            style="margin-left: 12px"
          />
          <span class="unit">次回测 / 候选（只用训练区间；实测容易过拟合，默认关闭）</span>
        </el-form-item>
        <el-form-item label="每轮候选数">
          <el-input-number v-model="settings.candidates" :min="1" :max="5" />
        </el-form-item>
        <el-form-item label="每次优化轮数">
          <el-input-number v-model="settings.rounds" :min="1" :max="50" />
        </el-form-item>
        <el-form-item>
          <span class="muted">
            检验区间：训练 {{ detail.settings_full.train_start }} ～ {{ detail.settings_full.train_end }}，验证
            {{ detail.settings_full.valid_start }} ～ {{ detail.settings_full.valid_end }}，留出
            {{ detail.settings_full.holdout_start }}
            起（创建后不能修改）。
          </span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="settings.open = false">取消</el-button>
        <el-button type="primary" @click="saveSettings">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="notes.open" title="编辑研究笔记" width="640px">
      <p class="muted">每行一条经验。会在之后的每次优化中提供给 AI。</p>
      <el-input v-model="notes.text" type="textarea" :rows="14" />
      <template #footer>
        <el-button @click="notes.open = false">取消</el-button>
        <el-button type="primary" @click="saveNotes">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="factorDialog" title="因子库" width="760px">
      <el-table :data="factors" size="small" max-height="520">
        <el-table-column prop="name" label="名称" width="130" />
        <el-table-column prop="title" label="因子" width="130" />
        <el-table-column prop="description" label="说明" min-width="220" />
        <el-table-column label="参数" min-width="160">
          <template #default="{ row }">
            <span v-for="(p, k) in row.params" :key="k" class="param"
              >{{ k }}={{ p.default }}（{{ p.min }}～{{ p.max }}）</span
            >
          </template>
        </el-table-column>
      </el-table>
    </el-dialog>
  </div>
</template>

<style scoped>
.tab-tag {
  margin-left: 6px;
}

.head {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}

h2 {
  margin: 4px 0;
  display: flex;
  align-items: center;
  gap: 8px;
}

.desc {
  margin: 4px 0;
  white-space: pre-line;
  font-size: 13px;
  max-width: 820px;
}

.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.readonly {
  margin: 4px 0 12px;
}

.actions {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
}

.quality {
  margin-bottom: 12px;
}

.notes {
  margin-bottom: 12px;
}

.ml {
  margin-left: 6px;
}

.notes-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.note-list {
  margin: 4px 0 0;
  padding-left: 20px;
  line-height: 1.8;
  font-size: 13px;
}

.run-title {
  display: flex;
  gap: 8px;
  align-items: center;
}

.round {
  margin: 8px 0 16px;
}

.round-head {
  display: flex;
  gap: 8px;
  align-items: center;
  font-weight: 600;
}

.analysis {
  white-space: pre-line;
  font-size: 13px;
  margin: 6px 0;
}

.drawer-actions {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-bottom: 8px;
}

.json {
  font-size: 12px;
  background: var(--el-fill-color-light);
  padding: 8px;
  border-radius: 4px;
  overflow: auto;
}

.mono :deep(textarea) {
  font-family: ui-monospace, Consolas, monospace;
  font-size: 12px;
}

.unit {
  margin-left: 6px;
  color: var(--el-text-color-secondary);
}

.param {
  margin-right: 8px;
  font-size: 12px;
}
</style>
