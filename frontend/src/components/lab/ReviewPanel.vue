<script setup lang="ts">
/**
 * AI 交易复核：规则账户与「规则 + AI 复核」账户并行，唯一区别是 AI 可以否决新买入。
 * 显示两条净值、每次复核的结论，以及被否决 / 放行的股票之后的表现（判断 AI 是加分还是减分）。
 */
import StockLink from '@/components/StockLink.vue'
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { errorMessage } from '@/api/http'
import { providersApi, type Provider } from '@/api/arena'
import { reviewApi, type ReviewEvaluation, type ReviewReport } from '@/api/quant'
import LabCurve from '@/components/lab/LabCurve.vue'
import { formatPercent } from '@/utils/format'

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
        <span class="muted">复核模型：{{ providerName }}；开启于 {{ rep.settings.since }}</span>
        <span class="spacer" />
        <el-button size="small" @click="load">刷新</el-button>
        <el-button size="small" type="danger" text @click="disable">停止 AI 复核</el-button>
      </div>

      <el-descriptions v-if="rep.summary" :column="3" border size="small" class="summary">
        <el-descriptions-item label="比较起点">{{ rep.since ?? '尚无净值' }}</el-descriptions-item>
        <el-descriptions-item label="纯规则">{{ formatPercent(rep.rule_return, 2) }}</el-descriptions-item>
        <el-descriptions-item label="规则 + AI 复核">{{ formatPercent(rep.ai_return, 2) }}</el-descriptions-item>
        <el-descriptions-item label="复核 / 否决">
          {{ rep.summary.reviewed }} 次 / {{ rep.summary.vetoed }} 次
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
        <template v-if="evaluation.result">
          <el-descriptions :column="3" border size="small">
            <el-descriptions-item label="评估时点">
              第 {{ evaluation.result.look }} 个交易日（{{ evaluation.result.look_date }}）
            </el-descriptions-item>
            <el-descriptions-item label="结论"
              ><b>{{ evaluation.result.verdict }}</b></el-descriptions-item
            >
            <el-descriptions-item label="样本">
              放行 {{ evaluation.result.approves }} / 否决 {{ evaluation.result.vetoes }}
            </el-descriptions-item>
            <el-descriptions-item label="否决质量 D">
              {{ formatPercent(evaluation.result.d, 2) }}（p = {{ evaluation.result.p_upper?.toFixed(3) ?? '-' }}）
            </el-descriptions-item>
            <el-descriptions-item label="账户差 ΔR">
              {{ formatPercent(evaluation.result.account.delta, 2) }}
            </el-descriptions-item>
          </el-descriptions>
        </template>
        <template v-else>
          <el-progress
            :percentage="Math.min(100, Math.round(((evaluation.elapsed ?? 0) / (evaluation.next_look ?? 120)) * 100))"
            :format="() => `${evaluation?.elapsed ?? 0} / ${evaluation?.next_look ?? 120} 个交易日`"
            :stroke-width="10"
          />
          <p class="muted">
            起点 {{ evaluation.start }}（第 0 个交易日）。两个组合累计复核 {{ evaluation.events ?? 0 }} 次、否决
            {{ evaluation.vetoes ?? 0 }} 次。
            <template v-for="l in evaluation.looks ?? []" :key="l.day">
              第 {{ l.day }} 个交易日{{ l.estimated ? '约' : '' }} {{ l.date }} 评估；
            </template>
            第一次评估需要至少 20 个满 20
            个交易日的否决事件，否则跳过、等第二次。评估时点之前不计算显著性，上面的「之后超额」只是展示，不据此做决定。
          </p>
        </template>
      </el-card>

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
            <el-tag :type="row.decision === 'veto' ? 'danger' : 'success'" size="small">
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
</style>
