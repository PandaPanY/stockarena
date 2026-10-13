<script setup lang="ts">
/**
 * 多风格组合（PRD 3.9）：把资金按权重分给几条不同风格策略的冠军版本，在同一个账户中运行。
 * 风格之间相关性越低，组合的回撤与波动越小，越容易在实盘中坚持和复现。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { errorMessage } from '@/api/http'
import {
  portfolioApi,
  quantApi,
  type AllocSettings,
  type Portfolio,
  type PortfolioReport,
  type Strategy,
} from '@/api/quant'
import CapacityPanel from '@/components/lab/CapacityPanel.vue'
import ReadinessPanel from '@/components/lab/ReadinessPanel.vue'
import ReviewPanel from '@/components/lab/ReviewPanel.vue'
import TiltPanel from '@/components/lab/TiltPanel.vue'
import { READY_TYPE } from '@/utils/readiness'
import LabCurve from '@/components/lab/LabCurve.vue'
import TrackingPanel from '@/components/lab/TrackingPanel.vue'
import { formatPercent } from '@/utils/format'

const list = ref<Portfolio[]>([])
const strategies = ref<Strategy[]>([])
const route = useRoute()
const selectedId = ref<number | null>(null)
const current = ref<(Portfolio & { report: PortfolioReport }) | null>(null)
const loading = ref(false)
const tab = ref('report')

async function loadList() {
  try {
    ;[list.value, strategies.value] = await Promise.all([
      portfolioApi.list(),
      quantApi.list().then((r) => r.strategies.filter((s) => s.mine)), // 组合只能用自己的策略（公共策略先另存）
    ])
    // 从首页点进来时（?id=）直接打开对应的组合
    const want = Number(route.query.id)
    if (!selectedId.value && list.value.length) select(list.value.some((p) => p.id === want) ? want : list.value[0].id)
  } catch (err) {
    ElMessage.error(errorMessage(err))
  }
}

async function select(id: number) {
  selectedId.value = id
  loading.value = true
  try {
    current.value = await portfolioApi.get(id)
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    loading.value = false
  }
}

onMounted(loadList)

// ---------------------------------------------------------------- 新建 / 编辑
const defaultAlloc = (): AllocSettings => ({
  method: 'fixed',
  lookback: 120,
  every: 20,
  vol_target: null,
  min_exposure: 0.3,
})
const dialog = reactive({
  open: false,
  id: null as number | null,
  name: '',
  weights: {} as Record<number, number>,
  alloc: defaultAlloc(),
  volOn: false,
})

function openCreate() {
  Object.assign(dialog, { open: true, id: null, name: '多风格组合', weights: {}, alloc: defaultAlloc(), volOn: false })
}

function openEdit() {
  const c = current.value!
  Object.assign(dialog, {
    open: true,
    id: c.id,
    name: c.name,
    weights: Object.fromEntries(c.members.map((m) => [m.strategy_id, Math.round(m.weight * 100)])),
    alloc: { ...defaultAlloc(), ...(c.settings?.alloc ?? {}) },
    volOn: !!c.settings?.alloc?.vol_target,
  })
}

function toggle(id: number, on: boolean) {
  if (on) dialog.weights[id] = dialog.weights[id] ?? 25
  else delete dialog.weights[id]
}

async function save() {
  const members = Object.entries(dialog.weights).map(([k, w]) => ({ strategy_id: Number(k), weight: w }))
  const alloc = { ...dialog.alloc, vol_target: dialog.volOn ? dialog.alloc.vol_target || 0.12 : null }
  const body = { name: dialog.name, members, settings: { alloc } }
  try {
    const pf = dialog.id ? await portfolioApi.update(dialog.id, body) : await portfolioApi.create(body)
    dialog.open = false
    await loadList()
    select(pf.id)
  } catch (err) {
    ElMessage.error(errorMessage(err))
  }
}

async function remove() {
  try {
    await ElMessageBox.confirm('删除这个组合？（成员策略不受影响）', '删除组合', { type: 'warning' })
    await portfolioApi.remove(current.value!.id)
    current.value = null
    selectedId.value = null
    loadList()
  } catch (err) {
    if (err !== 'cancel') ElMessage.error(errorMessage(err))
  }
}

// ---------------------------------------------------------------- 展示
const rep = computed(() => current.value?.report)
const pct = (v: number | null | undefined, d = 1) => formatPercent(v ?? null, d)
const num = (v: number | null | undefined) => (v === null || v === undefined ? '-' : v.toFixed(2))
const cls = (v: number | null | undefined) => (v === null || v === undefined ? '' : v >= 0 ? 'sa-up' : 'sa-down')
const rows = computed(() => {
  const r = rep.value
  if (!r) return []
  const ws = [
    { key: '开发期', w: r.dev },
    { key: '留出', w: r.holdout },
  ]
  return ws.map(({ key, w }) => ({
    key,
    range: w.start ? `${w.start} ～ ${w.end}` : '-',
    ann: pct(w.annualized_return ?? w.cumulative_return),
    sharpe: num(w.sharpe),
    mdd: formatPercent(w.max_drawdown ?? null, 1, false),
    vol: formatPercent(w.volatility ?? null, 1, false),
    excess: pct(w.excess_return),
  }))
})
const corrColor = (v: number) => ({ background: `rgba(42, 120, 214, ${Math.max(0, v) * 0.6})` })
const allocLast = computed(() => rep.value?.alloc?.log?.at(-1) ?? null)
</script>

<template>
  <div class="pf">
    <div class="head">
      <div>
        <h2>多风格组合</h2>
        <p class="muted">
          把资金按权重分给几条不同风格策略的冠军版本，在同一个账户中运行（各成员按自己的周期调仓，账户整体保持目标比例）。风格之间相关性越低，组合的回撤与波动越小，也越容易在实盘中坚持与复现。
        </p>
      </div>
      <el-button type="primary" @click="openCreate">新建组合</el-button>
    </div>

    <el-empty v-if="!list.length" description="还没有组合。选 3～5 条风格不同的策略组合起来试试。" />
    <el-row v-else :gutter="16">
      <el-col :span="5">
        <el-menu :default-active="String(selectedId)" class="list">
          <el-menu-item v-for="p in list" :key="p.id" :index="String(p.id)" @click="select(p.id)">
            {{ p.name }}
            <el-tag v-if="p.readiness" size="small" :type="READY_TYPE[p.readiness.status]" class="tab-tag">
              {{ p.readiness.status }}
            </el-tag>
          </el-menu-item>
        </el-menu>
      </el-col>
      <el-col :span="19">
        <div v-loading="loading">
          <template v-if="current && rep">
            <div class="title">
              <h3>{{ current.name }}</h3>
              <div>
                <el-button size="small" @click="openEdit">编辑</el-button>
                <el-button size="small" type="danger" text @click="remove">删除</el-button>
              </div>
            </div>
            <el-tabs v-model="tab">
              <el-tab-pane label="回测报告" name="report">
                <LabCurve :curves="rep.curves" :height="300" :name="current.name" />
                <el-table :data="rows" size="small" border class="block">
                  <el-table-column prop="key" label="区间" width="80" />
                  <el-table-column prop="range" label="日期" min-width="180" />
                  <el-table-column prop="ann" label="年化（留出为累计）" width="140" align="right" />
                  <el-table-column prop="sharpe" label="夏普" width="80" align="right" />
                  <el-table-column prop="mdd" label="最大回撤" width="90" align="right" />
                  <el-table-column prop="vol" label="年化波动" width="90" align="right" />
                  <el-table-column prop="excess" label="超额" width="90" align="right" />
                </el-table>

                <h4>资金配置：{{ rep.alloc?.text ?? '固定权重' }}</h4>
                <p v-if="allocLast" class="muted">
                  最近一次（{{ allocLast.date }}）：
                  <span v-for="(n, k) in rep.alloc!.names" :key="n"
                    >{{ n }} {{ formatPercent(allocLast.weights[k], 0, false) }}；</span
                  >
                  <template v-if="rep.alloc!.vol_target"
                    >总仓位系数 {{ formatPercent(allocLast.scale, 0, false) }}</template
                  >
                  <template v-if="allocLast.note">（{{ allocLast.note }}）</template>
                </p>

                <h4>成员</h4>
                <el-table :data="rep.members" size="small">
                  <el-table-column label="策略" min-width="120">
                    <template #default="{ row }">
                      <router-link :to="`/lab/${row.strategy_id}`">{{ row.name }}</router-link>
                      <span class="muted"> v{{ row.version }}</span>
                    </template>
                  </el-table-column>
                  <el-table-column label="权重" width="70" align="right">
                    <template #default="{ row }">{{ formatPercent(row.weight, 0, false) }}</template>
                  </el-table-column>
                  <el-table-column label="开发期年化" width="100" align="right">
                    <template #default="{ row }"
                      ><span :class="cls(row.dev.annualized_return)">{{
                        pct(row.dev.annualized_return)
                      }}</span></template
                    >
                  </el-table-column>
                  <el-table-column label="开发期夏普" width="100" align="right">
                    <template #default="{ row }">{{ num(row.dev.sharpe) }}</template>
                  </el-table-column>
                  <el-table-column label="开发期回撤" width="100" align="right">
                    <template #default="{ row }">{{ formatPercent(row.dev.max_drawdown, 1, false) }}</template>
                  </el-table-column>
                  <el-table-column label="留出收益" width="100" align="right">
                    <template #default="{ row }"
                      ><span :class="cls(row.holdout.cumulative_return)">{{
                        pct(row.holdout.cumulative_return)
                      }}</span></template
                    >
                  </el-table-column>
                </el-table>

                <h4>成员相关性（开发期月度收益，平均 {{ rep.avg_correlation?.toFixed(2) ?? '-' }}）</h4>
                <table class="corr">
                  <tr>
                    <th />
                    <th v-for="n in rep.correlation.names" :key="n">{{ n }}</th>
                  </tr>
                  <tr v-for="(row, i) in rep.correlation.matrix" :key="i">
                    <th>{{ rep.correlation.names[i] }}</th>
                    <td v-for="(v, j) in row" :key="j" :style="corrColor(v)">{{ v.toFixed(2) }}</td>
                  </tr>
                </table>
                <p class="muted">相关性越低（越接近 0 甚至为负），组合分散风险的效果越好。</p>
              </el-tab-pane>
              <el-tab-pane label="资金容量" name="capacity" lazy>
                <CapacityPanel :id="current.id" :key="current.id" kind="portfolio" />
              </el-tab-pane>
              <el-tab-pane label="信号与实时跟踪" name="signal" lazy>
                <TrackingPanel :key="current.id" :strategy-id="current.id" :name="current.name" kind="portfolio" />
              </el-tab-pane>
              <el-tab-pane label="可实盘检查" name="readiness" lazy>
                <ReadinessPanel :id="current.id" :key="current.id" kind="portfolio" />
              </el-tab-pane>
              <el-tab-pane label="AI 复核" name="review" lazy>
                <ReviewPanel :id="current.id" :key="current.id" :tracking="!!current.follow_account_id" />
              </el-tab-pane>
              <el-tab-pane label="自选倾斜" name="tilt" lazy>
                <TiltPanel :id="current.id" :key="current.id" :tracking="!!current.follow_account_id" />
              </el-tab-pane>
            </el-tabs>
          </template>
        </div>
      </el-col>
    </el-row>

    <el-dialog v-model="dialog.open" :title="dialog.id ? '编辑组合' : '新建组合'" width="680px">
      <el-form label-width="80px">
        <el-form-item label="名称"><el-input v-model="dialog.name" /></el-form-item>
        <el-form-item label="资金配置">
          <div class="alloc">
            <el-radio-group v-model="dialog.alloc.method" size="small">
              <el-radio-button value="fixed">固定权重</el-radio-button>
              <el-radio-button value="inverse_vol">波动率倒数</el-radio-button>
              <el-radio-button value="erc">风险平价</el-radio-button>
            </el-radio-group>
            <div v-if="dialog.alloc.method !== 'fixed'" class="muted">
              按成员过去
              <el-input-number v-model="dialog.alloc.lookback" :min="20" :max="500" :step="20" size="small" />
              个交易日的收益估计，每
              <el-input-number v-model="dialog.alloc.every" :min="1" :max="120" size="small" />
              个交易日更新；成员权重作为风险预算
            </div>
            <div>
              <el-checkbox v-model="dialog.volOn">目标波动</el-checkbox>
              <template v-if="dialog.volOn">
                <el-input-number
                  :model-value="Math.round((dialog.alloc.vol_target ?? 0.12) * 100)"
                  :min="2"
                  :max="50"
                  size="small"
                  @update:model-value="(v: number | undefined) => (dialog.alloc.vol_target = (v ?? 12) / 100)"
                />
                <span class="muted"
                  >% 年化；预估波动更高时降低总仓位（不加杠杆，最低保留
                  {{ Math.round(dialog.alloc.min_exposure * 100) }}%）</span
                >
              </template>
            </div>
          </div>
        </el-form-item>
        <el-form-item label="成员">
          <div class="members">
            <div v-for="s in strategies" :key="s.id" class="member-row">
              <el-checkbox
                :model-value="s.id in dialog.weights"
                @change="(v: string | number | boolean) => toggle(s.id, !!v)"
              >
                {{ s.name }}
              </el-checkbox>
              <el-input-number
                v-if="s.id in dialog.weights"
                v-model="dialog.weights[s.id]"
                :min="1"
                :max="100"
                size="small"
              />
              <span v-if="s.id in dialog.weights" class="muted">（权重，自动归一）</span>
            </div>
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialog.open = false">取消</el-button>
        <el-button type="primary" :disabled="Object.keys(dialog.weights).length < 2" @click="save">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.head,
.title {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 16px;
}

h2,
h3 {
  margin: 0 0 4px;
}

.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.list {
  border-right: none;
}

.tab-tag {
  margin-left: 6px;
}

.block {
  margin-top: 8px;
}

h4 {
  margin: 14px 0 6px;
}

.corr {
  border-collapse: collapse;
  font-size: 12px;
}

.corr th,
.corr td {
  border: 1px solid var(--el-border-color-lighter);
  padding: 4px 8px;
  text-align: center;
  font-variant-numeric: tabular-nums;
}

.members {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.alloc {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.member-row {
  display: flex;
  gap: 8px;
  align-items: center;
}
</style>
