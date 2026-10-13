<script setup lang="ts">
/**
 * 首页：各市场指数与 A股涨跌概况、我的组合今天 vs 基准、明星股。
 * 交易时段每 30 秒刷新一次（页面在后台时不刷新）。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { errorMessage } from '@/api/http'
import { homeApi, type HomeAccount, type HomeOverview } from '@/api/home'
import { MARKET_NAMES, stocksApi, type StockHit } from '@/api/stocks'
import Sparkline from '@/components/home/Sparkline.vue'
import { formatPercent } from '@/utils/format'

const router = useRouter()
const data = ref<HomeOverview | null>(null)
const loading = ref(false)
const updatedAt = ref('')

async function load(silent = false) {
  if (!silent) loading.value = true
  try {
    data.value = await homeApi.overview()
    updatedAt.value = new Date().toLocaleTimeString('zh-CN', { hour12: false })
  } catch (err) {
    if (!silent) ElMessage.error(errorMessage(err))
  } finally {
    loading.value = false
  }
}

onMounted(() => load())
const timer = setInterval(() => {
  if (document.visibilityState === 'visible') void load(true)
}, 30000)
onBeforeUnmount(() => clearInterval(timer))

const cls = (v: number | null | undefined) => (v == null ? '' : v >= 0 ? 'sa-up' : 'sa-down')

function fmtPrice(v: number | null) {
  if (v == null) return '-'
  return v >= 1000 ? v.toLocaleString('zh-CN', { maximumFractionDigits: 2 }) : v.toFixed(v < 10 ? 3 : 2)
}

function fmtTime(t: string | null | undefined) {
  if (!t) return ''
  const m = t.match(/^(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})/)
  return m
    ? `${m[2]}-${m[3]} ${m[4]}:${m[5]}`
    : t
        .replace(/^\d{4}[-/]/, '')
        .replace(/\//g, '-')
        .slice(0, 11)
}

function yi(v: number | null | undefined) {
  if (!v) return '-'
  return v >= 1e12 ? `${(v / 1e12).toFixed(2)} 万亿` : `${(v / 1e8).toFixed(0)} 亿`
}

const breadth = computed(() => data.value?.markets.find((m) => m.market === 'CN')?.breadth ?? null)
const upShare = computed(() => {
  const b = breadth.value
  if (!b?.up || !b.down) return 50
  return Math.round((b.up / (b.up + b.down + (b.flat ?? 0))) * 100)
})

function excess(a: number | null | undefined, b: number | null | undefined) {
  return a == null || b == null ? null : a - b
}

interface Row extends HomeAccount {
  group: string
  groupKind: string
  groupId: number
  first: boolean
  span: number
}
const rows = computed<Row[]>(() =>
  (data.value?.portfolios ?? []).flatMap((p) =>
    p.accounts.map((a, i) => ({
      ...a,
      group: p.name,
      groupKind: p.kind,
      groupId: p.id,
      first: i === 0,
      span: p.accounts.length,
    })),
  ),
)
function spanMethod({ row, columnIndex }: { row: Row; columnIndex: number }) {
  if (columnIndex !== 0) return [1, 1]
  return row.first ? [row.span, 1] : [0, 0]
}
function openGroup(r: Row) {
  void router.push(r.groupKind === 'portfolio' ? `/portfolios?id=${r.groupId}` : `/lab/${r.groupId}`)
}

// ---- 编辑明星股 ----
const editing = ref(false)
const draft = ref<{ symbol: string; name: string }[]>([])
const adding = ref('')
const saving = ref(false)

function openEdit() {
  draft.value = (data.value?.stars ?? []).map((s) => ({ symbol: s.symbol, name: s.name }))
  editing.value = true
}

async function suggest(q: string, cb: (items: (StockHit & { value: string })[]) => void) {
  if (!q.trim()) return cb([])
  try {
    cb((await stocksApi.search(q)).filter((h) => h.type !== 'index').map((h) => ({ ...h, value: h.symbol })))
  } catch {
    cb([])
  }
}

function addStar(item: Record<string, unknown>) {
  const sym = item.symbol as string
  if (!draft.value.some((d) => d.symbol === sym)) draft.value.push({ symbol: sym, name: item.name as string })
  adding.value = ''
}

function move(i: number, d: number) {
  const j = i + d
  if (j < 0 || j >= draft.value.length) return
  const arr = draft.value
  ;[arr[i], arr[j]] = [arr[j], arr[i]]
}

async function saveStars() {
  saving.value = true
  try {
    await homeApi.setStars(draft.value.map((d) => d.symbol))
    editing.value = false
    await load(true)
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <div v-loading="loading && !data" class="home">
    <div class="page-head">
      <h2>市场概览</h2>
      <span class="muted small">{{ updatedAt ? `更新于 ${updatedAt}，交易时段每 30 秒自动刷新` : '' }}</span>
    </div>

    <template v-if="data">
      <!-- 各市场指数 -->
      <el-row :gutter="16">
        <el-col v-for="m in data.markets" :key="m.market" :xs="24" :md="12" :xl="8">
          <el-card shadow="never" class="card market">
            <template #header>
              <div class="card-head">
                <b>{{ m.name }}</b>
                <span class="muted small">{{ fmtTime(m.indices[0]?.time) }}</span>
              </div>
            </template>
            <div v-for="i in m.indices" :key="i.symbol" class="idx" @click="router.push(`/stock/${i.symbol}`)">
              <span class="idx-name">{{ i.name }}</span>
              <span class="idx-price">{{ fmtPrice(i.price) }}</span>
              <span :class="['idx-chg', cls(i.change_pct)]">{{ formatPercent(i.change_pct, 2) }}</span>
              <Sparkline :values="i.spark" :width="72" />
            </div>
            <div v-if="m.market === 'CN'" class="breadth">
              <template v-if="breadth">
                <div class="bar">
                  <div class="up" :style="{ width: `${upShare}%` }" />
                </div>
                <div class="breadth-nums small">
                  <span class="sa-up">涨 {{ breadth.up ?? '-' }}</span>
                  <span class="muted">平 {{ breadth.flat ?? '-' }}</span>
                  <span class="sa-down">跌 {{ breadth.down ?? '-' }}</span>
                </div>
                <div class="breadth-nums small">
                  <span
                    >涨停 <b class="sa-up">{{ breadth.limit_up ?? '-' }}</b></span
                  >
                  <span
                    >跌停 <b class="sa-down">{{ breadth.limit_down ?? '-' }}</b></span
                  >
                  <span
                    >成交额 <b>{{ yi(m.turnover) }}</b></span
                  >
                </div>
              </template>
              <span v-else class="muted small">涨跌家数暂时获取不到</span>
            </div>
          </el-card>
        </el-col>
      </el-row>

      <!-- 我的组合 -->
      <el-card shadow="never" class="card">
        <template #header>
          <div class="card-head">
            <b>我的组合：今天 vs 基准</b>
            <span class="muted small">收盘后按净值记录；盘中为按持仓和实时价的估算，仅供参考</span>
          </div>
        </template>
        <el-table
          v-if="rows.length"
          :data="rows"
          size="small"
          :span-method="spanMethod"
          class="pf-table"
          row-key="account_id"
        >
          <el-table-column label="组合 / 策略" min-width="120">
            <template #default="{ row }">
              <a class="link" @click="openGroup(row)">{{ row.group }}</a>
            </template>
          </el-table-column>
          <el-table-column label="账户" min-width="120">
            <template #default="{ row }">
              <router-link :to="`/accounts/${row.account_id}`" class="link">{{ row.label }}</router-link>
            </template>
          </el-table-column>
          <el-table-column label="盘中估算" min-width="120" align="right">
            <template #default="{ row }">
              <template v-if="row.intraday">
                <div :class="['num', cls(row.intraday.est)]">{{ formatPercent(row.intraday.est, 2) }}</div>
                <div class="muted small">基准 {{ formatPercent(row.intraday.bench, 2) }}</div>
              </template>
              <span v-else class="muted">-</span>
            </template>
          </el-table-column>
          <el-table-column label="最近交易日" min-width="150" align="right">
            <template #default="{ row }">
              <template v-if="row.day != null">
                <div :class="['num', cls(row.day)]">{{ formatPercent(row.day, 2) }}</div>
                <div class="small">
                  <span class="muted">基准 {{ formatPercent(row.bench_day, 2) }} · </span>
                  <span :class="cls(excess(row.day, row.bench_day))"
                    >超额 {{ formatPercent(excess(row.day, row.bench_day), 2) }}</span
                  >
                </div>
                <div class="muted small">{{ row.date }}</div>
              </template>
              <span v-else class="muted small">{{ row.date ? `${row.date} 起，尚无完整交易日` : '尚无净值' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="跟踪以来" min-width="150" align="right">
            <template #default="{ row }">
              <template v-if="row.total != null">
                <div :class="['num', cls(row.total)]">{{ formatPercent(row.total, 2) }}</div>
                <div class="small">
                  <span class="muted">基准 {{ formatPercent(row.bench_total, 2) }} · </span>
                  <span :class="cls(excess(row.total, row.bench_total))"
                    >超额 {{ formatPercent(excess(row.total, row.bench_total), 2) }}</span
                  >
                </div>
                <div class="muted small">{{ row.since }} 起 · {{ row.benchmark_name || row.benchmark }}</div>
              </template>
              <span v-else class="muted">-</span>
            </template>
          </el-table-column>
        </el-table>
        <el-empty v-else :image-size="60" description="还没有实时跟踪的组合或策略">
          <el-button type="primary" size="small" @click="router.push('/portfolios')">去多风格组合</el-button>
        </el-empty>
      </el-card>

      <!-- 明星股 -->
      <el-card shadow="never" class="card">
        <template #header>
          <div class="card-head">
            <b>明星股</b>
            <el-button size="small" text icon="Edit" @click="openEdit">编辑</el-button>
          </div>
        </template>
        <div class="stars">
          <div v-for="s in data.stars" :key="s.symbol" class="star" @click="router.push(`/stock/${s.symbol}`)">
            <div class="star-name">{{ s.name }}</div>
            <div class="muted small">{{ s.symbol }} · {{ MARKET_NAMES[s.market] }}</div>
            <div class="star-price">
              <b>{{ fmtPrice(s.price) }}</b>
              <span :class="cls(s.change_pct)">{{ formatPercent(s.change_pct, 2) }}</span>
            </div>
          </div>
        </div>
      </el-card>
    </template>

    <el-dialog v-model="editing" title="编辑明星股" width="520px">
      <el-autocomplete
        v-model="adding"
        :fetch-suggestions="suggest"
        :debounce="250"
        :trigger-on-focus="false"
        placeholder="搜索并添加：代码或名称"
        prefix-icon="Search"
        style="width: 100%"
        @select="addStar"
      >
        <template #default="{ item }">
          {{ item.name }} <span class="muted small">{{ item.symbol }}</span>
        </template>
      </el-autocomplete>
      <div class="draft">
        <div v-for="(d, i) in draft" :key="d.symbol" class="draft-row">
          <span
            >{{ d.name }} <span class="muted small">{{ d.symbol }}</span></span
          >
          <span>
            <el-button size="small" text icon="ArrowUp" :disabled="i === 0" @click="move(i, -1)" />
            <el-button size="small" text icon="ArrowDown" :disabled="i === draft.length - 1" @click="move(i, 1)" />
            <el-button size="small" text type="danger" icon="Delete" @click="draft.splice(i, 1)" />
          </span>
        </div>
        <el-empty v-if="!draft.length" :image-size="40" description="还没有明星股" />
      </div>
      <template #footer>
        <el-button @click="editing = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="saveStars">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.page-head {
  display: flex;
  align-items: baseline;
  gap: 12px;
  margin-bottom: 12px;
}

.page-head h2 {
  margin: 0;
  font-size: 20px;
}

.card {
  margin-bottom: 16px;
}

.card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.muted {
  color: var(--el-text-color-secondary);
}

.small {
  font-size: 12px;
}

.idx {
  display: grid;
  grid-template-columns: minmax(56px, 1fr) auto 64px 72px;
  align-items: center;
  gap: 10px;
  padding: 6px 4px;
  border-radius: 4px;
  cursor: pointer;
}

.idx:hover {
  background: var(--el-fill-color-light);
}

.idx-name {
  font-size: 14px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.idx-price {
  font-variant-numeric: tabular-nums;
  text-align: right;
}

.idx-chg {
  text-align: right;
  font-variant-numeric: tabular-nums;
}

.breadth {
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px dashed var(--el-border-color-lighter);
}

.bar {
  height: 8px;
  border-radius: 4px;
  background: var(--sa-down);
  overflow: hidden;
}

.bar .up {
  height: 100%;
  background: var(--sa-up);
}

.breadth-nums {
  display: flex;
  justify-content: space-between;
  margin-top: 6px;
}

.num {
  font-size: 15px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}

.link {
  color: var(--el-color-primary);
  cursor: pointer;
  text-decoration: none;
}

.stars {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
  gap: 10px;
}

.star {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  padding: 8px 10px;
  cursor: pointer;
}

.star:hover {
  border-color: var(--el-color-primary-light-5);
  background: var(--el-fill-color-lighter);
}

.star-name {
  font-weight: 600;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.star-price {
  display: flex;
  justify-content: space-between;
  margin-top: 4px;
  font-variant-numeric: tabular-nums;
}

.draft {
  margin-top: 12px;
  max-height: 360px;
  overflow-y: auto;
}

.draft-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 4px 0;
  border-bottom: 1px solid var(--el-border-color-extra-light);
}
</style>
