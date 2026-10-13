<script setup lang="ts">
/**
 * 对账与健康度（PRD 3.9 G7 / G9）：某个跟踪账户（实时或回放校验）与快速回测的对账结果，以及跟踪期表现是否偏离回测历史。
 */
import StockLink from '@/components/StockLink.vue'
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { errorMessage } from '@/api/http'
import { checkApi, type HealthResult, type ReconcileResult } from '@/api/quant'
import LabCurve from '@/components/lab/LabCurve.vue'
import { formatPercent } from '@/utils/format'

const props = defineProps<{ accountId: number; title?: string }>()
const recon = ref<ReconcileResult | null>(null)
const health = ref<HealthResult | null>(null)
const loading = ref(false)

async function load() {
  loading.value = true
  try {
    const r = await checkApi.account(props.accountId)
    recon.value = r.reconcile
    health.value = r.health
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    loading.value = false
  }
}
onMounted(load)
watch(() => props.accountId, load)
defineExpose({ load })

const TYPE: Record<string, 'success' | 'warning' | 'danger' | 'info'> = {
  一致: 'success',
  有偏差: 'warning',
  偏差较大: 'danger',
  正常: 'success',
  关注: 'warning',
  提醒: 'danger',
}
const curves = computed(() => {
  const c = recon.value?.curve
  if (!c || c.dates.length < 2) return null
  return { dates: c.dates, nav: c.live, bench: c.backtest }
})
const pct = (v: number | null | undefined, d = 2) => formatPercent(v ?? null, d)
</script>

<template>
  <div v-loading="loading" class="check">
    <div class="head">
      <strong>{{ title ?? '对账与健康度' }}</strong>
      <el-button size="small" text type="primary" @click="load">刷新</el-button>
    </div>
    <template v-if="recon">
      <div class="row">
        <el-tag :type="TYPE[recon.status] ?? 'info'">对账：{{ recon.status }}</el-tag>
        <span class="muted">{{ recon.message }}</span>
      </div>
      <template v-if="recon.days">
        <div class="stats">
          <div>
            <span class="label">区间</span
            ><span class="value small">{{ recon.start }} ～ {{ recon.end }}（{{ recon.days }} 天）</span>
          </div>
          <div>
            <span class="label">交易引擎收益</span><span class="value">{{ pct(recon.live_return) }}</span>
          </div>
          <div>
            <span class="label">快速回测收益</span><span class="value">{{ pct(recon.backtest_return) }}</span>
          </div>
          <div>
            <span class="label">累计差 / 最大差</span
            ><span class="value">{{ pct(recon.gap) }} / {{ formatPercent(recon.max_gap ?? null, 2, false) }}</span>
          </div>
          <div>
            <span class="label">跟踪误差（年化）</span
            ><span class="value">{{ formatPercent(recon.tracking_error ?? null, 2, false) }}</span>
          </div>
          <div>
            <span class="label">日收益相关</span><span class="value">{{ recon.correlation?.toFixed(3) ?? '-' }}</span>
          </div>
          <div>
            <span class="label">成交笔数（引擎 / 回测）</span
            ><span class="value">{{ recon.trades_live }} / {{ recon.trades_backtest }}</span>
          </div>
          <div>
            <span class="label">成交价偏差</span>
            <span class="value">{{
              recon.price_dev_bps === null || recon.price_dev_bps === undefined ? '-' : `${recon.price_dev_bps} bp`
            }}</span>
            <span class="muted">（{{ recon.matched_fills }} 笔可对照，正数表示实际更差）</span>
          </div>
          <div>
            <span class="label">期末持仓重合度</span
            ><span class="value">{{ formatPercent(recon.holdings_overlap ?? null, 0, false) }}</span>
          </div>
        </div>
        <LabCurve v-if="curves" :curves="curves" :height="200" name="交易引擎" />
        <p class="muted">图中灰线为快速回测（同一版本、同一时期、同样本金）。</p>
        <el-table v-if="recon.weight_diffs?.length" :data="recon.weight_diffs" size="small" style="max-width: 560px">
          <el-table-column label="期末权重差异最大的标的" min-width="160">
            <template #default="{ row }"><StockLink :symbol="row.symbol" :name="row.name" /></template>
          </el-table-column>
          <el-table-column label="引擎" width="80" align="right">
            <template #default="{ row }">{{ formatPercent(row.live, 1, false) }}</template>
          </el-table-column>
          <el-table-column label="回测" width="80" align="right">
            <template #default="{ row }">{{ formatPercent(row.backtest, 1, false) }}</template>
          </el-table-column>
        </el-table>
      </template>
    </template>
    <template v-if="health">
      <div class="row health">
        <el-tag :type="TYPE[health.status] ?? 'info'">健康度：{{ health.status }}</el-tag>
        <span class="muted">{{ health.message }}</span>
      </div>
      <ul v-if="health.checks.length" class="checks">
        <li v-for="c in health.checks" :key="c.item">
          <el-tag size="small" :type="TYPE[c.level]">{{ c.level }}</el-tag>
          {{ c.item }}：{{ c.value === null ? '-' : formatPercent(c.value, 1) }}（{{ c.detail }}）
        </li>
      </ul>
    </template>
  </div>
</template>

<style scoped>
.head,
.row {
  display: flex;
  gap: 8px;
  align-items: center;
  justify-content: space-between;
}

.row {
  justify-content: flex-start;
  margin: 6px 0;
}

.health {
  margin-top: 12px;
}

.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.stats {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(170px, 1fr));
  gap: 8px;
  margin: 8px 0;
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
  font-size: 15px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}

.value.small {
  font-size: 12px;
}

.checks {
  margin: 4px 0;
  padding-left: 18px;
  font-size: 13px;
  line-height: 2;
}
</style>
