<script setup lang="ts">
/** 每日信号与实时跟踪（PRD 3.8）：最新收盘的目标持仓（可按资金量换算股数）、实时模拟账户的表现与调仓清单。 */

import StockLink from '@/components/StockLink.vue'
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { errorMessage } from '@/api/http'
import {
  checkApi,
  portfolioApi,
  signalApi,
  type PortfolioSignal,
  type ReplayCheck,
  type Signal,
  type Tracking,
} from '@/api/quant'
import CheckPanel from '@/components/lab/CheckPanel.vue'
import LabCurve from '@/components/lab/LabCurve.vue'
import { formatMoney, formatPercent } from '@/utils/format'
import { sideClass, sideText } from '@/utils/orders'

const props = withDefaults(defineProps<{ strategyId: number; name: string; kind?: 'strategy' | 'portfolio' }>(), {
  kind: 'strategy',
})
const isPf = computed(() => props.kind === 'portfolio')
const router = useRouter()

const tracking = ref<Tracking | null>(null)
const signal = ref<Signal | null>(null)
const pfSignal = ref<PortfolioSignal | null>(null)
const capital = ref(1000000)
const loadingSignal = ref(false)
const following = ref(false)

async function loadTracking() {
  try {
    tracking.value = isPf.value
      ? await portfolioApi.tracking(props.strategyId)
      : await signalApi.tracking(props.strategyId)
  } catch (err) {
    ElMessage.error(errorMessage(err))
  }
}

async function loadSignal() {
  loadingSignal.value = true
  try {
    if (isPf.value) pfSignal.value = await portfolioApi.signal(props.strategyId, capital.value)
    else signal.value = await signalApi.signal(props.strategyId, capital.value)
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    loadingSignal.value = false
  }
}

async function follow() {
  try {
    await ElMessageBox.confirm(
      `为「${props.name}」创建实时模拟账户（本金 100 万，与买入持有基准对照），从下一个交易日开始按${isPf.value ? '各成员的' : ''}冠军版本自动交易；冠军更新后自动切换。`,
      '开始实时跟踪',
    )
    following.value = true
    if (isPf.value) await portfolioApi.follow(props.strategyId)
    else await signalApi.follow(props.strategyId)
    ElMessage.success('已开始实时跟踪')
    loadTracking()
  } catch (err) {
    if (err !== 'cancel') ElMessage.error(errorMessage(err))
  } finally {
    following.value = false
  }
}

// ---- 回放校验
const replays = ref<ReplayCheck[]>([])
const replayStart = ref('2025-01-02')
const startingReplay = ref(false)
let replayTimer: number | undefined

async function loadReplays() {
  try {
    replays.value = await checkApi.replayChecks(props.kind, props.strategyId)
  } catch (err) {
    ElMessage.error(errorMessage(err))
  }
  window.clearTimeout(replayTimer)
  if (replays.value.some((r) => r.progress?.running)) replayTimer = window.setTimeout(loadReplays, 10000)
}

async function startReplay() {
  startingReplay.value = true
  try {
    await checkApi.replayCheck(props.kind, props.strategyId, replayStart.value)
    ElMessage.success('已开始回放校验')
    loadReplays()
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    startingReplay.value = false
  }
}

onMounted(() => {
  loadTracking()
  loadSignal()
  loadReplays()
})
onBeforeUnmount(() => window.clearTimeout(replayTimer))

const curves = computed(() => {
  const t = tracking.value
  if (!t || t.nav.length < 2) return null
  return { dates: t.nav.map((n) => n.date), nav: t.nav.map((n) => n.nav), bench: t.nav.map((n) => n.bench ?? NaN) }
})
const last = computed(() => tracking.value?.nav[tracking.value.nav.length - 1] ?? null)
const cls = (v: number | null | undefined) => (v === null || v === undefined ? '' : v >= 0 ? 'sa-up' : 'sa-down')
</script>

<template>
  <div class="tracking">
    <el-card shadow="never" class="block">
      <template #header>
        <div class="card-head">
          <span>最新信号</span>
          <div class="tools">
            <span class="muted">按资金</span>
            <el-input-number v-model="capital" :min="10000" :step="100000" size="small" style="width: 150px" />
            <el-button size="small" :loading="loadingSignal" @click="loadSignal">计算</el-button>
          </div>
        </div>
      </template>
      <template v-if="signal">
        <p class="muted">
          {{ signal.date }} 收盘后，冠军 v{{ signal.version }}（{{ signal.version_title }}）的目标持仓；从
          {{ signal.eligible }} 个可选标的中选出。<span v-if="!signal.timing_on" class="warn"
            >大盘择时条件不满足，已降低仓位。</span
          >
          该策略每 {{ signal.every_days }} 个交易日调仓一次，次日开盘按目标调整。
        </p>
        <el-table :data="signal.holdings" size="small" max-height="420">
          <el-table-column label="标的" min-width="140">
            <template #default="{ row }"><StockLink :symbol="row.symbol" :name="row.name" /></template>
          </el-table-column>
          <el-table-column label="目标权重" width="100" align="right">
            <template #default="{ row }">{{ formatPercent(row.weight, 1, false) }}</template>
          </el-table-column>
          <el-table-column label="收盘价" width="100" align="right">
            <template #default="{ row }">{{ row.close.toFixed(row.close < 10 ? 3 : 2) }}</template>
          </el-table-column>
          <el-table-column label="股数（整手）" width="120" align="right">
            <template #default="{ row }">{{ row.shares?.toLocaleString() ?? '-' }}</template>
          </el-table-column>
          <el-table-column label="金额" width="130" align="right">
            <template #default="{ row }">{{ row.amount !== undefined ? formatMoney(row.amount, 0) : '-' }}</template>
          </el-table-column>
        </el-table>
        <p class="muted">
          现金 {{ formatPercent(signal.cash_weight, 1, false) }}。信号只是模拟策略的输出，不构成投资建议。
        </p>
      </template>
      <template v-else-if="pfSignal">
        <p class="muted">
          {{ pfSignal.date }} 收盘后的组合目标持仓 = 各成员权重 × 成员冠军的目标权重：
          <span v-for="m in pfSignal.members" :key="m.name" class="member">
            {{ m.name }} {{ formatPercent(m.weight, 0, false) }}（v{{ m.version }}，{{ m.holdings }} 只{{
              m.timing_on ? '' : '，择时降仓'
            }}）
          </span>
        </p>
        <el-table :data="pfSignal.holdings" size="small" max-height="420">
          <el-table-column label="标的" min-width="140">
            <template #default="{ row }"><StockLink :symbol="row.symbol" :name="row.name" /></template>
          </el-table-column>
          <el-table-column label="来自" min-width="140">
            <template #default="{ row }">{{ row.from.join('、') }}</template>
          </el-table-column>
          <el-table-column label="目标权重" width="100" align="right">
            <template #default="{ row }">{{ formatPercent(row.weight, 2, false) }}</template>
          </el-table-column>
          <el-table-column label="收盘价" width="100" align="right">
            <template #default="{ row }">{{ row.close.toFixed(row.close < 10 ? 3 : 2) }}</template>
          </el-table-column>
          <el-table-column label="股数（整手）" width="120" align="right">
            <template #default="{ row }">{{ row.shares?.toLocaleString() ?? '-' }}</template>
          </el-table-column>
          <el-table-column label="金额" width="130" align="right">
            <template #default="{ row }">{{ row.amount !== undefined ? formatMoney(row.amount, 0) : '-' }}</template>
          </el-table-column>
        </el-table>
        <p class="muted">
          现金 {{ formatPercent(pfSignal.cash_weight, 1, false) }}。信号只是模拟策略的输出，不构成投资建议。
        </p>
      </template>
      <el-skeleton v-else :rows="4" animated />
    </el-card>

    <el-card shadow="never" class="block">
      <template #header>
        <div class="card-head">
          <span>实时跟踪</span>
          <el-button
            v-if="tracking"
            size="small"
            text
            type="primary"
            @click="router.push(`/accounts/${tracking.account_id}`)"
          >
            账户详情
          </el-button>
        </div>
      </template>
      <div v-if="!tracking" class="empty">
        <p class="muted">
          回测再好也可能是过拟合。实时跟踪用之后真实发生的行情检验策略：在模拟账户中按交易规则每天执行冠军版本。
        </p>
        <el-button type="primary" :loading="following" @click="follow">开始实时跟踪</el-button>
      </div>
      <template v-else>
        <div class="stats">
          <div>
            <span class="label">开始日期</span><span class="value">{{ tracking.start_date ?? '-' }}</span>
          </div>
          <div>
            <span class="label">累计收益</span>
            <span class="value" :class="cls(last ? last.nav - 1 : null)">{{
              last ? formatPercent(last.nav - 1) : '-'
            }}</span>
          </div>
          <div>
            <span class="label">基准</span>
            <span class="value">{{ last && last.bench ? formatPercent(last.bench - 1) : '-' }}</span>
          </div>
          <div>
            <span class="label">持仓</span><span class="value">{{ tracking.positions.length }} 只</span>
          </div>
        </div>
        <LabCurve v-if="curves" :curves="curves" :height="220" :name="name" />
        <p v-else class="muted">赛道已开始，首个交易日收盘后会有净值。</p>

        <template v-if="tracking.last_decision">
          <h4>
            最近决策：{{ tracking.last_decision.data_date }} 收盘
            <el-tag v-if="tracking.last_decision.parsed?.rebalance" size="small" type="warning">调仓日</el-tag>
          </h4>
          <p class="analysis">{{ tracking.last_decision.analysis }}</p>
          <h4>调仓清单（{{ tracking.last_decision.fill_date }} 开盘执行）</h4>
          <el-table v-if="tracking.orders.length" :data="tracking.orders" size="small">
            <el-table-column label="方向" width="70">
              <template #default="{ row }"
                ><span :class="sideClass(row.side)">{{ sideText(row.side) }}</span></template
              >
            </el-table-column>
            <el-table-column label="标的" min-width="140">
              <template #default="{ row }"><StockLink :symbol="row.symbol" :name="row.name" /></template>
            </el-table-column>
            <el-table-column prop="qty" label="数量" width="100" align="right" />
            <el-table-column prop="status" label="状态" width="100" />
            <el-table-column prop="note" label="说明" min-width="160" show-overflow-tooltip />
          </el-table>
          <p v-else class="muted">没有需要执行的交易。</p>
        </template>

        <h4>当前持仓</h4>
        <el-table :data="tracking.positions" size="small" max-height="360">
          <el-table-column label="标的" min-width="140">
            <template #default="{ row }"><StockLink :symbol="row.symbol" :name="row.name" /></template>
          </el-table-column>
          <el-table-column prop="qty" label="数量" width="90" align="right" />
          <el-table-column label="权重" width="80" align="right">
            <template #default="{ row }">{{ formatPercent(row.weight, 1, false) }}</template>
          </el-table-column>
          <el-table-column label="盈亏" width="90" align="right">
            <template #default="{ row }"
              ><span :class="cls(row.pnl_pct)">{{ formatPercent(row.pnl_pct, 1) }}</span></template
            >
          </el-table-column>
        </el-table>
      </template>
    </el-card>

    <el-card v-if="tracking" shadow="never" class="block">
      <CheckPanel :account-id="tracking.account_id" title="实时跟踪：对账与健康度" />
    </el-card>

    <el-card shadow="never" class="block">
      <template #header>
        <div class="card-head">
          <span>回放校验</span>
          <div class="tools">
            <span class="muted">从</span>
            <el-date-picker
              v-model="replayStart"
              type="date"
              value-format="YYYY-MM-DD"
              size="small"
              style="width: 140px"
            />
            <el-button size="small" :loading="startingReplay" @click="startReplay">开始回放校验</el-button>
          </div>
        </div>
      </template>
      <p class="muted">
        用交易引擎把历史行情重放一遍（与实时跟踪完全相同的执行路径：订单、风控、整手、费用、分红送转），再与快速回测对账。
        不用等实时数据积累就能确认「模拟实现」本身是否一致。约 400 个交易日需要 10～30 分钟。
      </p>
      <el-collapse v-if="replays.length">
        <el-collapse-item v-for="r in replays" :key="r.track_id" :name="r.account_id">
          <template #title>
            <span class="replay-title">
              #{{ r.track_id }} 从 {{ r.start_date }} 起 ·
              <span v-if="r.progress?.running">{{ r.progress.message }}</span>
              <span v-else>{{ r.status === 'running' ? '回放完成，转为实时' : r.status }}</span>
            </span>
          </template>
          <CheckPanel v-if="!r.progress?.running" :account-id="r.account_id" title="回放校验对账" />
        </el-collapse-item>
      </el-collapse>
    </el-card>
  </div>
</template>

<style scoped>
.block {
  margin-bottom: 16px;
}

.card-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.tools {
  display: flex;
  gap: 8px;
  align-items: center;
}

.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.replay-title {
  font-size: 13px;
}

.member {
  margin-right: 10px;
}

.warn {
  color: var(--el-color-warning);
}

.stats {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 8px;
  margin-bottom: 8px;
}

.stats > div {
  display: flex;
  flex-direction: column;
}

.label {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.value {
  font-size: 16px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}

.analysis {
  white-space: pre-line;
  font-size: 13px;
}

h4 {
  margin: 12px 0 6px;
}

.empty {
  text-align: center;
  padding: 12px;
}
</style>
