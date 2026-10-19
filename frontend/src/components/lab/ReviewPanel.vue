<script setup lang="ts">
/**
 * AI 交易复核：规则账户与「规则 + AI 复核」账户并行，唯一区别是 AI 可以否决新买入。
 * 显示两条净值、每次复核的结论，以及被否决 / 放行的股票之后的表现（判断 AI 是加分还是减分）。
 */
import StockLink from '@/components/StockLink.vue'
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { errorMessage } from '@/api/http'
import { providersApi, type Provider } from '@/api/arena'
import { reviewApi, type ReviewEvaluation, type ReviewReport } from '@/api/quant'
import LabCurve from '@/components/lab/LabCurve.vue'
import { formatDateTime, formatPercent } from '@/utils/format'

const props = defineProps<{ id: number; tracking: boolean }>()

const rep = ref<ReviewReport | null>(null)
const evaluation = ref<ReviewEvaluation | null>(null)
const providers = ref<Provider[]>([])
const providerId = ref<number | null>(null)
const loading = ref(false)
const saving = ref(false)
const onlyVeto = ref(false)

async function load() {
  loading.value = true
  try {
    ;[rep.value, providers.value] = await Promise.all([reviewApi.get(props.id), providersApi.list()])
    providerId.value = rep.value.settings.provider_id ?? providers.value[0]?.id ?? null
    // 评估进度单独加载，失败不影响页面其他部分
    reviewApi
      .evaluation(props.id)
      .then((e) => (evaluation.value = e))
      .catch(() => (evaluation.value = null))
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    loading.value = false
  }
}
onMounted(load)

async function enable() {
  if (!providerId.value) return
  saving.value = true
  try {
    await reviewApi.enable(props.id, providerId.value)
    ElMessage.success('已开启：AI 复核账户从下一个交易日收盘后开始决策')
    await load()
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    saving.value = false
  }
}

async function disable() {
  try {
    await ElMessageBox.confirm('停止后 AI 复核账户不再交易（已有记录保留），之后可以重新开启。', '停止 AI 复核', {
      type: 'warning',
    })
    await reviewApi.disable(props.id)
    await load()
  } catch (err) {
    if (err !== 'cancel') ElMessage.error(errorMessage(err))
  }
}

// ---------------------------------------------------------------- 更换复核模型（原模型失效等）
const change = reactive({ open: false, providerId: null as number | null, model: '', reason: '', saving: false })

function openChange() {
  Object.assign(change, {
    open: true,
    providerId: providers.value.find((p) => p.id !== rep.value?.settings.provider_id)?.id ?? null,
    model: '',
    reason: '',
    saving: false,
  })
}

async function submitChange() {
  if (!change.providerId) return ElMessage.warning('请选择模型供应商')
  if (!change.reason.trim()) return ElMessage.warning('请写明更换原因（会记入评估的变更记录）')
  change.saving = true
  try {
    await reviewApi.changeModel(props.id, {
      provider_id: change.providerId,
      model: change.model.trim() || null,
      reason: change.reason.trim(),
    })
    change.open = false
    ElMessage.success('已更换：从下一次决策起用新模型复核')
    await load()
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    change.saving = false
  }
}

const cur = computed(() => evaluation.value?.current ?? null)
const changes = computed(() => rep.value?.settings.changes ?? [])
const nameOf = (id: number | null | undefined) =>
  providers.value.find((p) => p.id === id)?.name ?? `供应商 ${id ?? '-'}`

const curves = computed(() => {
  const c = rep.value?.curve
  if (!c?.length) return null
  return { dates: c.map((x) => x.date), nav: c.map((x) => x.ai), bench: c.map((x) => x.rule) }
})
const rows = computed(() => (rep.value?.reviews ?? []).filter((x) => !onlyVeto.value || x.decision === 'veto'))
const providerName = computed(() => providers.value.find((p) => p.id === rep.value?.settings.provider_id)?.name ?? '-')
</script>

<template>
  <div v-loading="loading">
    <el-alert type="info" :closable="false" class="intro">
      <template #title>规则 + AI 复核：两个账户并行，唯一的区别是 AI 的判断</template>
      AI 复核账户与规则账户在同一个赛道中运行、本金相同、调仓时间与目标完全相同；每次调仓前，AI
      逐只检查将要<b>新买入</b>的股票（最近公告、新闻、走势），只在发现明确的重大风险时否决，被否决的那部分资金留作现金。
      卖出与其他调整和规则一致；AI 调用失败时按规则执行。两条净值的差就是 AI 带来的加分或减分。
    </el-alert>

    <template v-if="rep && !rep.enabled">
      <p v-if="!tracking" class="muted">请先在「信号与实时跟踪」中开始实时跟踪，AI 复核账户会加入同一个赛道。</p>
      <div class="toolbar">
        <span>复核模型</span>
        <el-select v-model="providerId" size="small" style="width: 220px">
          <el-option v-for="p in providers" :key="p.id" :label="`${p.name}（${p.default_model}）`" :value="p.id" />
        </el-select>
        <el-button type="primary" size="small" :disabled="!tracking || !providerId" :loading="saving" @click="enable"
          >开启 AI 复核</el-button
        >
      </div>
      <p class="muted">
        开启后从下一个交易日收盘起决策：第一次复制规则账户当前的目标持仓并全部复核，之后与规则账户同步调仓。
        使用本地模型时，需要模型服务在每个交易日收盘后（约 15:45 起）保持运行。
      </p>
    </template>

    <template v-else-if="rep">
      <div class="toolbar">
        <el-tag type="success">进行中</el-tag>
        <span class="muted"
          >复核模型：{{ providerName }}{{ rep.settings.model ? ` / ${rep.settings.model}` : '' }}；开启于
          {{ rep.settings.since }}</span
        >
        <span class="spacer" />
        <el-button size="small" @click="load">刷新</el-button>
        <el-button size="small" @click="openChange">更换复核模型</el-button>
        <el-button size="small" type="danger" text @click="disable">停止 AI 复核</el-button>
      </div>

      <div v-if="changes.length" class="muted changes">
        复核模型变更：
        <div v-for="c in changes" :key="c.at">
          {{ formatDateTime(c.at) }}：{{ nameOf(c.from.provider_id) }}{{ c.from.model ? ` / ${c.from.model}` : '' }} →
          {{ c.to.provider_name ?? nameOf(c.to.provider_id) }}{{ c.to.model ? ` / ${c.to.model}` : '' }}（{{
            c.reason
          }}）
        </div>
      </div>

      <el-descriptions v-if="rep.summary" :column="3" border size="small" class="summary">
        <el-descriptions-item label="比较起点">{{ rep.since ?? '尚无净值' }}</el-descriptions-item>
        <el-descriptions-item label="纯规则">{{ formatPercent(rep.rule_return, 2) }}</el-descriptions-item>
        <el-descriptions-item label="规则 + AI 复核">{{ formatPercent(rep.ai_return, 2) }}</el-descriptions-item>
        <el-descriptions-item label="复核 / 否决">
          {{ rep.summary.reviewed }} 次 / {{ rep.summary.vetoed }} 次
          <span v-if="rep.summary.failed" class="muted">（另有 {{ rep.summary.failed }} 次复核失败、按规则执行）</span>
        </el-descriptions-item>
        <el-descriptions-item label="被否决股票之后的超额">
          {{ formatPercent(rep.summary.veto_after_excess, 1) }}
        </el-descriptions-item>
        <el-descriptions-item label="被放行股票之后的超额">
          {{ formatPercent(rep.summary.approve_after_excess, 1) }}
        </el-descriptions-item>
        <el-descriptions-item label="调用技能">
          {{ rep.summary.skill_calls ?? 0 }} 次（在「配置中心 → 技能」中安装，AI 按需查询资料）
        </el-descriptions-item>
      </el-descriptions>
      <p class="muted">
        「之后的超额」= 执行日开盘至今相对沪深300 的收益。被否决的股票之后明显跑输被放行的，说明 AI 否决得对；反之说明
        AI 在拖后腿。样本少时差异主要是运气，至少积累几十次否决再下结论。
      </p>

      <el-card v-if="evaluation?.applies" shadow="never" class="eval">
        <template #header>
          <b>有效性评估（预注册）</b>
          <span class="muted">
            判定标准在第一次决策之前写定（docs/prereg/2026-10-08-ai-review.md），只在两个评估时点各看一次</span
          >
        </template>
        <el-alert v-if="cur?.waiting" type="warning" :closable="false" show-icon class="wait">
          <template #title>{{ cur.message }}</template>
          <template v-if="cur.missing?.length">还没有更换的组合：{{ cur.missing.join('、') }}。</template>
          在此之前复核会失败、按规则执行，这些记录不计入评估。
        </el-alert>
        <template v-else-if="cur?.result">
          <el-descriptions :column="3" border size="small">
            <el-descriptions-item label="评估时点">
              第 {{ cur.result.look }} 个交易日（{{ cur.result.look_date }}）
            </el-descriptions-item>
            <el-descriptions-item label="结论"
              ><b>{{ cur.result.verdict }}</b></el-descriptions-item
            >
            <el-descriptions-item label="样本">
              放行 {{ cur.result.approves }} / 否决 {{ cur.result.vetoes }}
            </el-descriptions-item>
            <el-descriptions-item label="否决质量 D">
              {{ formatPercent(cur.result.d, 2) }}（p = {{ cur.result.p_upper?.toFixed(3) ?? '-' }}）
            </el-descriptions-item>
            <el-descriptions-item label="账户差 ΔR">
              {{ formatPercent(cur.result.account.delta, 2) }}
            </el-descriptions-item>
          </el-descriptions>
        </template>
        <template v-else-if="cur">
          <el-progress
            :percentage="Math.min(100, Math.round(((cur.elapsed ?? 0) / (cur.next_look ?? 120)) * 100))"
            :format="() => `${cur?.elapsed ?? 0} / ${cur?.next_look ?? 120} 个交易日`"
            :stroke-width="10"
          />
          <p class="muted">
            起点 {{ cur.start }}（第 0 个交易日）。两个组合累计复核 {{ cur.events ?? 0 }} 次、否决
            {{ cur.vetoes ?? 0 }} 次。
            <template v-for="l in cur.looks ?? []" :key="l.day">
              第 {{ l.day }} 个交易日{{ l.estimated ? '约' : '' }} {{ l.date }} 评估；
            </template>
            第一次评估需要至少 20 个满 20
            个交易日的否决事件，否则跳过、等第二次。评估时点之前不计算显著性，上面的「之后超额」只是展示，不据此做决定。
          </p>
        </template>
        <div v-for="seg in evaluation.sealed ?? []" :key="seg.start" class="sealed">
          <b>已封存：{{ seg.start }} ～ {{ seg.end ?? '-' }}</b>
          <span class="muted">{{ seg.model }}；{{ seg.reason }}</span>
          <p class="muted">
            复核 {{ seg.events }} 次、否决 {{ seg.vetoes }} 次<template v-if="seg.failed"
              >、复核失败 {{ seg.failed }} 次</template
            >。{{ seg.verdict }}；这一段的数据单独保留，不并入之后的评估。
            <template v-if="seg.veto_list.length">
              否决：{{ seg.veto_list.map((v) => `${v.date} ${v.name || v.symbol}（${v.reason}）`).join('；') }}
            </template>
          </p>
        </div>
      </el-card>

      <el-dialog v-model="change.open" title="更换复核模型" width="560px">
        <el-alert type="warning" :closable="false" show-icon class="wait">
          更换后账户不重置、继续与规则账户并行；有效性评估会把之前的数据封存、单独报告，新模型从
          <b>两个组合都换好之后的第一次决策</b>起重新计第 0
          个交易日（判定标准不变）。请尽量让两个组合用同一个模型、同一天更换。
        </el-alert>
        <el-form label-width="90px" size="small">
          <el-form-item label="模型供应商">
            <el-select v-model="change.providerId" style="width: 260px">
              <el-option v-for="p in providers" :key="p.id" :label="`${p.name}（${p.default_model}）`" :value="p.id" />
            </el-select>
          </el-form-item>
          <el-form-item label="模型">
            <el-input v-model="change.model" placeholder="留空用供应商的默认模型" style="width: 260px" />
          </el-form-item>
          <el-form-item label="更换原因">
            <el-input
              v-model="change.reason"
              type="textarea"
              :rows="2"
              maxlength="500"
              placeholder="例如：原模型服务失效、无法恢复"
            />
          </el-form-item>
        </el-form>
        <template #footer>
          <el-button @click="change.open = false">取消</el-button>
          <el-button type="primary" :loading="change.saving" @click="submitChange">更换</el-button>
        </template>
      </el-dialog>

      <LabCurve v-if="curves" :curves="curves" :height="300" name="规则 + AI 复核" bench-name="纯规则" />
      <p v-else class="muted">两个账户都有净值后显示对比曲线。</p>

      <div class="toolbar">
        <h4>复核记录</h4>
        <el-checkbox v-model="onlyVeto" size="small">只看否决</el-checkbox>
      </div>
      <el-table :data="rows" size="small" max-height="460" empty-text="还没有复核记录">
        <el-table-column prop="date" label="决策日" width="100" />
        <el-table-column label="股票" width="150">
          <template #default="{ row }"><StockLink :symbol="row.symbol" :name="row.name" /></template>
        </el-table-column>
        <el-table-column label="结论" width="80">
          <template #default="{ row }">
            <el-tag v-if="row.failed" type="info" size="small">未复核</el-tag>
            <el-tag v-else :type="row.decision === 'veto' ? 'danger' : 'success'" size="small">
              {{ row.decision === 'veto' ? '否决' : '放行' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="风险" width="60" align="center">
          <template #default="{ row }">{{ row.risk ?? '-' }}</template>
        </el-table-column>
        <el-table-column prop="reason" label="理由" min-width="280" show-overflow-tooltip />
        <el-table-column label="之后超额" width="90" align="right">
          <template #default="{ row }">{{ formatPercent(row.after_excess, 1) }}</template>
        </el-table-column>
      </el-table>
    </template>
  </div>
</template>

<style scoped>
.intro {
  margin-bottom: 12px;
}

.toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 8px 0;
}

.spacer {
  flex: 1;
}

.summary {
  margin: 8px 0;
}

.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 1.8;
}

h4 {
  margin: 8px 0;
}

.eval {
  margin: 12px 0;
}

.eval .muted {
  margin-left: 8px;
}

.wait {
  margin-bottom: 8px;
}

.sealed {
  margin-top: 10px;
}

.changes {
  margin: 4px 0;
}
</style>
