<script setup lang="ts">
/**
 * 股票详情：行情（实时价、K 线）、估值与财务、因子排名、公告新闻，以及与平台的关联
 * （哪些跟踪账户持有、组合成员策略是否选中、AI 复核的历次结论）。
 */
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import type { KBar } from '@/api/arena'
import { errorMessage } from '@/api/http'
import {
  MARKET_NAMES,
  TYPE_NAMES,
  stocksApi,
  type FactorRanks,
  type StockDetail,
  type StockNews,
  type StockQuote,
} from '@/api/stocks'
import StockKline from '@/components/stock/StockKline.vue'
import { LEVEL_TEXT, LEVEL_TYPE, watchApi, type WatchItem } from '@/api/watch'
import { formatDateTime, formatPercent } from '@/utils/format'

const route = useRoute()
const symbol = computed(() => String(route.params.symbol || ''))

const detail = ref<StockDetail | null>(null)
const bars = ref<KBar[]>([])
const quote = ref<StockQuote | null>(null)
const ranks = ref<FactorRanks | null>(null)
const news = ref<StockNews | null>(null)
const loading = ref(false)
const barsLoading = ref(false)
const ranksLoading = ref(false)
const newsLoading = ref(false)
const error = ref('')
const freq = ref<'d' | 'w' | 'm'>('d')
const adjust = ref<'qfq' | 'hfq' | 'none'>('qfq')
const tab = ref('fin')
const watched = ref<WatchItem | null>(null)
const watchBusy = ref(false)

async function loadWatch() {
  try {
    watched.value = await watchApi.bySymbol(symbol.value)
  } catch {
    watched.value = null
  }
}

async function addWatch() {
  watchBusy.value = true
  try {
    await watchApi.add(symbol.value)
    ElMessage.success('已加入自选股：AI 每天晚上检查它的公告和新闻')
    await loadWatch()
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    watchBusy.value = false
  }
}

async function loadBars() {
  barsLoading.value = true
  try {
    bars.value = await stocksApi.bars(symbol.value, freq.value, adjust.value)
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    barsLoading.value = false
  }
}

async function loadQuote() {
  try {
    const q = await stocksApi.quote(symbol.value)
    quote.value = 'price' in q ? (q as StockQuote) : null
  } catch {
    /* 实时行情只用于展示，失败时显示最近收盘价 */
  }
}

async function loadRanks() {
  if (ranks.value || ranksLoading.value || !detail.value || detail.value.type !== 'stock') return
  ranksLoading.value = true
  try {
    ranks.value = await stocksApi.ranks(symbol.value)
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    ranksLoading.value = false
  }
}

async function loadNews() {
  if (news.value || newsLoading.value) return
  newsLoading.value = true
  try {
    news.value = await stocksApi.news(symbol.value)
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    newsLoading.value = false
  }
}

async function load() {
  loading.value = true
  error.value = ''
  detail.value = null
  ranks.value = null
  news.value = null
  bars.value = []
  quote.value = null
  watched.value = null
  try {
    // 详情会按需补齐日线，之后再取 K 线
    detail.value = await stocksApi.detail(symbol.value)
    quote.value = detail.value.quote
    const d = detail.value
    tab.value = d.type === 'stock' ? 'fin' : d.market === 'CN' ? 'ann' : 'news'
    document.title = `${detail.value.name} · StockArena`
    await loadBars()
    if (detail.value.type !== 'index') void loadWatch()
    if (tab.value === 'ranks') void loadRanks()
    if (tab.value === 'ann' || tab.value === 'news') void loadNews()
  } catch (err) {
    error.value = errorMessage(err)
  } finally {
    loading.value = false
  }
}

watch(symbol, (s) => s && load(), { immediate: true })
watch([freq, adjust], loadBars)
watch(tab, (t) => {
  if (t === 'ranks') void loadRanks()
  if (t === 'ann' || t === 'news') void loadNews()
})

// 实时价每 30 秒刷新（页面在后台时浏览器会自动降频）
const timer = setInterval(() => {
  if (detail.value && document.visibilityState === 'visible') void loadQuote()
}, 30000)
onBeforeUnmount(() => clearInterval(timer))

/** 行情时间里的日期（腾讯行情各市场格式不同：20261008161457 / 2026/10/08 16:08 / 2026-10-07 16:55） */
function quoteDate(q: StockQuote | null): string | null {
  const m = q?.time?.match(/(\d{4})[-/]?(\d{2})[-/]?(\d{2})/)
  return m ? `${m[1]}-${m[2]}-${m[3]}` : null
}

/** 日线上追加当天盘中的实时 K 线（只用于展示，不入库）；后复权价格与实时价不在同一尺度，不追加 */
const chartBars = computed<KBar[]>(() => {
  const q = quote.value
  const d = quoteDate(q)
  const last = bars.value[bars.value.length - 1]
  if (!q || !d || freq.value !== 'd' || adjust.value === 'hfq' || !last || d <= last[0]) return bars.value
  const vol = (q.volume ?? 0) * (detail.value?.market === 'CN' ? 100 : 1) // A股实时成交量单位为手
  const o = q.open ?? q.price
  return [...bars.value, [d, o, q.price, q.low ?? Math.min(o, q.price), q.high ?? Math.max(o, q.price), vol]]
})
const liveDate = computed(() => {
  const d = quoteDate(quote.value)
  const last = bars.value[bars.value.length - 1]
  return d && last && d > last[0] ? d : null
})

/** 行情时间显示为 YYYY-MM-DD HH:mm:ss（A股原始格式为 20261008161457） */
function quoteTime(t: string): string {
  const m = t.match(/^(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})$/)
  return m ? `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]}:${m[6]}` : t.replace(/\//g, '-')
}

const price = computed(() => quote.value?.price ?? detail.value?.last?.close ?? null)
const change = computed(() => quote.value?.change_pct ?? detail.value?.last?.change_pct ?? null)
const changeClass = computed(() => (change.value == null ? '' : change.value >= 0 ? 'sa-up' : 'sa-down'))
const priceDigits = computed(() => (price.value != null && price.value < 10 ? 3 : 2))

const FIN_COLS: { key: string; label: string; pct?: boolean }[] = [
  { key: 'revenue', label: '营收（亿）' },
  { key: 'revenue_yoy', label: '营收同比', pct: true },
  { key: 'net_profit', label: '归母净利（亿）' },
  { key: 'net_profit_yoy', label: '净利同比', pct: true },
  { key: 'eps', label: '每股收益' },
  { key: 'roe', label: 'ROE', pct: true },
  { key: 'gross_margin', label: '毛利率', pct: true },
  { key: 'net_margin', label: '净利率', pct: true },
  { key: 'debt_ratio', label: '负债率', pct: true },
]

function fin(v: unknown, pct?: boolean): string {
  if (typeof v !== 'number') return '-'
  return pct ? `${v.toFixed(1)}%` : v.toFixed(2)
}

function pctColor(p: number): string {
  return p >= 80 ? 'var(--el-color-primary)' : p <= 20 ? 'var(--el-color-info)' : 'var(--el-color-primary-light-3)'
}

const rel = computed(() => detail.value?.relations)
const hasRelations = computed(
  () => !!rel.value && (rel.value.holdings.length > 0 || rel.value.picks.length > 0 || rel.value.reviews.length > 0),
)
</script>

<template>
  <div v-loading="loading" class="stock-page" element-loading-text="首次打开需要下载历史行情，请稍候…">
    <el-result v-if="error" icon="warning" title="打不开这只股票" :sub-title="error" />

    <template v-else-if="detail">
      <!-- 抬头：名称、代码、实时价 -->
      <div class="head">
        <div class="title">
          <h2>{{ detail.name }}</h2>
          <span class="muted">{{ detail.symbol }}</span>
          <el-tag size="small">{{ MARKET_NAMES[detail.market] ?? detail.market }}</el-tag>
          <el-tag size="small" type="info">{{ TYPE_NAMES[detail.type] ?? detail.type }}</el-tag>
          <el-tag v-if="detail.industry" size="small" type="info">{{ detail.industry }}</el-tag>
          <el-tag v-if="detail.is_st" size="small" type="danger">ST</el-tag>
          <template v-if="detail.type !== 'index'">
            <router-link v-if="watched" to="/watch">
              <el-tag size="small" type="warning" effect="dark">自选 · 权重 {{ watched.weight }}</el-tag>
            </router-link>
            <el-button v-else size="small" icon="Star" :loading="watchBusy" @click="addWatch">加入自选</el-button>
          </template>
        </div>
        <div class="price">
          <span :class="['big', changeClass]">{{ price?.toFixed(priceDigits) ?? '-' }}</span>
          <span :class="changeClass">{{ formatPercent(change, 2) }}</span>
          <span class="muted small">
            {{ quote ? `行情 ${quoteTime(quote.time)}` : detail.last ? `收盘 ${detail.last.date}` : '' }}
            · {{ detail.currency }}
          </span>
        </div>
      </div>

      <el-row :gutter="16">
        <el-col :xs="24" :lg="17">
          <el-card shadow="never" class="card">
            <div class="toolbar">
              <el-radio-group v-model="freq" size="small">
                <el-radio-button value="d">日线</el-radio-button>
                <el-radio-button value="w">周线</el-radio-button>
                <el-radio-button value="m">月线</el-radio-button>
              </el-radio-group>
              <el-radio-group v-model="adjust" size="small">
                <el-radio-button value="qfq">前复权</el-radio-button>
                <el-radio-button value="hfq">后复权</el-radio-button>
                <el-radio-button value="none">不复权</el-radio-button>
              </el-radio-group>
              <span class="spacer" />
              <span v-if="liveDate" class="muted small">最后一根为 {{ liveDate }} 盘中实时数据</span>
            </div>
            <div v-loading="barsLoading">
              <StockKline :bars="chartBars" :live-date="liveDate" :height="540" />
            </div>
          </el-card>
        </el-col>

        <el-col :xs="24" :lg="7">
          <el-card shadow="never" class="card">
            <template #header><b>基本信息</b></template>
            <el-descriptions :column="1" size="small" border>
              <template v-if="detail.type === 'stock'">
                <el-descriptions-item label="市盈率（TTM）">{{
                  detail.valuation.pe_ttm ?? '亏损或无数据'
                }}</el-descriptions-item>
                <el-descriptions-item label="市净率">{{ detail.valuation.pb ?? '-' }}</el-descriptions-item>
                <el-descriptions-item label="股息率（近 12 个月）">
                  {{ detail.valuation.dividend_yield != null ? `${detail.valuation.dividend_yield}%` : '-' }}
                </el-descriptions-item>
                <el-descriptions-item label="总市值（约）">
                  {{ detail.valuation.market_cap != null ? `${detail.valuation.market_cap} 亿` : '-' }}
                </el-descriptions-item>
              </template>
              <el-descriptions-item label="今开 / 最高 / 最低">
                {{ quote ? `${quote.open ?? '-'} / ${quote.high ?? '-'} / ${quote.low ?? '-'}` : '-' }}
              </el-descriptions-item>
              <el-descriptions-item label="昨收">{{ quote?.prev_close ?? '-' }}</el-descriptions-item>
              <el-descriptions-item v-if="detail.type !== 'index'" label="每手"
                >{{ detail.lot_size }} 股</el-descriptions-item
              >
            </el-descriptions>
            <p v-if="detail.type === 'stock'" class="muted small note">
              市盈率用最近四个季度的每股收益；市值按财报估算股本，仅供参考。
            </p>
          </el-card>

          <el-card shadow="never" class="card">
            <template #header><b>在平台中</b></template>
            <template v-if="hasRelations && rel">
              <div v-if="rel.holdings.length" class="rel">
                <div class="rel-title">跟踪账户持有</div>
                <div v-for="h in rel.holdings" :key="h.account_id" class="rel-row">
                  <router-link :to="`/accounts/${h.account_id}`">{{ h.label }}</router-link>
                  <span class="muted">{{ h.qty }} 股，成本 {{ h.avg_cost.toFixed(2) }}</span>
                </div>
              </div>
              <div v-if="rel.picks.length" class="rel">
                <div class="rel-title">组合最近一次调仓（{{ rel.picks[0].date }} 收盘）</div>
                <div v-for="p in rel.picks" :key="p.portfolio" class="rel-row">
                  <span>{{ p.portfolio }}</span>
                  <span class="muted">
                    目标权重 {{ formatPercent(p.target, 2, false) }}
                    <template v-if="p.members.length">，由 {{ p.members.join('、') }} 选中</template>
                  </span>
                </div>
              </div>
              <div v-if="rel.reviews.length" class="rel">
                <div class="rel-title">AI 复核记录</div>
                <div v-for="(r, i) in rel.reviews.slice(0, 8)" :key="i" class="rel-row column">
                  <div>
                    <el-tag :type="r.decision === 'veto' ? 'danger' : 'success'" size="small">
                      {{ r.decision === 'veto' ? '否决' : '放行' }}
                    </el-tag>
                    <span class="muted small"> {{ r.date }} · {{ r.portfolio }} · 风险 {{ r.risk ?? '-' }}</span>
                  </div>
                  <div class="small">{{ r.reason }}</div>
                </div>
              </div>
            </template>
            <p v-else class="muted small">目前没有跟踪账户持有它，最近一次调仓也没有选中它。</p>
            <div v-if="watched?.events?.length" class="rel">
              <div class="rel-title">自选股事件（AI 整理）</div>
              <div v-for="e in watched.events.slice(0, 5)" :key="e.id" class="rel-row column">
                <div>
                  <el-tag :type="LEVEL_TYPE[e.level]" size="small">{{ LEVEL_TEXT[e.level] }}</el-tag>
                  <span class="muted small"> {{ e.date }}</span>
                </div>
                <div class="small">{{ e.title }}</div>
              </div>
            </div>
          </el-card>
        </el-col>
      </el-row>

      <el-card v-if="detail.type !== 'index'" shadow="never" class="card">
        <el-tabs v-model="tab">
          <el-tab-pane v-if="detail.type === 'stock'" label="财务" name="fin">
            <el-table :data="detail.fundamentals" size="small" empty-text="没有财报数据">
              <el-table-column prop="period" label="报告期" width="110" fixed />
              <el-table-column v-for="c in FIN_COLS" :key="c.key" :label="c.label" min-width="96" align="right">
                <template #default="{ row }">{{ fin(row[c.key], c.pct) }}</template>
              </el-table-column>
            </el-table>
            <p class="muted small note">
              策略只在「法定披露截止日」之后才使用一份财报（如一季报 4 月 30 日），避免提前用到当时还不知道的数据。
            </p>
          </el-tab-pane>

          <el-tab-pane v-if="detail.type === 'stock'" label="因子排名" name="ranks">
            <div v-loading="ranksLoading" class="ranks">
              <template v-if="ranks?.groups?.length">
                <p class="muted small">
                  {{ ranks.date }} 在股票池（{{ ranks.universe?.includes('csi800') ? '中证800' : '沪深300' }}，{{
                    ranks.peers
                  }}
                  只股票）中的分位：数值越大表示这项指标越高，<b>不代表好坏</b>（例如负债率、波动率高通常不是好事）。
                </p>
                <div v-for="g in ranks.groups" :key="g.title" class="rank-group">
                  <div class="rank-title">{{ g.title }}</div>
                  <div v-for="it in g.items" :key="it.name" class="rank-row">
                    <el-tooltip :content="it.description" placement="top">
                      <span class="rank-name">{{ it.title }}</span>
                    </el-tooltip>
                    <el-progress
                      :percentage="it.pct"
                      :stroke-width="10"
                      :color="pctColor(it.pct)"
                      :format="(p: number) => `${p.toFixed(0)}`"
                      class="rank-bar"
                    />
                    <span class="muted small rank-val">{{ it.value }}</span>
                  </div>
                </div>
              </template>
              <el-empty
                v-else-if="!ranksLoading"
                :image-size="60"
                description="只有沪深300 / 中证800 股票池里的股票有因子排名"
              />
            </div>
          </el-tab-pane>

          <el-tab-pane v-if="detail.market === 'CN' && detail.type !== 'index'" label="公告" name="ann">
            <div v-loading="newsLoading">
              <div v-for="a in news?.announcements ?? []" :key="a.url" class="news-row">
                <span class="muted small date">{{ a.date }}</span>
                <a :href="a.url" target="_blank" rel="noopener noreferrer">{{ a.title }}</a>
              </div>
              <el-empty v-if="news && !news.announcements.length" :image-size="60" description="近半年没有公告" />
            </div>
          </el-tab-pane>

          <el-tab-pane v-if="detail.type !== 'index'" label="新闻" name="news">
            <div v-loading="newsLoading">
              <div v-for="n in news?.news ?? []" :key="n.url" class="news-row">
                <span class="muted small date">{{ formatDateTime(n.time).slice(0, 16) }}</span>
                <a :href="n.url" target="_blank" rel="noopener noreferrer">{{ n.title }}</a>
                <span class="muted small">{{ n.source }}</span>
              </div>
              <el-empty v-if="news && !news.news.length" :image-size="60" description="没有找到新闻" />
            </div>
          </el-tab-pane>
        </el-tabs>
        <p v-if="news?.errors.length" class="muted small">部分资料获取失败：{{ news.errors.join('；') }}</p>
      </el-card>
    </template>
  </div>
</template>

<style scoped>
.stock-page {
  min-height: 300px;
}

.head {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-end;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}

.title {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}

.title h2 {
  margin: 0;
  font-size: 22px;
}

.price {
  display: flex;
  align-items: baseline;
  gap: 10px;
}

.big {
  font-size: 28px;
  font-weight: 700;
}

.card {
  margin-bottom: 16px;
}

.toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}

.spacer {
  flex: 1;
}

.muted {
  color: var(--el-text-color-secondary);
}

.small {
  font-size: 12px;
}

.note {
  margin: 8px 0 0;
  line-height: 1.6;
}

.rel + .rel {
  margin-top: 12px;
}

.rel-title {
  font-size: 13px;
  font-weight: 600;
  margin-bottom: 4px;
}

.rel-row {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  font-size: 13px;
  padding: 3px 0;
}

.rel-row.column {
  flex-direction: column;
  gap: 2px;
  border-bottom: 1px dashed var(--el-border-color-lighter);
  padding-bottom: 6px;
}

.rank-group {
  margin-bottom: 14px;
}

.rank-title {
  font-weight: 600;
  margin-bottom: 6px;
}

.rank-row {
  display: grid;
  grid-template-columns: 120px 1fr 90px;
  align-items: center;
  gap: 10px;
  padding: 2px 0;
}

.rank-name {
  font-size: 13px;
  cursor: help;
}

.rank-val {
  text-align: right;
}

.news-row {
  display: flex;
  gap: 10px;
  align-items: baseline;
  padding: 5px 0;
  border-bottom: 1px solid var(--el-border-color-extra-light);
  font-size: 13px;
}

.news-row a {
  color: var(--el-text-color-primary);
  text-decoration: none;
  flex: 1;
}

.news-row a:hover {
  color: var(--el-color-primary);
}

.date {
  flex: 0 0 auto;
  min-width: 80px;
}
</style>
