<script setup lang="ts">
import StockLink from '@/components/StockLink.vue'
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { errorMessage } from '@/api/http'
import {
  accountsApi,
  tracksApi,
  type AccountDetail,
  type FillItem,
  type KBar,
  type LedgerRow,
  type NavSeries,
  type OrderItem,
  type PositionItem,
} from '@/api/arena'
import KlineChart from '@/components/KlineChart.vue'
import NavChart from '@/components/NavChart.vue'
import StrategyPanel from '@/components/StrategyPanel.vue'
import { formatMoney, formatPercent } from '@/utils/format'
import { orderExtraText, orderTypeText, sideClass, sideText } from '@/utils/orders'

const route = useRoute()
const router = useRouter()
const accountId = computed(() => Number(route.params.id))

const account = ref<AccountDetail | null>(null)
const positions = ref<PositionItem[]>([])
const orders = ref<OrderItem[]>([])
const fills = ref<FillItem[]>([])
const ledger = ref<LedgerRow[]>([])
const nav = ref<NavSeries | null>(null)
const tab = ref(typeof route.query.tab === 'string' ? route.query.tab : 'positions')

const symbols = ref<string[]>([])
const klineSymbol = ref<string>('')
const kline = ref<{ name: string; bars: KBar[]; fills: FillItem[] } | null>(null)
const selectedFill = ref<FillItem | null>(null)

const ORDER_STATUS: Record<
  OrderItem['status'],
  { label: string; type: 'success' | 'info' | 'warning' | 'danger' | 'primary' }
> = {
  pending: { label: '待撮合', type: 'primary' },
  filled: { label: '已成交', type: 'success' },
  partially_filled: { label: '部分成交', type: 'warning' },
  rejected: { label: '已拒绝', type: 'danger' },
  expired: { label: '未成交', type: 'info' },
  cancelled: { label: '已撤销', type: 'info' },
}
const LEDGER_TYPES: Record<string, string> = {
  deposit: '入金',
  trade: '成交',
  fee: '费用',
  dividend: '分红',
  adjust: '调整',
}

const m = computed(() => account.value?.metrics)

async function load() {
  try {
    const id = accountId.value
    account.value = await accountsApi.get(id)
    const [p, o, f, l, s, n] = await Promise.all([
      accountsApi.positions(id),
      accountsApi.orders(id),
      accountsApi.fills(id),
      accountsApi.ledger(id),
      accountsApi.symbols(id),
      tracksApi.nav(account.value.track_id),
    ])
    positions.value = p
    orders.value = o
    fills.value = f
    ledger.value = l
    symbols.value = Array.from(new Set([...p.map((x) => x.symbol), ...s]))
    // 只显示本账户与基准
    nav.value = { benchmark: n.benchmark, accounts: n.accounts.filter((a) => a.account_id === id) }
    if (!klineSymbol.value && symbols.value.length) klineSymbol.value = symbols.value[0]
  } catch (err) {
    ElMessage.error(errorMessage(err))
  }
}

async function loadKline() {
  if (!klineSymbol.value) return
  try {
    kline.value = await accountsApi.kline(accountId.value, klineSymbol.value)
    selectedFill.value = null
  } catch (err) {
    ElMessage.error(errorMessage(err))
  }
}

function openDecision(decisionId: number | null) {
  if (decisionId) router.push({ name: 'decisions', query: { account_id: accountId.value, decision: decisionId } })
}

function pctClass(v: number | null | undefined) {
  if (v === null || v === undefined) return ''
  return v >= 0 ? 'sa-up' : 'sa-down'
}

watch(klineSymbol, loadKline)
watch(accountId, load)
onMounted(load)
</script>

<template>
  <div v-if="account && m">
    <el-button link icon="ArrowLeft" @click="router.push({ name: 'track', params: { id: account.track_id } })">
      {{ account.track_name }}
    </el-button>
    <h2 class="sa-page-title title">
      {{ account.agent.name }}
      <el-tag size="small" :type="account.agent.kind === 'llm' ? 'primary' : 'info'">
        {{ account.agent.kind === 'llm' ? 'AI 选手' : '基线' }}
      </el-tag>
      <span v-if="account.agent.model" class="model">{{ account.agent.model }}</span>
    </h2>

    <!-- 指标卡片 -->
    <div class="tiles">
      <div class="tile">
        <div class="tile-label">净值（{{ account.currency }}）</div>
        <div class="tile-value">{{ formatMoney(m.nav) }}</div>
        <div class="tile-sub">初始 {{ formatMoney(account.initial_cash, 0) }}</div>
      </div>
      <div class="tile">
        <div class="tile-label">累计收益</div>
        <div class="tile-value" :class="pctClass(m.cumulative_return)">{{ formatPercent(m.cumulative_return) }}</div>
        <div class="tile-sub">
          基准 {{ formatPercent(m.benchmark_return) }} · 超额
          <span :class="pctClass(m.excess_return)">{{ formatPercent(m.excess_return) }}</span>
        </div>
      </div>
      <div class="tile">
        <div class="tile-label">最大回撤</div>
        <div class="tile-value">{{ formatPercent(m.max_drawdown, 2, false) }}</div>
        <div class="tile-sub">最长回撤 {{ m.max_drawdown_days ?? 0 }} 个交易日</div>
      </div>
      <div class="tile">
        <div class="tile-label">夏普 / 索提诺</div>
        <div class="tile-value">{{ m.sharpe?.toFixed(2) ?? '-' }} / {{ m.sortino?.toFixed(2) ?? '-' }}</div>
        <div class="tile-sub">年化波动 {{ formatPercent(m.volatility, 1, false) }}</div>
      </div>
      <div class="tile">
        <div class="tile-label">交易</div>
        <div class="tile-value">{{ m.trades }} 笔</div>
        <div class="tile-sub">
          胜率 {{ formatPercent(m.win_rate, 0, false) }} · 费用 {{ formatMoney(m.total_fees) }}
        </div>
      </div>
      <div v-if="account.margin" class="tile" :class="{ alert: account.margin.margin_call_since }">
        <div class="tile-label">
          杠杆 / {{ account.margin.method === 'collateral_ratio' ? '维持担保比例' : '保证金比率' }}
        </div>
        <div class="tile-value">
          {{ account.margin.leverage?.toFixed(2) ?? '-' }} 倍 /
          {{ account.margin.ratio === null ? '无负债' : formatPercent(account.margin.ratio, 1, false) }}
        </div>
        <div class="tile-sub">
          上限 {{ account.margin.leverage_cap }} 倍 · 维持线 {{ formatPercent(account.margin.maintenance, 0, false) }} ·
          强平线 {{ formatPercent(account.margin.liquidation, 0, false) }}
        </div>
        <div class="tile-sub">
          空头 {{ formatMoney(account.margin.short_value, 0) }} · 融资负债 {{ formatMoney(account.margin.debt, 0) }}
          <template v-if="m.margin_calls || m.forced_liquidations">
            · 追保 {{ m.margin_calls }} 次 · 强平 {{ m.forced_liquidations }} 次</template
          >
        </div>
        <div v-if="account.margin.margin_call_since" class="tile-sub warn">
          ⚠️ {{ account.margin.margin_call_since }} 起处于追保状态
        </div>
      </div>
      <div class="tile">
        <div class="tile-label">持仓风格</div>
        <div class="tile-value">{{ m.avg_holding_days?.toFixed(1) ?? '-' }} 天</div>
        <div class="tile-sub">
          平均持仓 · 最大单票 {{ formatPercent(m.max_weight, 1, false) }} · 年化换手
          {{ m.turnover?.toFixed(1) ?? '-' }} 倍
        </div>
      </div>
      <div v-if="m.ai.llm" class="tile">
        <div class="tile-label">AI 决策</div>
        <div class="tile-value">{{ formatPercent(m.ai.valid_rate, 0, false) }}</div>
        <div class="tile-sub">
          {{ m.ai.rounds }} 轮 · 均耗时 {{ m.ai.avg_latency_s?.toFixed(0) ?? '-' }} 秒 · 费用
          {{ (m.ai.cost ?? 0).toFixed(2) }}
        </div>
      </div>
    </div>
    <p v-if="!m.reliable" class="muted small">
      已运行 {{ m.days }} 个交易日，样本较少，指标仅供参考；不足 60 日不计算年化收益。
    </p>

    <el-card shadow="never" header="收益曲线（对比基准）" class="mb">
      <NavChart :data="nav" :height="360" />
    </el-card>

    <el-card shadow="never" class="mb">
      <el-tabs v-model="tab">
        <el-tab-pane :label="`持仓（${positions.length}）`" name="positions">
          <el-table :data="positions" empty-text="空仓">
            <el-table-column label="标的" min-width="150">
              <template #default="{ row }">
                <StockLink :symbol="row.symbol" :name="row.name" />
                <el-tag v-if="row.short" size="small" type="success" class="ml-s">空头</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="qty" label="数量" align="right" width="90" />
            <el-table-column
              :label="account.margin?.allow_short ? '成本 / 卖空均价' : '成本价'"
              align="right"
              width="120"
              ><template #default="{ row }"
                ><span class="num">{{ row.avg_cost }}</span></template
              ></el-table-column
            >
            <el-table-column label="收盘价" align="right" width="100"
              ><template #default="{ row }"
                ><span class="num">{{ row.close }}</span></template
              ></el-table-column
            >
            <el-table-column label="市值" align="right" width="120"
              ><template #default="{ row }"
                ><span class="num">{{ formatMoney(row.market_value) }}</span></template
              ></el-table-column
            >
            <el-table-column label="浮动盈亏" align="right" width="150">
              <template #default="{ row }">
                <span class="num" :class="pctClass(row.pnl_pct)"
                  >{{ formatMoney(row.pnl) }}（{{ formatPercent(row.pnl_pct) }}）</span
                >
              </template>
            </el-table-column>
            <el-table-column label="占净值" align="right" width="90"
              ><template #default="{ row }"
                ><span class="num">{{ formatPercent(row.weight, 1, false) }}</span></template
              ></el-table-column
            >
          </el-table>
        </el-tab-pane>
        <el-tab-pane label="订单" name="orders">
          <el-table :data="orders" empty-text="暂无订单" max-height="480">
            <el-table-column prop="placed_date" label="决策日" width="105" />
            <el-table-column prop="target_date" label="撮合日" width="105" />
            <el-table-column label="方向" width="70">
              <template #default="{ row }">
                <span :class="sideClass(row.side)">{{ sideText(row.side) }}</span>
                <el-tag v-if="row.forced" size="small" type="danger" class="ml-s">强平</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="symbol" label="标的" width="100" />
            <el-table-column label="类型" width="120">
              <template #default="{ row }">{{ orderTypeText(row) }}</template>
            </el-table-column>
            <el-table-column label="数量 / 成交" width="110" align="right">
              <template #default="{ row }"
                ><span class="num">{{ row.qty }} / {{ row.filled_qty }}</span></template
              >
            </el-table-column>
            <el-table-column label="状态" width="90">
              <template #default="{ row }"
                ><el-tag size="small" :type="ORDER_STATUS[row.status as OrderItem['status']].type">{{
                  ORDER_STATUS[row.status as OrderItem['status']].label
                }}</el-tag></template
              >
            </el-table-column>
            <el-table-column label="理由 / 说明" min-width="260" show-overflow-tooltip>
              <template #default="{ row }">
                {{ row.reason }}<span v-if="orderExtraText(row)" class="note"> · {{ orderExtraText(row) }}</span>
                <span v-if="row.note" class="note"> · {{ row.note }}</span>
              </template>
            </el-table-column>
            <el-table-column width="70">
              <template #default="{ row }"
                ><el-button v-if="row.decision_id" link type="primary" @click="openDecision(row.decision_id)"
                  >决策</el-button
                ></template
              >
            </el-table-column>
          </el-table>
        </el-tab-pane>
        <el-tab-pane label="成交" name="fills">
          <el-table :data="fills" empty-text="暂无成交" max-height="480">
            <el-table-column prop="trade_date" label="日期" width="105" />
            <el-table-column label="方向" width="60">
              <template #default="{ row }"
                ><span :class="sideClass(row.side)">{{ sideText(row.side) }}</span></template
              >
            </el-table-column>
            <el-table-column prop="symbol" label="标的" width="100" />
            <el-table-column prop="qty" label="数量" align="right" width="90" />
            <el-table-column prop="price" label="成交价" align="right" width="100" />
            <el-table-column label="成交额" align="right" width="120"
              ><template #default="{ row }">{{ formatMoney(row.amount) }}</template></el-table-column
            >
            <el-table-column label="费用" align="right" width="90"
              ><template #default="{ row }">{{ row.total_fee }}</template></el-table-column
            >
            <el-table-column label="已实现盈亏" align="right" min-width="110">
              <template #default="{ row }">
                <span v-if="row.realized_pnl !== null" :class="Number(row.realized_pnl) >= 0 ? 'sa-up' : 'sa-down'">{{
                  formatMoney(row.realized_pnl)
                }}</span>
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>
        <el-tab-pane v-if="account.agent.kind === 'llm'" label="策略演进" name="strategy" lazy>
          <StrategyPanel :account-id="accountId" />
        </el-tab-pane>
        <el-tab-pane label="资金流水" name="ledger">
          <el-table :data="ledger" max-height="480">
            <el-table-column prop="trade_date" label="日期" width="105" />
            <el-table-column label="类型" width="70"
              ><template #default="{ row }">{{ LEDGER_TYPES[row.type] ?? row.type }}</template></el-table-column
            >
            <el-table-column label="金额" align="right" width="130">
              <template #default="{ row }"
                ><span class="num">{{ formatMoney(row.amount) }}</span></template
              >
            </el-table-column>
            <el-table-column label="余额" align="right" width="130">
              <template #default="{ row }"
                ><span class="num">{{ formatMoney(row.balance_after) }}</span></template
              >
            </el-table-column>
            <el-table-column prop="note" label="说明" show-overflow-tooltip />
          </el-table>
        </el-tab-pane>
      </el-tabs>
    </el-card>

    <el-card shadow="never">
      <template #header>
        <div class="card-header">
          <span>K 线与买卖点</span>
          <el-select v-model="klineSymbol" placeholder="选择标的" style="width: 200px" :disabled="!symbols.length">
            <el-option v-for="s in symbols" :key="s" :label="s" :value="s" />
          </el-select>
        </div>
      </template>
      <el-empty v-if="!symbols.length" description="还没有交易记录" :image-size="60" />
      <template v-else-if="kline">
        <KlineChart :bars="kline.bars" :fills="kline.fills" @select-fill="(f) => (selectedFill = f)" />
        <el-alert v-if="selectedFill" type="info" :closable="true" class="mt" @close="selectedFill = null">
          <template #title>
            {{ selectedFill.trade_date }} {{ selectedFill.side === 'buy' ? '买入' : '卖出' }} {{ selectedFill.qty }} 股
            @ {{ selectedFill.price }}
          </template>
          {{ selectedFill.reason || '（无理由说明）' }}
        </el-alert>
      </template>
    </el-card>
  </div>
  <el-skeleton v-else :rows="8" animated />
</template>

<style scoped>
.title {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 4px 0 16px;
}

.model {
  font-size: 12px;
  font-weight: normal;
  color: var(--el-text-color-secondary);
  max-width: 420px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tiles {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: 12px;
  margin-bottom: 8px;
}

.tile {
  background: var(--el-bg-color);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  padding: 14px 16px;
}

.tile-label {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.tile-value {
  font-size: 24px;
  font-weight: 600;
  margin: 6px 0 4px;
}

.tile-sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.mb {
  margin: 16px 0;
}

.mt {
  margin-top: 12px;
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.num {
  font-variant-numeric: tabular-nums;
}

.muted {
  color: var(--el-text-color-secondary);
}

.small {
  font-size: 12px;
}

.note {
  color: var(--el-text-color-secondary);
}

.ml-s {
  margin-left: 4px;
}

.tile.alert {
  border-color: var(--el-color-danger-light-5);
}

.warn {
  color: var(--el-color-danger);
}
</style>
