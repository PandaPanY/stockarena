<script setup lang="ts">
/** 试算结果：只显示开发期（不保存、不看留出期，反复修改规则时不消耗留出期的检验意义）。 */
import type { PreviewResult } from '@/api/quant'
import LabCurve from '@/components/lab/LabCurve.vue'
import { formatPercent } from '@/utils/format'

defineProps<{ result: PreviewResult }>()

const num = (v: number | null | undefined, d = 2) => (v === null || v === undefined ? '-' : v.toFixed(d))
</script>

<template>
  <div class="preview">
    <p class="muted">
      开发期 {{ result.window.start }} ～ {{ result.window.end }} 的回测（未保存）。为保持检验意义，试算不显示留出期；
      保存后会按完整流程回测并检验。
    </p>
    <el-alert v-for="w in result.warnings" :key="w" type="warning" :closable="false" :title="w" class="warn" />
    <div class="kpis">
      <div>
        <span>年化收益</span><b>{{ formatPercent(result.dev.annualized_return, 1) }}</b>
      </div>
      <div>
        <span>累计超额</span><b>{{ formatPercent(result.dev.excess_return, 1) }}</b>
      </div>
      <div>
        <span>夏普</span><b>{{ num(result.dev.sharpe) }}</b>
      </div>
      <div>
        <span>最大回撤</span><b>{{ formatPercent(result.dev.max_drawdown, 1, false) }}</b>
      </div>
      <div>
        <span>年化换手</span><b>{{ num(result.dev.turnover, 1) }} 倍</b>
      </div>
      <div>
        <span>平均持仓</span><b>{{ num(result.dev.avg_holdings, 1) }} 只</b>
      </div>
    </div>
    <LabCurve :curves="result.curves" :height="220" name="试算" />
    <el-table v-if="result.dev.yearly?.length" :data="result.dev.yearly" size="small" class="years">
      <el-table-column label="年份" width="80">
        <template #default="{ row }">{{ row.year }}{{ row.partial ? '*' : '' }}</template>
      </el-table-column>
      <el-table-column label="收益" align="right">
        <template #default="{ row }">{{ formatPercent(row.return, 1) }}</template>
      </el-table-column>
      <el-table-column label="基准" align="right">
        <template #default="{ row }">{{ formatPercent(row.benchmark, 1) }}</template>
      </el-table-column>
      <el-table-column label="超额" align="right">
        <template #default="{ row }">{{ formatPercent(row.excess, 1) }}</template>
      </el-table-column>
      <el-table-column label="最大回撤" align="right">
        <template #default="{ row }">{{ formatPercent(row.max_drawdown, 1, false) }}</template>
      </el-table-column>
    </el-table>
    <ul class="rules">
      <li v-for="r in result.rules" :key="r">{{ r }}</li>
    </ul>
  </div>
</template>

<style scoped>
.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  margin: 0 0 8px;
}
.warn {
  margin-bottom: 6px;
}
.kpis {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(110px, 1fr));
  gap: 8px;
  margin: 8px 0;
}
.kpis div {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.kpis span {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.kpis b {
  font-size: 16px;
}
.years {
  margin-top: 8px;
}
.rules {
  margin: 8px 0 0;
  padding-left: 18px;
  color: var(--el-text-color-regular);
  font-size: 13px;
}
</style>
