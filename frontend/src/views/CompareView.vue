<script setup lang="ts">
/**
 * 对比分析（PRD US-6.4）：选 2～5 个选手（可以来自不同赛道），对比指标、雷达、收益曲线、日收益相关性、持仓重合度。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import VChart from 'vue-echarts'
import { errorMessage } from '@/api/http'
import { compareApi, tracksApi, type CompareResult, type Metrics, type NavSeries, type Track } from '@/api/arena'
import NavChart from '@/components/NavChart.vue'
import { useChartTheme } from '@/composables/useChartTheme'
import { formatPercent } from '@/utils/format'

const route = useRoute()
const router = useRouter()
const theme = useChartTheme()

const tracks = ref<Track[]>([])
const selected = ref<number[]>([])
const result = ref<CompareResult | null>(null)
const benchmark = ref<NavSeries['benchmark'] | null>(null)
const loading = ref(false)

// ---------------------------------------------------------------- 选择选手
async function loadTracks() {
  try {
    const list = await tracksApi.list()
    tracks.value = await Promise.all(list.filter((t) => t.status !== 'draft').map((t) => tracksApi.get(t.id)))
  } catch (err) {
    ElMessage.error(errorMessage(err))
  }
}

function selectTrack(t: Track) {
  selected.value = (t.accounts ?? []).slice(0, 5).map((a) => a.id)
}

async function load() {
  const ids = selected.value
  router.replace({ query: ids.length ? { ids: ids.join(',') } : {} })
  if (ids.length < 2) {
    result.value = null
    return
  }
  loading.value = true
  try {
    result.value = await compareApi.get(ids)
    // 同一赛道时叠加基准曲线
    const trackIds = new Set(result.value.accounts.map((a) => a.track_id))
    benchmark.value = trackIds.size === 1 ? (await tracksApi.nav([...trackIds][0])).benchmark : null
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    loading.value = false
  }
}

watch(selected, load)

onMounted(async () => {
  await loadTracks()
  const ids = String(route.query.ids ?? '')
    .split(',')
    .map(Number)
    .filter((x) => x > 0)
  if (ids.length) selected.value = ids.slice(0, 5)
})

const accounts = computed(() => result.value?.accounts ?? [])
const names = computed(() =>
  accounts.value.map((a) =>
    new Set(accounts.value.map((x) => x.track_id)).size > 1 ? `${a.agent_name}（${a.track_name}）` : a.agent_name,
  ),
)
const currencies = computed(() => new Set(accounts.value.map((a) => a.currency)))

// ---------------------------------------------------------------- 收益曲线
const navData = computed<NavSeries | null>(() => {
  if (!accounts.value.length) return null
  return {
    benchmark: benchmark.value ?? { symbol: '', name: '无（跨赛道）', points: [] },
    accounts: accounts.value.map((a, i) => ({
      account_id: a.account_id,
      name: names.value[i],
      kind: a.kind,
      points: a.nav,
    })),
  }
})

// ---------------------------------------------------------------- 雷达
const RADAR_DIMS = [
  { key: 'return', name: '收益' },
  { key: 'risk', name: '风险控制' },
  { key: 'stability', name: '稳定性' },
  { key: 'efficiency', name: '交易效率' },
  { key: 'cost', name: '成本效率' },
] as const

const radarOption = computed(() => {
  const t = theme.value
  return {
    backgroundColor: 'transparent',
    legend: { bottom: 0, textStyle: { color: t.textSecondary, fontSize: 12 } },
    tooltip: {
      backgroundColor: t.surface,
      borderColor: t.grid,
      textStyle: { color: t.textPrimary, fontSize: 12 },
    },
    radar: {
      radius: '62%',
      center: ['50%', '46%'],
      indicator: RADAR_DIMS.map((d) => ({ name: d.name, max: 100 })),
      axisName: { color: t.textSecondary, fontSize: 12 },
      splitLine: { lineStyle: { color: t.grid } },
      splitArea: { show: false },
      axisLine: { lineStyle: { color: t.grid } },
    },
    series: [
      {
        type: 'radar',
        symbolSize: 5,
        data: accounts.value.map((a, i) => ({
          name: names.value[i],
          // 无法评分的维度按 0 绘制，表格中显示为「-」
          value: RADAR_DIMS.map((d) => a.metrics.radar?.[d.key] ?? 0),
          lineStyle: { color: t.seriesColor(i), width: 2 },
          itemStyle: { color: t.seriesColor(i) },
          areaStyle: { color: t.seriesColor(i), opacity: 0.08 },
        })),
      },
    ],
  }
})

// ---------------------------------------------------------------- 指标表
type Fmt = 'pct' | 'pct0' | 'pct2' | 'num' | 'int' | 'x' | 'days' | 'money'
interface Row {
  group?: string
  key: string
  label: string
  fmt: Fmt
  better?: 'high' | 'low'
}
const ROWS: Row[] = [
  { group: '收益', key: 'cumulative_return', label: '累计收益', fmt: 'pct', better: 'high' },
  { key: 'annualized_return', label: '年化收益（≥60 日）', fmt: 'pct', better: 'high' },
  { key: 'excess_return', label: '超额收益', fmt: 'pct', better: 'high' },
  { group: '风险', key: 'volatility', label: '年化波动率', fmt: 'pct0', better: 'low' },
  { key: 'max_drawdown', label: '最大回撤', fmt: 'pct0', better: 'low' },
  { key: 'max_drawdown_days', label: '最长回撤期（交易日）', fmt: 'int', better: 'low' },
  { group: '风险调整', key: 'sharpe', label: '夏普比率', fmt: 'num', better: 'high' },
  { key: 'sortino', label: '索提诺比率', fmt: 'num', better: 'high' },
  { key: 'calmar', label: '卡玛比率', fmt: 'num', better: 'high' },
  { key: 'alpha', label: 'Alpha（年化）', fmt: 'pct', better: 'high' },
  { key: 'beta', label: 'Beta', fmt: 'num' },
  { key: 'information_ratio', label: '信息比率', fmt: 'num', better: 'high' },
  { group: '交易行为', key: 'trades', label: '成交笔数', fmt: 'int' },
  { key: 'win_rate', label: '胜率', fmt: 'pct0', better: 'high' },
  { key: 'profit_factor', label: '盈亏比', fmt: 'num', better: 'high' },
  { key: 'turnover', label: '年化换手率', fmt: 'x' },
  { key: 'avg_holding_days', label: '平均持仓天数', fmt: 'days' },
  { key: 'max_weight', label: '最大单票权重', fmt: 'pct0' },
  { key: 'hhi', label: '持仓集中度 HHI', fmt: 'num' },
  { key: 'fee_ratio', label: '费用侵蚀（费用 / 本金）', fmt: 'pct2', better: 'low' },
  { group: '杠杆', key: 'avg_leverage', label: '平均杠杆', fmt: 'x' },
  { key: 'max_leverage', label: '最大杠杆', fmt: 'x' },
  { key: 'margin_calls', label: '追保次数', fmt: 'int', better: 'low' },
  { key: 'forced_liquidations', label: '强平次数', fmt: 'int', better: 'low' },
  { group: 'AI', key: 'ai.valid_rate', label: '决策有效率', fmt: 'pct0', better: 'high' },
  { key: 'ai.avg_latency_s', label: '平均决策耗时（秒）', fmt: 'int', better: 'low' },
  { key: 'ai.reject_rate', label: '订单被拒率', fmt: 'pct0', better: 'low' },
  { key: 'ai.cost', label: 'AI 累计费用', fmt: 'money', better: 'low' },
  { key: 'ai.cost_benefit', label: '成本收益比（超额收益 / AI 费用）', fmt: 'num', better: 'high' },
  { key: 'ai.consistency_rate', label: '推理-行为一致率（评审抽样）', fmt: 'pct0', better: 'high' },
]

function value(m: Metrics, key: string): number | null {
  const v = key.startsWith('ai.')
    ? (m.ai as Record<string, unknown>)[key.slice(3)]
    : (m as unknown as Record<string, unknown>)[key]
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

function format(v: number | null, fmt: Fmt): string {
  if (v === null) return '-'
  switch (fmt) {
    case 'pct':
      return formatPercent(v)
    case 'pct0':
      return formatPercent(v, 1, false)
    case 'pct2':
      return formatPercent(v, 2, false)
    case 'int':
      return v.toFixed(0)
    case 'x':
      return `${v.toFixed(2)} 倍`
    case 'days':
      return `${v.toFixed(1)} 天`
    case 'money':
      return v.toFixed(2)
    default:
      return v.toFixed(2)
  }
}

const table = computed(() =>
  ROWS.map((row) => {
    const values = accounts.value.map((a) => value(a.metrics, row.key))
    const valid = values.filter((v): v is number => v !== null)
    let best: number | null = null
    if (row.better && valid.length > 1 && new Set(valid).size > 1) {
      best = row.better === 'high' ? Math.max(...valid) : Math.min(...valid)
    }
    return { ...row, cells: values.map((v) => ({ text: format(v, row.fmt), best: v !== null && v === best })) }
  }),
)

/** 实时与回放并列对比时，标出每个结果的前视风险（PRD 5.1 v1.0 退出标准） */
function modeTag(a: CompareResult['accounts'][number]): {
  label: string
  type: 'success' | 'danger' | 'warning' | 'info'
} {
  if (a.mode === 'live') return { label: '实时', type: 'success' }
  if (a.kind === 'baseline') return { label: '回放', type: 'info' }
  if (a.lookahead === 'high') return { label: '回放·前视风险高', type: 'danger' }
  if (a.lookahead === 'low') return { label: '回放·风险低', type: 'info' }
  return { label: '回放·风险未知', type: 'warning' }
}

function modeHint(a: CompareResult['accounts'][number]): string {
  if (a.mode === 'live') return '实时模拟：决策时还不知道之后的行情，没有前视偏差'
  if (a.kind === 'baseline') return '历史回放中的基线选手（规则策略，不涉及模型记忆）'
  if (a.lookahead === 'high') return '历史回放：模型知识截止日期晚于回放开始，模型可能在训练中见过这段行情'
  if (a.lookahead === 'low') return '历史回放：模型知识截止日期早于回放开始，前视风险较低'
  return '历史回放：未填写模型知识截止日期，无法判断前视风险'
}

// ---------------------------------------------------------------- 矩阵（相关性 / 重合度）
function matrixCell(v: number | null, diverging: boolean): Record<string, string> {
  if (v === null) return {}
  const t = theme.value
  // 发散色：正相关橙、负相关蓝；重合度只用蓝色深浅
  const color = diverging && v > 0 ? t.seriesColor(1) : t.seriesColor(0)
  const alpha = Math.round(Math.min(Math.abs(v), 1) * 70)
  return { background: `color-mix(in srgb, ${color} ${alpha}%, transparent)` }
}

const matrices = computed(() => {
  const r = result.value
  if (!r) return []
  return [
    {
      title: '日收益相关性',
      hint: '1 表示同涨同跌，0 表示无关，负数表示走势相反',
      data: r.correlation,
      diverging: true,
      fmt: (v: number) => v.toFixed(2),
    },
    {
      title: '当前持仓重合度',
      hint: '同方向持仓取两者权重较小值求和；0 表示完全不同，100% 表示完全相同',
      data: r.holding_overlap,
      diverging: false,
      fmt: (v: number) => formatPercent(v, 0, false),
    },
    {
      title: '交易标的重合度',
      hint: '交易过的标的交集 / 并集',
      data: r.traded_overlap,
      diverging: false,
      fmt: (v: number) => formatPercent(v, 0, false),
    },
  ]
})
</script>

<template>
  <div>
    <h2 class="sa-page-title">对比分析</h2>

    <el-card shadow="never" class="mb">
      <div class="picker">
        <el-select
          v-model="selected"
          multiple
          :multiple-limit="5"
          filterable
          collapse-tags-tooltip
          placeholder="选择 2～5 个选手（可以来自不同赛道）"
          style="flex: 1; min-width: 280px"
        >
          <el-option-group v-for="t in tracks" :key="t.id" :label="t.name">
            <el-option v-for="a in t.accounts ?? []" :key="a.id" :label="`${a.agent_name} · ${t.name}`" :value="a.id" />
          </el-option-group>
        </el-select>
        <el-dropdown trigger="click" @command="(t: Track) => selectTrack(t)">
          <el-button>选择整个赛道</el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item v-for="t in tracks" :key="t.id" :command="t">{{ t.name }}</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </div>
      <p v-if="currencies.size > 1" class="muted small">
        注意：所选选手的计价货币不同，金额类指标不能直接比较，收益率可以比较。
      </p>
    </el-card>

    <el-empty v-if="selected.length < 2" description="请至少选择 2 个选手" />
    <div v-else v-loading="loading">
      <template v-if="result">
        <div class="grid2">
          <el-card shadow="never" header="收益曲线">
            <NavChart :data="navData" :height="340" />
          </el-card>
          <el-card shadow="never">
            <template #header>
              <span>五维评分</span>
              <el-tooltip
                placement="top"
                content="按固定区间换算为 0～100：收益（超额 ±10%）、风险控制（回撤 0～20%）、稳定性（夏普 −2～2）、交易效率（胜率与盈亏比）、成本效率（费用占本金 0～2%）。样本不足的维度按 0 显示。"
              >
                <el-icon class="help"><QuestionFilled /></el-icon>
              </el-tooltip>
            </template>
            <v-chart :option="radarOption" autoresize style="height: 340px" />
          </el-card>
        </div>

        <el-card shadow="never" header="指标对比" class="mb">
          <el-table :data="table" size="small" row-key="key">
            <el-table-column label="指标" min-width="200">
              <template #default="{ row }">
                <span v-if="row.group" class="group">{{ row.group }}</span
                >{{ row.label }}
              </template>
            </el-table-column>
            <el-table-column
              v-for="(a, i) in accounts"
              :key="a.account_id"
              :label="names[i]"
              align="right"
              min-width="130"
            >
              <template #header>
                <span class="dot" :style="{ background: theme.seriesColor(i) }" />{{ names[i] }}
                <el-tooltip :content="modeHint(a)">
                  <el-tag size="small" :type="modeTag(a).type" class="mode">{{ modeTag(a).label }}</el-tag>
                </el-tooltip>
              </template>
              <template #default="{ row }">
                <span class="num" :class="{ best: row.cells[i].best }">{{ row.cells[i].text }}</span>
              </template>
            </el-table-column>
          </el-table>
          <p class="muted small">加粗为该项最优；运行不足 20 个交易日时指标主要反映短期波动，仅供参考。</p>
        </el-card>

        <div class="grid3 mb">
          <el-card v-for="mx in matrices" :key="mx.title" shadow="never">
            <template #header>
              <span>{{ mx.title }}</span>
              <el-tooltip placement="top" :content="mx.hint"
                ><el-icon class="help"><QuestionFilled /></el-icon
              ></el-tooltip>
            </template>
            <table class="matrix">
              <thead>
                <tr>
                  <th />
                  <th v-for="(_, j) in accounts" :key="j">
                    <span class="dot" :style="{ background: theme.seriesColor(j) }" />
                  </th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(row, i) in mx.data" :key="i">
                  <th class="rowname">
                    <span class="dot" :style="{ background: theme.seriesColor(i) }" />{{ names[i] }}
                  </th>
                  <td v-for="(v, j) in row" :key="j" :style="i === j ? {} : matrixCell(v, mx.diverging)">
                    {{ i === j ? '—' : v === null ? '-' : mx.fmt(v) }}
                  </td>
                </tr>
              </tbody>
            </table>
          </el-card>
        </div>

        <el-card v-if="result.repeats.length" shadow="never" class="mb">
          <template #header>
            <span>重复实验（同一选手在多个赛道的结果）</span>
            <el-tooltip
              placement="top"
              content="同一个选手（同一模型与设置）多次参赛时，收益的均值和标准差反映结果的稳定性；标准差大说明单次结果主要受随机性影响。可在赛道页「复制赛道」快速重复实验。"
            >
              <el-icon class="help"><QuestionFilled /></el-icon>
            </el-tooltip>
          </template>
          <el-table :data="result.repeats" size="small">
            <el-table-column prop="agent_name" label="选手" min-width="150" />
            <el-table-column prop="runs" label="次数" width="60" align="right" />
            <el-table-column label="累计收益：均值 ± 标准差" min-width="180" align="right">
              <template #default="{ row }">
                <span v-if="row.cumulative_return"
                  >{{ formatPercent(row.cumulative_return.mean) }} ±
                  {{ formatPercent(row.cumulative_return.std, 2, false) }}</span
                >
              </template>
            </el-table-column>
            <el-table-column label="最差 / 最好" min-width="150" align="right">
              <template #default="{ row }">
                <span v-if="row.cumulative_return"
                  >{{ formatPercent(row.cumulative_return.min) }} / {{ formatPercent(row.cumulative_return.max) }}</span
                >
              </template>
            </el-table-column>
            <el-table-column label="超额收益均值" width="120" align="right">
              <template #default="{ row }">{{
                row.excess_return ? formatPercent(row.excess_return.mean) : '-'
              }}</template>
            </el-table-column>
            <el-table-column label="最大回撤均值" width="120" align="right">
              <template #default="{ row }">{{
                row.max_drawdown ? formatPercent(row.max_drawdown.mean, 2, false) : '-'
              }}</template>
            </el-table-column>
            <el-table-column label="赛道" min-width="220" show-overflow-tooltip>
              <template #default="{ row }">{{ row.tracks.join('、') }}</template>
            </el-table-column>
          </el-table>
        </el-card>

        <el-card shadow="never" header="当前持仓构成（占净值）">
          <el-table :data="accounts" size="small">
            <el-table-column label="选手" min-width="180">
              <template #default="{ $index }"
                ><span class="dot" :style="{ background: theme.seriesColor($index) }" />{{ names[$index] }}</template
              >
            </el-table-column>
            <el-table-column label="股票多头" align="right"
              ><template #default="{ row }">{{
                formatPercent(row.composition.stock_long, 1, false)
              }}</template></el-table-column
            >
            <el-table-column label="ETF 多头" align="right"
              ><template #default="{ row }">{{
                formatPercent(row.composition.etf_long, 1, false)
              }}</template></el-table-column
            >
            <el-table-column label="空头" align="right"
              ><template #default="{ row }">{{
                formatPercent(row.composition.short, 1, false)
              }}</template></el-table-column
            >
            <el-table-column label="现金" align="right"
              ><template #default="{ row }">{{
                formatPercent(row.composition.cash, 1, false)
              }}</template></el-table-column
            >
          </el-table>
          <p class="muted small">按标的类型统计（暂无行业分类数据）；使用杠杆时多空合计可以超过 100%。</p>
        </el-card>
      </template>
    </div>
  </div>
</template>

<style scoped>
.mb {
  margin-bottom: 16px;
}

.picker {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.grid2 {
  display: grid;
  grid-template-columns: minmax(0, 3fr) minmax(0, 2fr);
  gap: 16px;
  margin-bottom: 16px;
}

.grid3 {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
  gap: 16px;
}

@media (max-width: 900px) {
  .grid2 {
    grid-template-columns: minmax(0, 1fr);
  }
}

.group {
  display: inline-block;
  min-width: 64px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.mode {
  margin-left: 4px;
}

.best {
  font-weight: 700;
}

.num {
  font-variant-numeric: tabular-nums;
}

.dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  margin-right: 6px;
  vertical-align: middle;
}

.matrix {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}

.matrix th,
.matrix td {
  padding: 6px 4px;
  text-align: center;
  border-bottom: 1px solid var(--el-border-color-lighter);
}

.matrix .rowname {
  text-align: left;
  font-weight: normal;
  white-space: nowrap;
  max-width: 140px;
  overflow: hidden;
  text-overflow: ellipsis;
}

.help {
  margin-left: 4px;
  vertical-align: middle;
  color: var(--el-text-color-secondary);
  cursor: help;
}

.muted {
  color: var(--el-text-color-secondary);
}

.small {
  font-size: 12px;
  margin: 8px 0 0;
}
</style>
