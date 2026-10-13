<script setup lang="ts">
/**
 * 策略版本的交易明细（PRD 3.8）：交易了哪些股票 / 基金、每笔持有收益如何、为什么买入和卖出。
 * 按区间查看；持有记录按卖出日期归入区间，回测结束时仍持有的只出现在最后的区间。
 */

import StockLink from '@/components/StockLink.vue'
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { errorMessage } from '@/api/http'
import { detailsApi, type DetailWindow, type EpisodeRow, type TradeDetails } from '@/api/quant'
import { formatMoney, formatPercent } from '@/utils/format'

const props = defineProps<{ versionId: number }>()

const windowKey = ref<DetailWindow>('holdout')
const tab = ref('overview')
const data = ref<TradeDetails | null>(null)
const loading = ref(false)

async function load() {
  loading.value = true
  try {
    data.value = await detailsApi.trades(props.versionId, windowKey.value)
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    loading.value = false
  }
}
watch([() => props.versionId, windowKey], load, { immediate: true })

const WINDOWS: { key: DetailWindow; label: string }[] = [
  { key: 'dev', label: '开发期' },
  { key: 'train', label: '其中训练' },
  { key: 'valid', label: '其中验证' },
  { key: 'holdout', label: '留出' },
  { key: 'all', label: '全部' },
]

const s = computed(() => data.value?.summary)
const cls = (v: number | null | undefined) => (v === null || v === undefined ? '' : v >= 0 ? 'sa-up' : 'sa-down')
const wan = (v: number) => `${v >= 0 ? '+' : ''}${(v / 1e4).toFixed(1)} 万`
const typeText = (t: string) => (t === 'stock' ? '股票' : '基金')

// ---- 持仓收益：筛选与分页
const epFilter = ref<'all' | 'win' | 'loss' | 'open'>('all')
const epType = ref<'all' | 'stock' | 'fund'>('all')
const epPage = ref(1)
const episodes = computed<EpisodeRow[]>(() => {
  let rows = data.value?.episodes ?? []
  if (epFilter.value === 'win') rows = rows.filter((e) => !e.open && e.pnl > 0)
  if (epFilter.value === 'loss') rows = rows.filter((e) => !e.open && e.pnl <= 0)
  if (epFilter.value === 'open') rows = rows.filter((e) => e.open)
  if (epType.value !== 'all') rows = rows.filter((e) => (epType.value === 'stock') === (e.type === 'stock'))
  return rows
})
const PAGE = 50
const epRows = computed(() => episodes.value.slice((epPage.value - 1) * PAGE, epPage.value * PAGE))
watch([epFilter, epType, windowKey], () => (epPage.value = 1))

// ---- 成交流水分页
const trPage = ref(1)
const trRows = computed(() => (data.value?.trades ?? []).slice((trPage.value - 1) * PAGE, trPage.value * PAGE))
watch(windowKey, () => (trPage.value = 1))

// ---- 调仓记录分页（每页 20 次）
const rbPage = ref(1)
const rbRows = computed(() => (data.value?.rebalances ?? []).slice((rbPage.value - 1) * 20, rbPage.value * 20))
watch(windowKey, () => (rbPage.value = 1))

const pctTop = (p: number | null) => (p === null ? '-' : `前 ${Math.max(1, Math.round((1 - p) * 100))}%`)
</script>

<template>
  <div v-loading="loading" class="details">
    <div class="bar">
      <el-radio-group v-model="windowKey" size="small">
        <el-radio-button v-for="w in WINDOWS" :key="w.key" :value="w.key">{{ w.label }}</el-radio-button>
      </el-radio-group>
      <span v-if="s" class="muted">{{ s.start }} ～ {{ s.end }}</span>
    </div>

    <template v-if="data && s">
      <el-tabs v-model="tab">
        <el-tab-pane label="概览" name="overview">
          <div class="stats">
            <div>
              <span class="label">成交</span><span class="value">{{ s.trades }} 笔</span>
              <span class="muted">买 {{ s.buys }} / 卖 {{ s.sells }}</span>
            </div>
            <div>
              <span class="label">完整持有</span><span class="value">{{ s.closed }} 次</span>
              <span class="muted">期末仍持有 {{ s.open }}</span>
            </div>
            <div>
              <span class="label">胜率</span><span class="value">{{ formatPercent(s.win_rate, 0, false) }}</span>
            </div>
            <div>
              <span class="label">平均盈利 / 亏损</span>
              <span class="value"
                ><span class="sa-up">{{ formatPercent(s.avg_win, 1) }}</span> /
                <span class="sa-down">{{ formatPercent(s.avg_loss, 1) }}</span></span
              >
            </div>
            <div>
              <span class="label">平均持有</span><span class="value">{{ s.avg_days ?? '-' }} 天</span>
            </div>
            <div>
              <span class="label">交易盈亏</span><span class="value" :class="cls(s.pnl)">{{ wan(s.pnl) }}</span>
              <span class="muted">费用 {{ formatMoney(s.fees, 0) }}</span>
            </div>
            <div>
              <span class="label">涉及标的</span><span class="value">{{ s.symbols }} 个</span>
            </div>
            <div>
              <span class="label">调仓 / 止损</span><span class="value">{{ s.rebalances }} / {{ s.stops }} 次</span>
            </div>
          </div>
          <h4>卖出原因</h4>
          <el-table :data="s.exit_reasons" size="small" style="max-width: 640px">
            <el-table-column prop="reason" label="原因" width="140" />
            <el-table-column prop="count" label="次数" width="80" align="right" />
            <el-table-column label="平均收益" width="110" align="right">
              <template #default="{ row }"
                ><span :class="cls(row.avg_ret)">{{ formatPercent(row.avg_ret, 1) }}</span></template
              >
            </el-table-column>
            <el-table-column label="合计盈亏" align="right">
              <template #default="{ row }"
                ><span :class="cls(row.pnl)">{{ wan(row.pnl) }}</span></template
              >
            </el-table-column>
          </el-table>
          <h4>盈亏最多的标的</h4>
          <el-table :data="data.symbols.slice(0, 10)" size="small" style="max-width: 640px">
            <el-table-column label="标的" min-width="150">
              <template #default="{ row }"><StockLink :symbol="row.symbol" :name="row.name" /></template>
            </el-table-column>
            <el-table-column label="持有次数" width="90" align="right">
              <template #default="{ row }">{{ row.episodes }}（赚 {{ row.wins }}）</template>
            </el-table-column>
            <el-table-column label="合计盈亏" width="110" align="right">
              <template #default="{ row }"
                ><span :class="cls(row.pnl)">{{ wan(row.pnl) }}</span></template
              >
            </el-table-column>
          </el-table>
        </el-tab-pane>

        <el-tab-pane :label="`调仓记录（${data.rebalances.length}）`" name="rebalances">
          <p class="muted">
            每次调仓在 T 日收盘计算，T+1 开盘执行。展开可看到入选标的的综合排名与各因子数值（「前
            5%」表示按该因子的方向在可选标的中排前 5%；「越低越好」的因子如资产负债率，数值越低排名越靠前）。
          </p>
          <el-collapse>
            <el-collapse-item v-for="r in rbRows" :key="r.date + r.kind" :name="r.date + r.kind">
              <template #title>
                <div class="rb-title">
                  <el-tag size="small" :type="r.kind === 'stop' ? 'danger' : 'primary'">
                    {{ r.kind === 'stop' ? '止损' : '调仓' }}
                  </el-tag>
                  <span>{{ r.date }} 收盘 → {{ r.exec }} 执行</span>
                  <span v-if="r.kind === 'rebalance'" class="muted">
                    可选 {{ r.eligible }} 个，选中 {{ r.picks?.length }}（新买 {{ r.new }}，继续持有
                    {{ r.kept }}），卖出 {{ r.drops.length }}
                  </span>
                  <span v-else class="muted">{{ r.drops.map((d) => d.name).join('、') }}</span>
                  <el-tag v-if="r.timing_on === false" size="small" type="warning">择时降仓</el-tag>
                </div>
              </template>
              <p v-if="r.timing" class="muted">大盘择时：{{ r.timing }}</p>
              <el-table v-if="r.picks?.length" :data="r.picks" size="small">
                <el-table-column label="排名" width="80">
                  <template #default="{ row }">{{ row.defensive ? '防守' : `${row.rank}/${row.of}` }}</template>
                </el-table-column>
                <el-table-column label="标的" min-width="140">
                  <template #default="{ row }"><StockLink :symbol="row.symbol" :name="row.name" /></template>
                </el-table-column>
                <el-table-column label="目标权重" width="90" align="right">
                  <template #default="{ row }">{{ formatPercent(row.weight, 1, false) }}</template>
                </el-table-column>
                <el-table-column label="因子（数值 / 在可选标的中的位置）" min-width="300">
                  <template #default="{ row }">
                    <span v-for="f in row.factors" :key="f.name" class="factor">
                      {{ f.name }} {{ f.value === null ? '-' : f.value.toPrecision(3) }}
                      <span v-if="!row.defensive" class="muted">（{{ pctTop(f.pct) }}）</span>
                    </span>
                  </template>
                </el-table-column>
              </el-table>
              <div v-if="r.drops.length" class="drops">
                <strong>卖出：</strong>
                <div v-for="d in r.drops" :key="d.symbol">
                  <StockLink :symbol="d.symbol" :name="d.name" />：{{ d.reason }}
                </div>
              </div>
            </el-collapse-item>
          </el-collapse>
          <el-pagination
            v-if="data.rebalances.length > 20"
            v-model:current-page="rbPage"
            :page-size="20"
            :total="data.rebalances.length"
            layout="prev, pager, next"
            small
            class="pager"
          />
        </el-tab-pane>

        <el-tab-pane :label="`持仓收益（${data.episodes.length}）`" name="episodes">
          <div class="bar">
            <el-radio-group v-model="epFilter" size="small">
              <el-radio-button value="all">全部</el-radio-button>
              <el-radio-button value="win">盈利</el-radio-button>
              <el-radio-button value="loss">亏损</el-radio-button>
              <el-radio-button value="open">仍持有</el-radio-button>
            </el-radio-group>
            <el-radio-group v-model="epType" size="small">
              <el-radio-button value="all">全部品种</el-radio-button>
              <el-radio-button value="stock">股票</el-radio-button>
              <el-radio-button value="fund">基金</el-radio-button>
            </el-radio-group>
          </div>
          <el-table :data="epRows" size="small" :default-sort="{ prop: 'exit', order: 'descending' }">
            <el-table-column label="标的" min-width="150">
              <template #default="{ row }">
                {{ row.name }} <span class="muted">{{ row.symbol }} · {{ typeText(row.type) }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="entry" label="买入" width="100" sortable />
            <el-table-column label="卖出" width="100" prop="exit" sortable>
              <template #default="{ row }">{{ row.exit ?? '仍持有' }}</template>
            </el-table-column>
            <el-table-column prop="days" label="持有天数" width="90" align="right" sortable />
            <el-table-column label="收益率" width="90" align="right" prop="ret" sortable>
              <template #default="{ row }"
                ><span :class="cls(row.ret)">{{ formatPercent(row.ret, 1) }}</span></template
              >
            </el-table-column>
            <el-table-column label="盈亏" width="100" align="right" prop="pnl" sortable>
              <template #default="{ row }"
                ><span :class="cls(row.pnl)">{{ formatMoney(row.pnl, 0) }}</span></template
              >
            </el-table-column>
            <el-table-column prop="reason" label="卖出原因" min-width="220" show-overflow-tooltip />
          </el-table>
          <el-pagination
            v-if="episodes.length > PAGE"
            v-model:current-page="epPage"
            :page-size="PAGE"
            :total="episodes.length"
            layout="total, prev, pager, next"
            small
            class="pager"
          />
        </el-tab-pane>

        <el-tab-pane :label="`标的汇总（${data.symbols.length}）`" name="symbols">
          <el-table :data="data.symbols" size="small" max-height="560">
            <el-table-column label="标的" min-width="150">
              <template #default="{ row }">
                {{ row.name }} <span class="muted">{{ row.symbol }} · {{ typeText(row.type) }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="episodes" label="持有次数" width="90" align="right" sortable />
            <el-table-column label="胜率" width="80" align="right">
              <template #default="{ row }">{{ formatPercent(row.wins / row.episodes, 0, false) }}</template>
            </el-table-column>
            <el-table-column prop="days" label="累计持有天数" width="110" align="right" sortable />
            <el-table-column label="最好 / 最差" width="150" align="right">
              <template #default="{ row }">
                <span :class="cls(row.best)">{{ formatPercent(row.best, 1) }}</span> /
                <span :class="cls(row.worst)">{{ formatPercent(row.worst, 1) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="合计盈亏" width="110" align="right" prop="pnl" sortable>
              <template #default="{ row }"
                ><span :class="cls(row.pnl)">{{ wan(row.pnl) }}</span></template
              >
            </el-table-column>
          </el-table>
        </el-tab-pane>

        <el-tab-pane :label="`成交流水（${data.trades_total}）`" name="trades">
          <p v-if="data.trades_total > data.trades.length" class="muted">
            只显示最近 {{ data.trades.length }} 笔，请切换到更短的区间查看更早的成交。
          </p>
          <el-table :data="trRows" size="small">
            <el-table-column prop="date" label="日期" width="100" />
            <el-table-column label="方向" width="60">
              <template #default="{ row }">
                <span :class="row.side === 'buy' ? 'sa-up' : 'sa-down'">{{
                  row.side === 'buy' ? '买入' : '卖出'
                }}</span>
              </template>
            </el-table-column>
            <el-table-column label="标的" min-width="140">
              <template #default="{ row }"><StockLink :symbol="row.symbol" :name="row.name" /></template>
            </el-table-column>
            <el-table-column prop="qty" label="数量" width="90" align="right" />
            <el-table-column label="价格" width="80" align="right">
              <template #default="{ row }">{{ row.price.toFixed(row.price < 10 ? 3 : 2) }}</template>
            </el-table-column>
            <el-table-column label="金额" width="110" align="right">
              <template #default="{ row }">{{ formatMoney(row.amount, 0) }}</template>
            </el-table-column>
            <el-table-column label="费用" width="80" align="right">
              <template #default="{ row }">{{ formatMoney(row.fee, 2) }}</template>
            </el-table-column>
            <el-table-column prop="note" label="原因" min-width="220" show-overflow-tooltip />
          </el-table>
          <el-pagination
            v-if="data.trades.length > PAGE"
            v-model:current-page="trPage"
            :page-size="PAGE"
            :total="data.trades.length"
            layout="total, prev, pager, next"
            small
            class="pager"
          />
        </el-tab-pane>
      </el-tabs>
    </template>
  </div>
</template>

<style scoped>
.bar {
  display: flex;
  gap: 12px;
  align-items: center;
  margin-bottom: 8px;
  flex-wrap: wrap;
}

.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.stats {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
  gap: 12px;
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

h4 {
  margin: 12px 0 6px;
}

.rb-title {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
}

.factor {
  margin-right: 12px;
  white-space: nowrap;
}

.drops {
  margin-top: 8px;
  font-size: 13px;
  line-height: 1.8;
}

.pager {
  margin-top: 8px;
}
</style>
