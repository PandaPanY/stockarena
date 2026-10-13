<script setup lang="ts">
/** 每日信号（PRD 3.8）：所有策略的实时跟踪表现、最近一次决策与下一交易日的调仓清单。 */

import StockLink from '@/components/StockLink.vue'
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { errorMessage } from '@/api/http'
import { quantApi, signalApi, type Strategy, type Tracking } from '@/api/quant'
import { formatPercent } from '@/utils/format'
import { sideClass, sideText } from '@/utils/orders'

const router = useRouter()
const rows = ref<{ s: Strategy; t: Tracking | null }[]>([])
const loading = ref(true)

onMounted(async () => {
  try {
    const strategies = (await quantApi.list()).strategies.filter((s) => s.mine) // 信号与跟踪只看自己的策略
    const tracks = await Promise.all(strategies.map((s) => (s.follow_account_id ? signalApi.tracking(s.id) : null)))
    rows.value = strategies.map((s, i) => ({ s, t: tracks[i] }))
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    loading.value = false
  }
})

const followed = computed(() => rows.value.filter((r) => r.t))
const others = computed(() => rows.value.filter((r) => !r.t))
const ret = (t: Tracking) => (t.nav.length ? t.nav[t.nav.length - 1].nav - 1 : null)
const bench = (t: Tracking) => {
  const b = t.nav.length ? t.nav[t.nav.length - 1].bench : null
  return b === null ? null : b - 1
}
const cls = (v: number | null) => (v === null ? '' : v >= 0 ? 'sa-up' : 'sa-down')
const open = (id: number) => router.push({ path: `/lab/${id}`, query: { tab: 'signal' } })
</script>

<template>
  <div v-loading="loading">
    <h2 class="sa-page-title">每日信号</h2>
    <p class="muted">
      实时跟踪中的策略每个交易日收盘后按冠军版本决策，下一交易日开盘执行。这里汇总各策略的表现和调仓清单；未跟踪的策略可以在详情页查看最新信号或开始跟踪。
    </p>

    <el-empty v-if="!loading && !followed.length" description="还没有实时跟踪的策略">
      <el-button type="primary" @click="router.push('/lab')">去策略库选择</el-button>
    </el-empty>

    <el-card v-for="{ s, t } in followed" :key="s.id" shadow="never" class="block">
      <template #header>
        <div class="head">
          <div>
            <el-link type="primary" class="name" @click="open(s.id)">{{ s.name }}</el-link>
            <el-tag v-if="s.champion" size="small" type="success">v{{ s.champion.number }}</el-tag>
            <span class="muted">自 {{ t!.start_date }} 跟踪</span>
          </div>
          <div class="stat">
            <span :class="cls(ret(t!))">{{ formatPercent(ret(t!)) }}</span>
            <span class="muted">基准 {{ formatPercent(bench(t!)) }}</span>
          </div>
        </div>
      </template>
      <template v-if="t!.last_decision">
        <div class="decision">
          <strong>{{ t!.last_decision.data_date }} 收盘：</strong>
          <el-tag v-if="t!.last_decision.parsed?.rebalance" size="small" type="warning">调仓日</el-tag>
          <span>{{ t!.last_decision.analysis?.split('\n')[0] }}</span>
        </div>
        <el-table v-if="t!.orders.length" :data="t!.orders" size="small">
          <el-table-column label="方向" width="70">
            <template #default="{ row }"
              ><span :class="sideClass(row.side)">{{ sideText(row.side) }}</span></template
            >
          </el-table-column>
          <el-table-column label="标的" min-width="160">
            <template #default="{ row }"><StockLink :symbol="row.symbol" :name="row.name" /></template>
          </el-table-column>
          <el-table-column prop="qty" label="数量" width="100" align="right" />
          <el-table-column prop="status" label="状态" width="100" />
        </el-table>
        <p v-else class="muted">{{ t!.last_decision.fill_date }} 无需交易。</p>
      </template>
      <p v-else class="muted">等待首个交易日收盘后决策。</p>
    </el-card>

    <template v-if="others.length">
      <h3>未跟踪的策略</h3>
      <el-table :data="others" size="small">
        <el-table-column label="策略" min-width="140">
          <template #default="{ row }">
            <el-link @click="open(row.s.id)">{{ row.s.name }}</el-link>
          </template>
        </el-table-column>
        <el-table-column label="冠军" width="80">
          <template #default="{ row }">{{ row.s.champion ? `v${row.s.champion.number}` : '-' }}</template>
        </el-table-column>
        <el-table-column label="开发期夏普" width="100" align="right">
          <template #default="{ row }">{{ row.s.champion?.dev.sharpe?.toFixed(2) ?? '-' }}</template>
        </el-table-column>
        <el-table-column label="留出期收益" width="110" align="right">
          <template #default="{ row }">{{
            formatPercent(row.s.champion?.holdout.cumulative_return ?? null, 1)
          }}</template>
        </el-table-column>
        <el-table-column label="" width="160">
          <template #default="{ row }">
            <el-button size="small" @click="open(row.s.id)">查看信号 / 开始跟踪</el-button>
          </template>
        </el-table-column>
      </el-table>
    </template>
  </div>
</template>

<style scoped>
.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.block {
  margin-bottom: 16px;
}

.head {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.head > div {
  display: flex;
  gap: 8px;
  align-items: center;
}

.name {
  font-size: 16px;
  font-weight: 600;
}

.stat {
  font-size: 16px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}

.decision {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-bottom: 8px;
  font-size: 13px;
}
</style>
