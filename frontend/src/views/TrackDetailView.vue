<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { errorMessage } from '@/api/http'
import {
  agentsApi,
  tracksApi,
  type Agent,
  type EventItem,
  type LeaderboardRow,
  type NavSeries,
  type Round,
  type Track,
} from '@/api/arena'
import ConsistencyPanel from '@/components/ConsistencyPanel.vue'
import NavChart from '@/components/NavChart.vue'
import { formatDateTime, formatMoney, formatPercent } from '@/utils/format'

const route = useRoute()
const router = useRouter()
const trackId = computed(() => Number(route.params.id))

const track = ref<Track | null>(null)
const board = ref<LeaderboardRow[]>([])
const nav = ref<NavSeries | null>(null)
const rounds = ref<Round[]>([])
const events = ref<EventItem[]>([])
const allAgents = ref<Agent[]>([])
const tab = ref('rounds')
const acting = ref(false)
let timer: ReturnType<typeof setTimeout> | undefined

const STATUS_TYPES = { draft: 'info', running: 'success', paused: 'warning', finished: 'primary' } as const
const TRIGGER_LABELS: Record<string, string> = {
  manual: '手动',
  replay: '回放',
  schedule: '收盘后',
  interval: '定时',
  event: '异动',
}
const MARKET_TZ: Record<string, string> = { CN: 'Asia/Shanghai', HK: 'Asia/Hong_Kong', US: 'America/New_York' }
const ROUND_STATUS: Record<string, { label: string; type: 'success' | 'info' | 'warning' | 'danger' }> = {
  done: { label: '完成', type: 'success' },
  running: { label: '进行中', type: 'warning' },
  missed: { label: '错过', type: 'info' },
  failed: { label: '失败', type: 'danger' },
}
const PARAM_LABELS: Record<string, string> = {
  slippage_bps: '滑点（基点）',
  participation_rate: '成交量参与率上限',
  max_position_pct: '单票持仓上限',
  max_orders_per_round: '单轮订单数上限',
  rebalance_threshold_pct: '调仓忽略阈值',
  cash_buffer_pct: '预留现金比例',
  commission_rate: '佣金费率',
  commission_min: '最低佣金（元）',
  stamp_duty_rate: '印花税（卖出）',
  transfer_fee_rate: '过户费',
  dividend_tax_rate: '红利税',
  allow_short: '允许做空',
  allow_margin: '允许融资',
  max_leverage: '最大杠杆',
  allow_leveraged_etf: '允许杠杆 / 反向 ETF',
  margin_check_method: '保证金比率口径',
  initial_margin: '初始保证金率',
  maintenance_margin: '维持线',
  liquidation_margin: '强平线',
  liquidation_buffer: '强平恢复缓冲',
  margin_interest_rate: '融资年利率',
  borrow_fee_rate: '融券年费率',
  day_count: '计息天数基准',
  cn_short_t1: 'A股融券当日不可还',
}

const busy = computed(() => !!track.value && (track.value.busy || !!track.value.last_run?.running))
async function cloneTrack() {
  acting.value = true
  try {
    const copy = await tracksApi.clone(trackId.value)
    ElMessage.success('已复制为草稿赛道，确认后点击「启动比赛」')
    router.push({ name: 'track', params: { id: copy.id } })
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    acting.value = false
  }
}

const EXPORT_KINDS = [
  { kind: 'orders', label: '订单' },
  { kind: 'fills', label: '成交' },
  { kind: 'nav', label: '每日净值' },
  { kind: 'decisions', label: '决策记录' },
]

/** 导出走浏览器下载（同源接口，开发时经 Vite 代理） */
function exportData(command: string) {
  const base = `/api/tracks/${trackId.value}`
  if (command === 'report') return window.open(`${base}/report`, '_blank')
  if (command === 'report-download') return window.open(`${base}/report?download=true`, '_self')
  const [kind, fmt] = command.split('-')
  window.open(`${base}/export?kind=${kind}&fmt=${fmt ?? 'csv'}`, '_self')
}

const lookaheadHigh = computed(() => (track.value?.lookahead ?? []).some((r) => r.level === 'high'))
const leveraged = computed(() => !!(track.value?.rule_params?.allow_short || track.value?.rule_params?.allow_margin))
const candidates = computed(() => {
  const enrolled = new Set(track.value?.accounts?.map((a) => a.agent_id))
  return allAgents.value.filter((a) => !enrolled.has(a.id))
})
const enrollAgent = ref<number | null>(null)

async function load() {
  try {
    const id = trackId.value
    const t = await tracksApi.get(id)
    track.value = t
    if (t.status === 'draft') {
      allAgents.value = await agentsApi.list()
      board.value = []
      nav.value = null
    } else {
      ;[board.value, nav.value, rounds.value, events.value] = await Promise.all([
        tracksApi.leaderboard(id),
        tracksApi.nav(id),
        tracksApi.rounds(id),
        tracksApi.events(id),
      ])
    }
  } catch (err) {
    ElMessage.error(errorMessage(err))
  }
  schedule()
}

function schedule() {
  clearTimeout(timer)
  // 执行中每 5 秒刷新，否则每分钟刷新
  timer = setTimeout(load, busy.value ? 5000 : 60000)
}

async function act(fn: () => Promise<unknown>, ok: string) {
  acting.value = true
  try {
    await fn()
    ElMessage.success(ok)
    await load()
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    acting.value = false
  }
}

async function start() {
  const text =
    track.value?.mode === 'replay'
      ? `将从 ${track.value.start_date} 起用历史行情逐日回放到最近一个收盘日，每个交易日都会请所有 AI 选手决策（本地模型每天约需数分钟），回放在后台进行，可以随时暂停。完成后转为实时模式继续运行。确定启动？`
      : '启动后股票池、规则和选手将被冻结，不能再修改。系统会在每个交易日收盘后自动结算并请各选手决策。确定启动？'
  await ElMessageBox.confirm(text, '启动赛道', { type: 'warning', confirmButtonText: '启动' })
  await act(() => tracksApi.start(trackId.value), '赛道已启动，正在准备首轮决策')
}

async function stop() {
  await ElMessageBox.confirm('结束后赛道不再结算和决策，且不能恢复。确定结束？', '结束赛道', { type: 'warning' })
  await act(() => tracksApi.action(trackId.value, 'stop'), '赛道已结束')
}

async function forceRound() {
  await ElMessageBox.confirm(
    '将用最新收盘数据再发起一轮决策（即使今天已经决策过），新订单会在下一交易日撮合。通常只用于调试。确定？',
    '强制新一轮决策',
    { type: 'warning' },
  )
  await act(() => tracksApi.run(trackId.value, true), '已开始执行')
}

async function remove() {
  await ElMessageBox.confirm('确定删除这个未启动的赛道？', '删除赛道', { type: 'warning' })
  await act(() => tracksApi.remove(trackId.value), '已删除')
  router.push({ name: 'tracks' })
}

async function enroll() {
  if (!enrollAgent.value) return
  await act(() => tracksApi.enroll(trackId.value, enrollAgent.value as number), '已加入')
  enrollAgent.value = null
}

const snapshotVisible = ref(false)
const snapshot = ref<{ table: string | null; snapshot_hash: string; data_date: string } | null>(null)
async function showSnapshot(r: Round) {
  try {
    snapshot.value = await tracksApi.snapshot(trackId.value, r.id)
    snapshotVisible.value = true
  } catch (err) {
    ElMessage.error(errorMessage(err))
  }
}

const snapshotRows = computed(() => {
  const lines = snapshot.value?.table?.split('\n') ?? []
  const header = lines[0]?.split(',') ?? []
  return { header, rows: lines.slice(1).map((l) => l.split(',')) }
})

function pctClass(v: number | null | undefined) {
  if (v === null || v === undefined) return ''
  return v >= 0 ? 'sa-up' : 'sa-down'
}

watch(trackId, load)
onMounted(load)
onBeforeUnmount(() => clearTimeout(timer))
</script>

<template>
  <div v-if="track">
    <div class="header">
      <div>
        <el-button link icon="ArrowLeft" @click="router.push({ name: 'tracks' })">赛道列表</el-button>
        <h2 class="sa-page-title title">
          {{ track.name }}
          <el-tag :type="STATUS_TYPES[track.status]" size="small">{{ track.status_label }}</el-tag>
          <el-tag v-if="track.mode === 'replay'" size="small" type="warning" effect="plain">
            {{ track.replay_pending ? '历史回放中' : '历史回放 → 实时' }}
          </el-tag>
        </h2>
        <div v-if="track.last_run" class="last-run">
          <el-icon v-if="track.last_run.running" class="is-loading"><Loading /></el-icon>
          {{ track.last_run.message }}
        </div>
      </div>
      <div class="actions">
        <template v-if="track.status === 'draft'">
          <el-button type="danger" plain :loading="acting" @click="remove">删除</el-button>
          <el-button type="primary" :loading="acting" icon="VideoPlay" @click="start">启动比赛</el-button>
        </template>
        <template v-else-if="track.status === 'running' || track.status === 'paused'">
          <el-button
            :loading="acting || busy"
            icon="Refresh"
            @click="act(() => tracksApi.run(trackId), '已开始检查与执行')"
          >
            立即检查
          </el-button>
          <el-button v-if="track.status === 'running' && !track.replay_pending" :loading="acting" @click="forceRound"
            >强制新一轮</el-button
          >
          <el-button
            v-if="track.status === 'running'"
            :loading="acting"
            icon="VideoPause"
            @click="act(() => tracksApi.action(trackId, 'pause'), '已暂停')"
            >暂停</el-button
          >
          <el-button
            v-else
            type="success"
            :loading="acting"
            icon="VideoPlay"
            @click="act(() => tracksApi.action(trackId, 'resume'), '已恢复')"
            >恢复</el-button
          >
          <el-button type="danger" plain :loading="acting" @click="stop">结束</el-button>
        </template>
        <el-tooltip content="复制为新的草稿赛道（相同股票池、规则、选手与设置），用于重复实验或改设置重跑">
          <el-button v-if="track.status !== 'draft'" :loading="acting" icon="CopyDocument" @click="cloneTrack"
            >复制赛道</el-button
          >
        </el-tooltip>
      </div>
    </div>

    <el-alert
      v-if="track.mode === 'replay' && track.status !== 'draft'"
      :type="lookaheadHigh ? 'error' : 'warning'"
      :closable="false"
      show-icon
      class="mb"
      :title="`${track.start_date} ～ ${track.replay_until} 为历史回放：模型可能已在训练中见过这段行情，这部分收益不代表 AI 的真实水平。`"
    >
      <div v-for="r in track.lookahead" :key="r.account_id" class="small">
        <b>{{ r.agent_name }}</b
        >：
        <template v-if="r.level === 'high'">知识截止 {{ r.cutoff }}，晚于回放开始，<b>前视偏差风险高</b></template>
        <template v-else-if="r.level === 'unknown'"
          >未填写模型知识截止日期，无法判断（可在「配置中心 → 模型供应商」中填写）</template
        >
        <template v-else>知识截止 {{ r.cutoff }}，早于回放开始，前视风险低</template>
      </div>
      <div v-if="track.anonymize" class="small">已开启代码匿名化：prompt 中的证券代码、名称和日期已替换为代号。</div>
    </el-alert>

    <el-card shadow="never" class="mb">
      <el-descriptions :column="4" size="small">
        <el-descriptions-item label="股票池"
          >{{ track.universe_name }}（{{ track.symbol_count || '启动时确定' }} 个标的）</el-descriptions-item
        >
        <el-descriptions-item label="基准">{{ track.benchmark_name }}</el-descriptions-item>
        <el-descriptions-item label="规则集">
          {{ track.rule_set_title }}
          <el-tag v-if="track.rule_params?.allow_short" size="small" type="danger" class="ml">做空</el-tag>
          <el-tag v-if="track.rule_params?.allow_margin" size="small" type="danger" class="ml">融资</el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="初始本金"
          >{{ formatMoney(track.initial_cash, 0) }} {{ track.currency }}</el-descriptions-item
        >
        <el-descriptions-item label="决策方式">
          {{ track.output_mode === 'targets' ? '目标仓位' : '直接下单' }} ·
          {{ track.decision_mode === 'tools' ? `工具调用（每轮最多 ${track.max_tool_calls} 次）` : '快照' }}
          <template v-if="track.data_options?.fundamentals"> · 附带财报</template>
          <template v-if="track.data_options?.news"> · 附带新闻</template> ·
          {{ track.frequency === 'intraday' ? `分钟级，每 ${track.interval_min} 分钟` : '日频' }}
          <template v-if="track.frequency === 'intraday' && track.event_move_pct">
            · 异动 {{ (track.event_move_pct * 100).toFixed(1) }}% 触发
          </template>
        </el-descriptions-item>
        <el-descriptions-item label="决策时限">{{ track.decision_timeout_s }} 秒</el-descriptions-item>
        <el-descriptions-item :label="track.mode === 'replay' ? '回放开始' : '首轮决策'">{{
          track.start_date ?? '—'
        }}</el-descriptions-item>
        <el-descriptions-item label="最近结算">{{ track.last_settled_date ?? '—' }}</el-descriptions-item>
      </el-descriptions>
    </el-card>

    <!-- 未启动：管理选手 -->
    <el-card v-if="track.status === 'draft'" shadow="never" header="参赛选手">
      <el-table :data="track.accounts">
        <el-table-column prop="agent_name" label="选手" />
        <el-table-column label="类型" width="100">
          <template #default="{ row }">
            <el-tag size="small" :type="row.agent_kind === 'llm' ? 'primary' : 'info'">{{
              row.agent_kind === 'llm' ? 'AI' : '基线'
            }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column width="100">
          <template #default="{ row }">
            <el-button link type="danger" @click="act(() => tracksApi.withdraw(trackId, row.id), '已移除')"
              >移除</el-button
            >
          </template>
        </el-table-column>
      </el-table>
      <div class="enroll">
        <el-select v-model="enrollAgent" placeholder="添加选手" style="width: 240px">
          <el-option v-for="a in candidates" :key="a.id" :label="a.name" :value="a.id" />
        </el-select>
        <el-button :disabled="!enrollAgent" @click="enroll">加入</el-button>
      </div>
    </el-card>

    <template v-else>
      <el-card shadow="never" class="mb">
        <template #header>
          <div class="card-header">
            <span>排行榜</span>
            <span v-if="board.length && !board[0].reliable" class="muted small">
              已运行 {{ board[0].days }} 个交易日，少于 20 日，结果主要反映短期波动，仅供参考
            </span>
            <span class="actions">
              <el-button
                v-if="board.length >= 2"
                size="small"
                icon="Histogram"
                @click="
                  router.push({
                    name: 'compare',
                    query: {
                      ids: board
                        .slice(0, 5)
                        .map((b) => b.account_id)
                        .join(','),
                    },
                  })
                "
              >
                对比
              </el-button>
              <el-dropdown trigger="click" @command="exportData">
                <el-button size="small" icon="Download">导出</el-button>
                <template #dropdown>
                  <el-dropdown-menu>
                    <el-dropdown-item command="report">查看 HTML 报告</el-dropdown-item>
                    <el-dropdown-item command="report-download">下载 HTML 报告</el-dropdown-item>
                    <el-dropdown-item v-for="k in EXPORT_KINDS" :key="k.kind" :command="k.kind" divided>
                      {{ k.label }}（CSV）
                    </el-dropdown-item>
                    <el-dropdown-item v-for="k in EXPORT_KINDS" :key="`${k.kind}-json`" :command="`${k.kind}-json`">
                      {{ k.label }}（JSON）
                    </el-dropdown-item>
                  </el-dropdown-menu>
                </template>
              </el-dropdown>
            </span>
          </div>
        </template>
        <el-table :data="board" :default-sort="{ prop: 'rank', order: 'ascending' }">
          <el-table-column prop="rank" label="#" width="50" sortable />
          <el-table-column label="选手" min-width="170">
            <template #default="{ row }">
              <router-link :to="{ name: 'account', params: { id: row.account_id } }" class="link">{{
                row.agent_name
              }}</router-link>
              <el-tag v-if="row.kind === 'baseline'" size="small" type="info" class="ml">基线</el-tag>
              <el-tooltip
                v-else-if="row.reflect_enabled || row.strategy_version"
                placement="top"
                :content="row.reflect_enabled ? '自我进化：定期复盘并改写策略笔记' : '使用策略笔记（未开启自动复盘）'"
              >
                <el-tag size="small" :type="row.reflect_enabled ? 'success' : 'info'" class="ml">
                  {{ row.reflect_enabled ? '进化' : '策略'
                  }}{{ row.strategy_version ? ` v${row.strategy_version}` : '' }}
                </el-tag>
              </el-tooltip>
            </template>
          </el-table-column>
          <el-table-column label="净值" min-width="110" align="right" sortable :sort-by="(r: LeaderboardRow) => r.nav">
            <template #default="{ row }"
              ><span class="num">{{ formatMoney(row.nav) }}</span></template
            >
          </el-table-column>
          <el-table-column
            label="累计收益"
            width="110"
            align="right"
            sortable
            :sort-by="(r: LeaderboardRow) => r.cumulative_return ?? 0"
          >
            <template #default="{ row }"
              ><span class="num" :class="pctClass(row.cumulative_return)">{{
                formatPercent(row.cumulative_return)
              }}</span></template
            >
          </el-table-column>
          <el-table-column
            label="超额收益"
            width="110"
            align="right"
            sortable
            :sort-by="(r: LeaderboardRow) => r.excess_return ?? 0"
          >
            <template #default="{ row }"
              ><span class="num" :class="pctClass(row.excess_return)">{{
                formatPercent(row.excess_return)
              }}</span></template
            >
          </el-table-column>
          <el-table-column
            label="最大回撤"
            width="100"
            align="right"
            sortable
            :sort-by="(r: LeaderboardRow) => r.max_drawdown ?? 0"
          >
            <template #default="{ row }"
              ><span class="num">{{ formatPercent(row.max_drawdown, 2, false) }}</span></template
            >
          </el-table-column>
          <el-table-column label="夏普" width="80" align="right">
            <template #default="{ row }"
              ><span class="num">{{ row.sharpe?.toFixed(2) ?? '-' }}</span></template
            >
          </el-table-column>
          <el-table-column v-if="leveraged" label="最大杠杆" width="90" align="right">
            <template #default="{ row }">
              <span class="num">{{ row.max_leverage?.toFixed(2) ?? '-' }}</span>
              <el-tooltip
                v-if="row.forced_liquidations"
                :content="`追保 ${row.margin_calls} 次，强平 ${row.forced_liquidations} 次`"
              >
                <el-tag size="small" type="danger" class="ml">强平</el-tag>
              </el-tooltip>
            </template>
          </el-table-column>
          <el-table-column label="成交" width="70" align="right">
            <template #default="{ row }"
              ><span class="num">{{ row.trades }}</span></template
            >
          </el-table-column>
          <el-table-column label="胜率" width="80" align="right">
            <template #default="{ row }"
              ><span class="num">{{ formatPercent(row.win_rate, 0, false) }}</span></template
            >
          </el-table-column>
          <el-table-column label="AI 有效率" width="90" align="right">
            <template #default="{ row }"
              ><span class="num">{{ row.ai.llm ? formatPercent(row.ai.valid_rate, 0, false) : '—' }}</span></template
            >
          </el-table-column>
          <el-table-column label="AI 费用" width="90" align="right">
            <template #default="{ row }"
              ><span class="num">{{ row.ai.llm ? (row.ai.cost ?? 0).toFixed(2) : '—' }}</span></template
            >
          </el-table-column>
        </el-table>
      </el-card>

      <el-card shadow="never" header="收益曲线与回撤" class="mb">
        <NavChart :data="nav" :height="440" />
      </el-card>

      <el-card shadow="never">
        <el-tabs v-model="tab">
          <el-tab-pane label="决策轮次" name="rounds">
            <el-table :data="rounds" empty-text="还没有决策轮次">
              <el-table-column label="数据时间" width="140">
                <template #default="{ row }">
                  {{
                    row.snapshot_at
                      ? formatDateTime(row.snapshot_at, MARKET_TZ[track.market]).slice(0, 16)
                      : row.data_date
                  }}
                </template>
              </el-table-column>
              <el-table-column label="撮合" width="110">
                <template #default="{ row }">{{ row.snapshot_at ? '下一分钟起' : row.fill_date }}</template>
              </el-table-column>
              <el-table-column label="状态" width="90">
                <template #default="{ row }">
                  <el-tag size="small" :type="ROUND_STATUS[row.status]?.type ?? 'info'">{{
                    ROUND_STATUS[row.status]?.label ?? row.status
                  }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column label="触发" width="80">
                <template #default="{ row }">{{ TRIGGER_LABELS[row.trigger] ?? '自动' }}</template>
              </el-table-column>
              <el-table-column prop="note" label="说明" min-width="240" show-overflow-tooltip />
              <el-table-column label="快照哈希" width="120">
                <template #default="{ row }"
                  ><span class="mono">{{ row.snapshot_hash?.slice(0, 10) ?? '—' }}</span></template
                >
              </el-table-column>
              <el-table-column width="170">
                <template #default="{ row }">
                  <el-button v-if="row.snapshot_hash" link type="primary" @click="showSnapshot(row)"
                    >行情快照</el-button
                  >
                  <el-button
                    v-if="row.status === 'done'"
                    link
                    type="primary"
                    @click="router.push({ name: 'decisions', query: { track_id: trackId } })"
                    >决策</el-button
                  >
                </template>
              </el-table-column>
            </el-table>
          </el-tab-pane>
          <el-tab-pane label="事件日志" name="events">
            <el-table :data="events" empty-text="暂无事件">
              <el-table-column label="时间" width="170">
                <template #default="{ row }">{{ formatDateTime(row.created_at, 'Asia/Shanghai') }}</template>
              </el-table-column>
              <el-table-column label="级别" width="80">
                <template #default="{ row }">
                  <el-tag
                    size="small"
                    :type="row.level === 'error' ? 'danger' : row.level === 'warning' ? 'warning' : 'info'"
                  >
                    {{ { info: '信息', warning: '警告', error: '错误' }[row.level as 'info'] }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column prop="message" label="内容" />
            </el-table>
          </el-tab-pane>
          <el-tab-pane label="一致性评估" name="consistency" lazy>
            <ConsistencyPanel :track-id="trackId" />
          </el-tab-pane>
          <el-tab-pane label="规则参数（已冻结）" name="rules">
            <el-descriptions :column="3" border size="small">
              <el-descriptions-item v-for="(v, k) in track.rule_params" :key="k" :label="PARAM_LABELS[k] ?? String(k)">
                {{ typeof v === 'boolean' ? (v ? '是' : '否') : v }}
              </el-descriptions-item>
            </el-descriptions>
          </el-tab-pane>
        </el-tabs>
      </el-card>
    </template>

    <el-dialog v-model="snapshotVisible" :title="`行情快照 ${snapshot?.data_date ?? ''}`" width="90%" top="5vh">
      <p class="muted small">
        所有选手在这一轮看到的是同一份数据。哈希：<span class="mono">{{ snapshot?.snapshot_hash }}</span>
      </p>
      <el-table :data="snapshotRows.rows" max-height="65vh" size="small">
        <el-table-column v-for="(h, i) in snapshotRows.header" :key="h" :label="h" :min-width="i === 1 ? 110 : 80">
          <template #default="{ row }">{{ row[i] }}</template>
        </el-table-column>
      </el-table>
    </el-dialog>
  </div>
  <el-skeleton v-else :rows="8" animated />
</template>

<style scoped>
.header {
  display: flex;
  justify-content: space-between;
  align-items: flex-end;
  margin-bottom: 12px;
  gap: 12px;
  flex-wrap: wrap;
}

.title {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 4px 0;
}

.last-run {
  font-size: 13px;
  color: var(--el-text-color-secondary);
  display: flex;
  align-items: center;
  gap: 6px;
}

.actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.mb {
  margin-bottom: 16px;
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.enroll {
  display: flex;
  gap: 8px;
  margin-top: 12px;
}

.link {
  color: var(--el-color-primary);
  text-decoration: none;
}

.ml {
  margin-left: 6px;
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

.mono {
  font-family: ui-monospace, Consolas, monospace;
  font-size: 12px;
}
</style>
