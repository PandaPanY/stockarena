<script setup lang="ts">
/** 一个策略版本的完整报告：规则、三段区间指标、收益曲线、分年收益、稳健性与诊断。 */
import { computed } from 'vue'
import type { VersionDetail, WindowReport } from '@/api/quant'
import LabCurve from '@/components/lab/LabCurve.vue'
import { formatPercent } from '@/utils/format'

const props = defineProps<{ version: VersionDetail; name?: string }>()

const windows = computed(() => [
  { key: '开发期', rep: props.version.dev_full, hint: '训练 + 验证，升级检验在这一段上逐年进行' },
  { key: '其中训练', rep: props.version.train_full, hint: '开发期的前一部分' },
  { key: '其中验证', rep: props.version.valid_full, hint: '开发期的后一部分' },
  { key: '留出', rep: props.version.holdout_full, hint: '从不参与优化' },
])

type Row = { label: string; get: (r: WindowReport) => string; tip?: string }
const pct = (v: number | null | undefined, d = 1) => formatPercent(v ?? null, d)
const num = (v: number | null | undefined, d = 2) => (v === null || v === undefined ? '-' : v.toFixed(d))
const rows: Row[] = [
  { label: '区间', get: (r) => (r.start ? `${r.start} ～ ${r.end}` : '-') },
  { label: '累计收益', get: (r) => pct(r.cumulative_return) },
  { label: '年化收益', get: (r) => pct(r.annualized_return) },
  { label: '基准收益', get: (r) => pct(r.benchmark_return) },
  { label: '超额收益', get: (r) => pct(r.excess_return) },
  { label: '夏普比率', get: (r) => num(r.sharpe) },
  { label: '卡玛比率', get: (r) => num(r.calmar), tip: '年化收益 / 最大回撤' },
  { label: '最大回撤', get: (r) => formatPercent(r.max_drawdown ?? null, 1, false) },
  { label: '年化波动', get: (r) => formatPercent(r.volatility ?? null, 1, false) },
  {
    label: '年化换手',
    get: (r) => (r.turnover === null || r.turnover === undefined ? '-' : `${r.turnover.toFixed(1)} 倍`),
  },
  { label: '年化成本', get: (r) => formatPercent(r.cost_ratio ?? null, 2, false) },
  { label: '清仓胜率', get: (r) => formatPercent(r.win_rate ?? null, 0, false) },
  {
    label: '平均持仓',
    get: (r) => (r.avg_holdings === null || r.avg_holdings === undefined ? '-' : `${r.avg_holdings} 只`),
  },
  { label: '平均仓位', get: (r) => formatPercent(r.avg_exposure ?? null, 0, false) },
]

const years = computed(() => {
  const all = [
    ...(props.version.train_full.yearly ?? []).map((y) => ({ ...y, window: '训练' })),
    ...(props.version.valid_full.yearly ?? []).map((y) => ({ ...y, window: '验证' })),
    ...(props.version.holdout_full.yearly ?? []).map((y) => ({ ...y, window: '留出' })),
  ]
  return all
})

const rb = computed(() => props.version.robustness)
const tuning = computed(() => props.version.train_full.tuning)
const vs = computed(() => props.version.dev_full?.vs_champion ?? null)
const dg = computed(() => props.version.dev_full?.diagnostics ?? props.version.train_full.diagnostics)
const cls = (v: number | null | undefined) => (v === null || v === undefined ? '' : v >= 0 ? 'sa-up' : 'sa-down')
</script>

<template>
  <div class="report">
    <el-row :gutter="16">
      <el-col :xs="24" :lg="10">
        <h4>策略规则</h4>
        <ol class="rules">
          <li v-for="(r, i) in version.rules" :key="i">{{ r }}</li>
        </ol>
        <div v-if="version.rationale" class="rationale"><strong>改动理由：</strong>{{ version.rationale }}</div>
        <div v-if="tuning" class="decision">
          <strong>程序调参：</strong>
          <span v-if="tuning.changes.length">{{ tuning.changes.join('；') }}</span>
          <span v-else>保持 AI 给出的参数</span>
          <span class="muted">（训练区间，回测 {{ tuning.evals }} 次{{ tuning.note ? `，${tuning.note}` : '' }}）</span>
        </div>
        <div v-if="version.decision" class="decision"><strong>检验结论：</strong>{{ version.decision }}</div>
      </el-col>
      <el-col :xs="24" :lg="14">
        <LabCurve :curves="version.curves" :height="300" :name="name ?? `v${version.number}`" />
      </el-col>
    </el-row>

    <h4>区间表现</h4>
    <el-table :data="rows" size="small" border class="metrics">
      <el-table-column label="指标" width="110">
        <template #default="{ row }">
          <el-tooltip v-if="row.tip" :content="row.tip"
            ><span class="tip">{{ row.label }}</span></el-tooltip
          >
          <span v-else>{{ row.label }}</span>
        </template>
      </el-table-column>
      <el-table-column v-for="w in windows" :key="w.key" align="right">
        <template #header>
          <el-tooltip :content="w.hint"
            ><span class="tip">{{ w.key }}</span></el-tooltip
          >
        </template>
        <template #default="{ row }">{{ w.rep && w.rep.days ? row.get(w.rep) : '-' }}</template>
      </el-table-column>
    </el-table>

    <el-row :gutter="16" class="lower">
      <el-col :xs="24" :lg="12">
        <h4>分年收益</h4>
        <el-table :data="years" size="small" max-height="320">
          <el-table-column label="年份" width="90">
            <template #default="{ row }">{{ row.year }}{{ row.partial ? '*' : '' }}</template>
          </el-table-column>
          <el-table-column prop="window" label="区间" width="60" />
          <el-table-column label="策略" align="right">
            <template #default="{ row }"
              ><span :class="cls(row.return)">{{ pct(row.return) }}</span></template
            >
          </el-table-column>
          <el-table-column label="基准" align="right">
            <template #default="{ row }">{{ pct(row.benchmark) }}</template>
          </el-table-column>
          <el-table-column label="超额" align="right">
            <template #default="{ row }"
              ><span :class="cls(row.excess)">{{ pct(row.excess) }}</span></template
            >
          </el-table-column>
          <el-table-column label="回撤" align="right">
            <template #default="{ row }">{{ formatPercent(row.max_drawdown, 1, false) }}</template>
          </el-table-column>
        </el-table>
        <div class="muted">* 不满一年</div>
      </el-col>
      <el-col :xs="24" :lg="12">
        <template v-if="vs">
          <h4>升级检验（与当时的冠军相比）</h4>
          <p class="muted">
            开发期 {{ vs.months }} 个月，候选收益按两者波动之比换算到冠军的波动水平后比较： 年化超额
            <span :class="cls(vs.excess_annual)">{{ pct(vs.excess_annual) }}</span
            >， 单侧 p = {{ vs.p.toFixed(3) }}（同轮 {{ vs.n_tests }} 个候选）；{{ vs.years }} 年中
            {{ vs.year_wins }} 年跑赢。
          </p>
          <div class="segs">
            <div v-for="(d, y) in vs.by_year" :key="y" class="seg">
              <span class="muted">{{ y }}</span>
              <span :class="cls(d)">{{ pct(d) }}</span>
            </div>
          </div>
        </template>
        <h4>稳健性</h4>
        <p v-if="rb && rb.samples" class="muted">
          所有数值参数同时随机浮动 ±20%，重复 {{ rb.samples }} 次：开发期夏普中位数 {{ num(rb.median_sharpe) }}（范围
          {{ num(rb.min_sharpe) }} ～ {{ num(rb.max_sharpe) }}）<span v-if="rb.ratio !== null && rb.ratio !== undefined"
            >，为原值的 {{ formatPercent(rb.ratio, 0, false) }}</span
          >。
        </p>
        <p v-else class="muted">未做稳健性检验（开发期夏普不高于冠军的候选直接淘汰）。</p>
        <template v-if="dg">
          <h4>开发期诊断</h4>
          <div v-if="dg.drawdowns.length" class="muted">
            最深回撤：
            <span v-for="d in dg.drawdowns" :key="d.start" class="dd">
              {{ d.start }} → {{ d.trough }}（{{ formatPercent(d.depth, 1, false)
              }}{{ d.recovered ? '' : '，未恢复' }}）
            </span>
          </div>
          <div class="muted">
            盈利最多：{{ dg.best.map((x) => `${x.name} ${(x.pnl / 1e4).toFixed(1)} 万`).join('、') || '-' }}
          </div>
          <div class="muted">
            亏损最多：{{ dg.worst.map((x) => `${x.name} ${(x.pnl / 1e4).toFixed(1)} 万`).join('、') || '-' }}
          </div>
        </template>
      </el-col>
    </el-row>
  </div>
</template>

<style scoped>
h4 {
  margin: 12px 0 8px;
}

.rules {
  margin: 0;
  padding-left: 20px;
  line-height: 1.8;
  font-size: 13px;
}

.rationale,
.decision {
  margin-top: 8px;
  font-size: 13px;
  color: var(--el-text-color-regular);
}

.metrics :deep(.cell) {
  font-variant-numeric: tabular-nums;
}

.tip {
  border-bottom: 1px dashed var(--el-text-color-placeholder);
  cursor: help;
}

.lower {
  margin-top: 4px;
}

.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 1.8;
}

.dd {
  margin-right: 8px;
}

.segs {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(110px, 1fr));
  gap: 8px;
}

.seg {
  display: flex;
  flex-direction: column;
  font-variant-numeric: tabular-nums;
}
</style>
