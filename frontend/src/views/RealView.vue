<script setup lang="ts">
/**
 * 实盘对照（PRD 3.9 G8）：录入 / 导入真实成交，与对应策略或组合的实时跟踪（模拟账户）比较，
 * 量化执行损耗（成交价差、延迟、漏做 / 多做的交易），检验「模拟中的效果」能否在实盘中复现。
 * 只保存用户提供的成交数据，不连接券商、不保存券商账号或密码。
 */
import StockLink from '@/components/StockLink.vue'
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { errorMessage } from '@/api/http'
import {
  portfolioApi,
  quantApi,
  realApi,
  type ImportResult,
  type Kind,
  type Portfolio,
  type RealAccount,
  type RealFill,
  type RealReport,
  type Strategy,
} from '@/api/quant'
import LabCurve from '@/components/lab/LabCurve.vue'
import { formatPercent } from '@/utils/format'

const list = ref<RealAccount[]>([])
const strategies = ref<Strategy[]>([])
const portfolios = ref<Portfolio[]>([])
const selectedId = ref<number | null>(null)
const tab = ref('report')
const current = computed(() => list.value.find((a) => a.id === selectedId.value) ?? null)

async function loadList() {
  try {
    ;[list.value, strategies.value, portfolios.value] = await Promise.all([
      realApi.list(),
      quantApi.list().then((r) => r.strategies.filter((s) => s.mine)),
      portfolioApi.list(),
    ])
    if (!current.value && list.value.length) select(list.value[0].id)
  } catch (err) {
    ElMessage.error(errorMessage(err))
  }
}
onMounted(loadList)

function select(id: number) {
  selectedId.value = id
  report.value = null
  fills.value = []
  loadReport()
  loadFills()
}

// ---------------------------------------------------------------- 账户
const today = () => new Date().toISOString().slice(0, 10)
const dialog = reactive({
  open: false,
  id: null as number | null,
  name: '',
  kind: 'portfolio' as Kind,
  ref_id: null as number | null,
  initial_cash: 1_000_000,
  start_date: today(),
  cash_flows: [] as { date: string; amount: number; note?: string }[],
  note: '',
})
const refOptions = computed(() =>
  dialog.kind === 'strategy'
    ? strategies.value.map((s) => ({
        id: s.id,
        name: s.name,
        tracking: !!s.follow_account_id,
      }))
    : portfolios.value.map((p) => ({
        id: p.id,
        name: p.name,
        tracking: !!p.follow_account_id,
      })),
)

function openCreate() {
  Object.assign(dialog, {
    open: true,
    id: null,
    name: '我的实盘账户',
    kind: 'portfolio',
    ref_id: null,
    initial_cash: 1_000_000,
    start_date: today(),
    cash_flows: [],
    note: '',
  })
}

function openEdit() {
  const a = current.value!
  Object.assign(dialog, {
    open: true,
    id: a.id,
    name: a.name,
    kind: a.kind,
    ref_id: a.ref_id,
    initial_cash: a.initial_cash,
    start_date: a.start_date,
    cash_flows: a.cash_flows.map((c) => ({ ...c })),
    note: a.note,
  })
}

async function saveAccount() {
  if (!dialog.ref_id) {
    ElMessage.warning('请选择对应的策略或组合')
    return
  }
  const body = {
    name: dialog.name,
    kind: dialog.kind,
    ref_id: dialog.ref_id,
    initial_cash: dialog.initial_cash,
    start_date: dialog.start_date,
    cash_flows: dialog.cash_flows.filter((c) => c.date && c.amount),
    note: dialog.note,
  }
  try {
    const acc = dialog.id ? await realApi.update(dialog.id, body) : await realApi.create(body)
    dialog.open = false
    await loadList()
    select(acc.id)
  } catch (err) {
    ElMessage.error(errorMessage(err))
  }
}

async function removeAccount() {
  try {
    await ElMessageBox.confirm('归档这个实盘账户？（成交记录会保留在数据库中）', '归档实盘账户', { type: 'warning' })
    await realApi.remove(current.value!.id)
    selectedId.value = null
    await loadList()
  } catch (err) {
    if (err !== 'cancel') ElMessage.error(errorMessage(err))
  }
}

// ---------------------------------------------------------------- 成交
const fills = ref<RealFill[]>([])
const fillsLoading = ref(false)

async function loadFills() {
  if (!selectedId.value) return
  fillsLoading.value = true
  try {
    fills.value = await realApi.fills(selectedId.value)
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    fillsLoading.value = false
  }
}

const fillDialog = reactive({
  open: false,
  trade_date: today(),
  symbol: '',
  side: 'buy' as 'buy' | 'sell',
  qty: 100,
  price: 0,
  fee: 5,
})

async function saveFill() {
  try {
    await realApi.addFill(selectedId.value!, {
      trade_date: fillDialog.trade_date,
      symbol: fillDialog.symbol,
      side: fillDialog.side,
      qty: fillDialog.qty,
      price: fillDialog.price,
      fee: fillDialog.fee,
    })
    ElMessage.success('已录入')
    fillDialog.symbol = ''
    fillDialog.price = 0
    await Promise.all([loadFills(), loadList()])
    loadReport()
  } catch (err) {
    ElMessage.error(errorMessage(err))
  }
}

async function removeFill(f: RealFill) {
  try {
    await ElMessageBox.confirm(`删除 ${f.trade_date} ${f.name || f.symbol} 的这笔成交？`, '删除成交', {
      type: 'warning',
    })
    await realApi.removeFill(selectedId.value!, f.id)
    await Promise.all([loadFills(), loadList()])
    loadReport()
  } catch (err) {
    if (err !== 'cancel') ElMessage.error(errorMessage(err))
  }
}

// 导入：浏览器读出原始字节（券商导出常为 GBK 编码），由后端识别编码与列名
const fileInput = ref<HTMLInputElement | null>(null)
const preview = reactive({
  open: false,
  content: '',
  fileName: '',
  result: null as ImportResult | null,
  busy: false,
})

function toBase64(buf: ArrayBuffer): string {
  const bytes = new Uint8Array(buf)
  let bin = ''
  for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000))
  return btoa(bin)
}

async function onFile(e: Event) {
  const file = (e.target as HTMLInputElement).files?.[0]
  if (!file) return
  ;(e.target as HTMLInputElement).value = ''
  try {
    preview.content = toBase64(await file.arrayBuffer())
    preview.fileName = file.name
    preview.result = await realApi.importFills(selectedId.value!, preview.content, false)
    preview.open = true
  } catch (err) {
    ElMessage.error(errorMessage(err))
  }
}

async function confirmImport() {
  preview.busy = true
  try {
    const r = await realApi.importFills(selectedId.value!, preview.content, true)
    ElMessage.success(`已导入 ${r.added} 笔${r.duplicates ? `，跳过重复 ${r.duplicates} 笔` : ''}`)
    preview.open = false
    await Promise.all([loadFills(), loadList()])
    loadReport()
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    preview.busy = false
  }
}

// ---------------------------------------------------------------- 对比报告
const report = ref<RealReport | null>(null)
const reportLoading = ref(false)

async function loadReport() {
  if (!selectedId.value) return
  reportLoading.value = true
  try {
    report.value = await realApi.report(selectedId.value)
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    reportLoading.value = false
  }
}

const STATUS_TYPE: Record<string, 'success' | 'warning' | 'danger' | 'info'> = {
  一致: 'success',
  有偏差: 'warning',
  偏差较大: 'danger',
}
const curves = computed(() => {
  const c = report.value?.curve
  if (!c || c.dates.length < 2) return null
  return { dates: c.dates, nav: c.real, bench: c.sim }
})
const pct = (v: number | null | undefined, d = 2) => formatPercent(v ?? null, d)
const money = (v: number | null | undefined) =>
  v === null || v === undefined ? '-' : v.toLocaleString('zh-CN', { maximumFractionDigits: 2 })
const sideText = (s: string) => (s === 'buy' ? '买入' : '卖出')
const COLUMN_NAMES: Record<string, string> = {
  date: '日期',
  symbol: '代码',
  name: '名称',
  side: '买卖方向',
  qty: '数量',
  price: '价格',
  amount: '成交金额',
  fee_total: '费用合计',
  fee_parts: '费用（相加）',
}
</script>

<template>
  <div class="real">
    <div class="head">
      <div>
        <h2>实盘对照</h2>
        <p class="muted">
          录入或导入你在券商的真实成交，与对应策略 /
          组合的实时跟踪（模拟账户）逐日比较：收益差、跟踪误差、每笔成交的价差与延迟、漏做和多做的交易、持仓差异。
          这里只保存你提供的成交数据，不连接券商，也不需要任何账号密码。
        </p>
      </div>
      <el-button type="primary" @click="openCreate">新建实盘账户</el-button>
    </div>

    <el-empty v-if="!list.length" description="还没有实盘账户。开始实盘后，新建一个账户并选择你跟随的策略或组合。" />
    <el-row v-else :gutter="16">
      <el-col :span="5">
        <el-menu :default-active="String(selectedId)" class="list">
          <el-menu-item v-for="a in list" :key="a.id" :index="String(a.id)" @click="select(a.id)">
            <div class="menu-item">
              <span>{{ a.name }}</span>
              <span class="muted">{{ a.ref_name }} · {{ a.fills }} 笔</span>
            </div>
          </el-menu-item>
        </el-menu>
      </el-col>
      <el-col :span="19">
        <template v-if="current">
          <div class="title">
            <div>
              <h3>{{ current.name }}</h3>
              <span class="muted">
                对照{{ current.kind === 'strategy' ? '策略' : '组合' }}「{{ current.ref_name }}」· 初始资金
                {{ money(current.initial_cash) }} 元 · {{ current.start_date }} 起
                <template v-if="!current.sim_account_id"
                  >· <b class="warn">对应的策略 / 组合尚未开始实时跟踪</b></template
                >
              </span>
            </div>
            <div>
              <el-button size="small" @click="openEdit">编辑</el-button>
              <el-button size="small" type="danger" text @click="removeAccount">归档</el-button>
            </div>
          </div>
          <el-tabs v-model="tab">
            <el-tab-pane label="对比报告" name="report">
              <div v-loading="reportLoading">
                <template v-if="report">
                  <div class="row">
                    <el-tag :type="STATUS_TYPE[report.status] ?? 'info'">{{ report.status }}</el-tag>
                    <span>{{ report.message }}</span>
                    <el-button size="small" text type="primary" @click="loadReport">刷新</el-button>
                  </div>
                  <el-alert
                    v-for="w in report.warnings ?? []"
                    :key="w"
                    :title="w"
                    type="warning"
                    :closable="false"
                    show-icon
                    class="warning"
                  />
                  <div class="stats">
                    <div>
                      <span class="label">实盘收益</span><span class="value">{{ pct(report.real_return) }}</span>
                    </div>
                    <div>
                      <span class="label">模拟收益</span><span class="value">{{ pct(report.sim_return) }}</span>
                    </div>
                    <div>
                      <span class="label">收益差（年化）</span>
                      <span class="value">{{ pct(report.gap) }}（{{ pct(report.gap_annual) }}）</span>
                    </div>
                    <div>
                      <span class="label">跟踪误差（年化）</span
                      ><span class="value">{{ formatPercent(report.tracking_error ?? null, 2, false) }}</span>
                    </div>
                    <div>
                      <span class="label">日收益相关</span
                      ><span class="value">{{ report.correlation?.toFixed(3) ?? '-' }}</span>
                    </div>
                    <div>
                      <span class="label">对比区间</span
                      ><span class="value small"
                        >{{ report.compare_start ?? '-' }} ～ {{ report.end ?? '-' }}（{{ report.days ?? 0 }} 天）</span
                      >
                    </div>
                    <div>
                      <span class="label">当前净值</span><span class="value">{{ money(report.nav_last) }}</span>
                    </div>
                  </div>
                  <LabCurve v-if="curves" :curves="curves" :height="240" name="实盘" />
                  <p v-if="curves" class="muted">
                    灰线为模拟账户（实时跟踪）。实盘收益已扣除资金转入转出的影响（时间加权）。
                  </p>

                  <template v-if="report.execution">
                    <h4>执行对照</h4>
                    <div class="stats">
                      <div>
                        <span class="label">配对成交（实盘 / 模拟）</span>
                        <span class="value"
                          >{{ report.execution.matched }}（{{ report.execution.real_trades }} /
                          {{ report.execution.sim_trades }}）</span
                        >
                      </div>
                      <div>
                        <span class="label">平均成交价差</span>
                        <span class="value">{{
                          report.execution.avg_dev_bps === null ? '-' : `${report.execution.avg_dev_bps} bp`
                        }}</span>
                        <span class="muted">正数表示实盘更差</span>
                      </div>
                      <div>
                        <span class="label">价差损耗</span
                        ><span class="value">{{ money(report.execution.price_cost) }} 元</span>
                      </div>
                      <div>
                        <span class="label">平均延迟</span
                        ><span class="value">{{ report.execution.avg_lag ?? '-' }} 个交易日</span>
                      </div>
                      <div>
                        <span class="label">费用（实盘 / 模拟）</span>
                        <span class="value"
                          >{{ money(report.fees_real) }} / {{ money(report.execution.fees_sim) }}</span
                        >
                      </div>
                      <div>
                        <span class="label">漏做 / 多做</span>
                        <span class="value"
                          >{{ report.execution.missed_count }} / {{ report.execution.extra_count }} 笔</span
                        >
                      </div>
                    </div>
                    <el-tabs type="border-card" class="inner">
                      <el-tab-pane :label="`配对成交（按损耗排序）`">
                        <el-table :data="report.execution.pairs" size="small" max-height="360">
                          <el-table-column label="标的" min-width="140">
                            <template #default="{ row }"><StockLink :symbol="row.symbol" :name="row.name" /></template>
                          </el-table-column>
                          <el-table-column label="方向" width="60"
                            ><template #default="{ row }">{{ sideText(row.side) }}</template></el-table-column
                          >
                          <el-table-column label="实盘日期" prop="date" width="100" />
                          <el-table-column label="模拟日期" prop="sim_date" width="100" />
                          <el-table-column label="数量（实盘 / 模拟）" width="140" align="right">
                            <template #default="{ row }">{{ row.real_qty }} / {{ row.sim_qty }}</template>
                          </el-table-column>
                          <el-table-column label="价格（实盘 / 模拟）" width="150" align="right">
                            <template #default="{ row }"
                              >{{ row.real_price.toFixed(3) }} / {{ row.sim_price.toFixed(3) }}</template
                            >
                          </el-table-column>
                          <el-table-column label="价差" width="80" align="right"
                            ><template #default="{ row }">{{ row.dev_bps }} bp</template></el-table-column
                          >
                          <el-table-column label="损耗（元）" width="100" align="right"
                            ><template #default="{ row }">{{ money(row.cost) }}</template></el-table-column
                          >
                        </el-table>
                      </el-tab-pane>
                      <el-tab-pane :label="`漏做（${report.execution.missed_count}）`">
                        <p class="muted">模拟账户成交了、但实盘在之后 5 个交易日内没有对应成交的交易。</p>
                        <el-table :data="report.execution.missed" size="small" max-height="360">
                          <el-table-column label="日期" prop="date" width="100" />
                          <el-table-column label="标的" min-width="140">
                            <template #default="{ row }"><StockLink :symbol="row.symbol" :name="row.name" /></template>
                          </el-table-column>
                          <el-table-column label="方向" width="60"
                            ><template #default="{ row }">{{ sideText(row.side) }}</template></el-table-column
                          >
                          <el-table-column label="数量" prop="qty" width="90" align="right" />
                          <el-table-column label="金额" width="110" align="right"
                            ><template #default="{ row }">{{ money(row.amount) }}</template></el-table-column
                          >
                        </el-table>
                      </el-tab-pane>
                      <el-tab-pane :label="`多做（${report.execution.extra_count}）`">
                        <p class="muted">实盘有、但模拟账户没有对应成交的交易。</p>
                        <el-table :data="report.execution.extra" size="small" max-height="360">
                          <el-table-column label="日期" prop="date" width="100" />
                          <el-table-column label="标的" min-width="140">
                            <template #default="{ row }"><StockLink :symbol="row.symbol" :name="row.name" /></template>
                          </el-table-column>
                          <el-table-column label="方向" width="60"
                            ><template #default="{ row }">{{ sideText(row.side) }}</template></el-table-column
                          >
                          <el-table-column label="数量" prop="qty" width="90" align="right" />
                          <el-table-column label="金额" width="110" align="right"
                            ><template #default="{ row }">{{ money(row.amount) }}</template></el-table-column
                          >
                        </el-table>
                      </el-tab-pane>
                    </el-tabs>
                  </template>

                  <template v-if="report.holdings">
                    <h4>
                      持仓对照（重合度
                      {{ formatPercent(report.holdings.overlap, 0, false) }}，股票仓位 实盘
                      {{ formatPercent(report.holdings.real_exposure, 0, false) }}
                      / 模拟
                      {{ formatPercent(report.holdings.sim_exposure, 0, false) }}）
                    </h4>
                    <el-table :data="report.holdings.rows" size="small" max-height="360" style="max-width: 640px">
                      <el-table-column label="标的（按权重差排序）" min-width="160">
                        <template #default="{ row }"><StockLink :symbol="row.symbol" :name="row.name" /></template>
                      </el-table-column>
                      <el-table-column label="实盘" width="80" align="right"
                        ><template #default="{ row }">{{
                          formatPercent(row.real, 1, false)
                        }}</template></el-table-column
                      >
                      <el-table-column label="模拟" width="80" align="right"
                        ><template #default="{ row }">{{ formatPercent(row.sim, 1, false) }}</template></el-table-column
                      >
                    </el-table>
                  </template>
                </template>
              </div>
            </el-tab-pane>

            <el-tab-pane :label="`成交记录（${current.fills}）`" name="fills">
              <div class="toolbar">
                <el-button size="small" type="primary" @click="fillDialog.open = true">录入成交</el-button>
                <el-button size="small" @click="fileInput?.click()">导入券商成交明细（CSV）</el-button>
                <input ref="fileInput" type="file" accept=".csv,.txt,.tsv" hidden @change="onFile" />
                <span class="muted">
                  支持券商软件导出的「当日 / 历史成交」：自动识别 UTF-8 / GBK
                  编码与常见列名（成交日期、证券代码、买卖标志、成交数量、成交价格、佣金、印花税、过户费等），重复记录自动跳过。
                </span>
              </div>
              <el-table v-loading="fillsLoading" :data="fills" size="small" max-height="560">
                <el-table-column label="日期" prop="trade_date" width="100" />
                <el-table-column label="标的" min-width="150">
                  <template #default="{ row }"><StockLink :symbol="row.symbol" :name="row.name" /></template>
                </el-table-column>
                <el-table-column label="方向" width="60"
                  ><template #default="{ row }">{{ sideText(row.side) }}</template></el-table-column
                >
                <el-table-column label="数量" prop="qty" width="90" align="right" />
                <el-table-column label="价格" width="90" align="right"
                  ><template #default="{ row }">{{ row.price.toFixed(3) }}</template></el-table-column
                >
                <el-table-column label="金额" width="110" align="right"
                  ><template #default="{ row }">{{ money(row.amount) }}</template></el-table-column
                >
                <el-table-column label="费用" width="80" align="right"
                  ><template #default="{ row }">{{ money(row.fee) }}</template></el-table-column
                >
                <el-table-column label="来源" width="70"
                  ><template #default="{ row }">{{
                    row.source === 'import' ? '导入' : '手工'
                  }}</template></el-table-column
                >
                <el-table-column width="70">
                  <template #default="{ row }"
                    ><el-button size="small" text type="danger" @click="removeFill(row)">删除</el-button></template
                  >
                </el-table-column>
              </el-table>
            </el-tab-pane>
          </el-tabs>
        </template>
      </el-col>
    </el-row>

    <el-dialog v-model="dialog.open" :title="dialog.id ? '编辑实盘账户' : '新建实盘账户'" width="600px">
      <el-form label-width="90px">
        <el-form-item label="名称"><el-input v-model="dialog.name" /></el-form-item>
        <el-form-item label="对照">
          <el-radio-group v-model="dialog.kind" @change="dialog.ref_id = null">
            <el-radio-button value="portfolio">多风格组合</el-radio-button>
            <el-radio-button value="strategy">单个策略</el-radio-button>
          </el-radio-group>
          <el-select v-model="dialog.ref_id" placeholder="选择你实盘跟随的对象" class="ref-select">
            <el-option
              v-for="o in refOptions"
              :key="o.id"
              :value="o.id"
              :label="o.tracking ? o.name : `${o.name}（未开始跟踪）`"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="初始资金">
          <el-input-number
            v-model="dialog.initial_cash"
            :min="1000"
            :step="100000"
            :controls="false"
            style="width: 180px"
          />
          <span class="muted">&nbsp;元（开始日期时账户里的现金）</span>
        </el-form-item>
        <el-form-item label="开始日期">
          <el-date-picker v-model="dialog.start_date" type="date" value-format="YYYY-MM-DD" />
        </el-form-item>
        <el-form-item label="资金变动">
          <div class="flows">
            <div v-for="(c, k) in dialog.cash_flows" :key="k" class="flow">
              <el-date-picker v-model="c.date" type="date" value-format="YYYY-MM-DD" style="width: 150px" />
              <el-input-number
                v-model="c.amount"
                :step="10000"
                :controls="false"
                placeholder="转入为正，转出为负"
                style="width: 160px"
              />
              <el-button size="small" text type="danger" @click="dialog.cash_flows.splice(k, 1)">删除</el-button>
            </div>
            <el-button size="small" @click="dialog.cash_flows.push({ date: today(), amount: 0 })"
              >添加转入 / 转出</el-button
            >
          </div>
        </el-form-item>
        <el-form-item label="备注"><el-input v-model="dialog.note" type="textarea" :rows="2" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialog.open = false">取消</el-button>
        <el-button type="primary" @click="saveAccount">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="fillDialog.open" title="录入成交" width="480px">
      <el-form label-width="70px">
        <el-form-item label="日期"
          ><el-date-picker v-model="fillDialog.trade_date" type="date" value-format="YYYY-MM-DD"
        /></el-form-item>
        <el-form-item label="代码"
          ><el-input v-model="fillDialog.symbol" placeholder="如 600519 或 510300.SH"
        /></el-form-item>
        <el-form-item label="方向">
          <el-radio-group v-model="fillDialog.side">
            <el-radio-button value="buy">买入</el-radio-button>
            <el-radio-button value="sell">卖出</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="数量"><el-input-number v-model="fillDialog.qty" :min="1" :step="100" /></el-form-item>
        <el-form-item label="价格"
          ><el-input-number v-model="fillDialog.price" :min="0" :precision="3" :step="0.01"
        /></el-form-item>
        <el-form-item label="费用"
          ><el-input-number v-model="fillDialog.fee" :min="0" :precision="2" /><span class="muted"
            >&nbsp;佣金 + 印花税 + 过户费</span
          ></el-form-item
        >
      </el-form>
      <template #footer>
        <el-button @click="fillDialog.open = false">关闭</el-button>
        <el-button type="primary" :disabled="!fillDialog.symbol || !fillDialog.price" @click="saveFill"
          >保存并继续录入</el-button
        >
      </template>
    </el-dialog>

    <el-dialog v-model="preview.open" :title="`导入预览：${preview.fileName}`" width="860px">
      <template v-if="preview.result">
        <p class="muted">
          识别到的列：
          <span v-for="(v, k) in preview.result.columns" :key="k"
            >{{ COLUMN_NAMES[k] ?? k }} ← {{ Array.isArray(v) ? v.join(' + ') : v }}；</span
          >
        </p>
        <p>
          共识别 {{ preview.result.rows.length }} 笔成交<span v-if="preview.result.skipped.length"
            >，跳过 {{ preview.result.skipped.length }} 行</span
          >。
        </p>
        <el-table :data="preview.result.rows" size="small" max-height="320">
          <el-table-column label="日期" prop="trade_date" width="100" />
          <el-table-column label="标的" min-width="140">
            <template #default="{ row }"><StockLink :symbol="row.symbol" :name="row.name" /></template>
          </el-table-column>
          <el-table-column label="方向" width="60"
            ><template #default="{ row }">{{ sideText(row.side) }}</template></el-table-column
          >
          <el-table-column label="数量" prop="qty" width="90" align="right" />
          <el-table-column label="价格" prop="price" width="90" align="right" />
          <el-table-column label="费用" prop="fee" width="80" align="right" />
        </el-table>
        <el-collapse v-if="preview.result.skipped.length" class="skipped">
          <el-collapse-item :title="`跳过的行（${preview.result.skipped.length}）`">
            <div v-for="s in preview.result.skipped" :key="s.line" class="muted">
              第 {{ s.line }} 行：{{ s.reason }}
            </div>
          </el-collapse-item>
        </el-collapse>
      </template>
      <template #footer>
        <el-button @click="preview.open = false">取消</el-button>
        <el-button
          type="primary"
          :loading="preview.busy"
          :disabled="!preview.result?.rows.length"
          @click="confirmImport"
          >确认导入</el-button
        >
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.head,
.title {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 16px;
}

h2,
h3 {
  margin: 0 0 4px;
}

h4 {
  margin: 16px 0 8px;
}

.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 1.8;
}

.warn {
  color: var(--el-color-warning);
}

.list {
  border-right: none;
}

.menu-item {
  display: flex;
  flex-direction: column;
  line-height: 1.4;
}

.row {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 8px;
}

.warning {
  margin-bottom: 8px;
}

.stats {
  display: flex;
  flex-wrap: wrap;
  gap: 12px 28px;
  margin: 8px 0 12px;
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

.value.small {
  font-size: 13px;
  font-weight: 400;
}

.inner {
  margin-top: 8px;
}

.toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
  flex-wrap: wrap;
}

.ref-select {
  margin-left: 12px;
  width: 240px;
}

.flows {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.flow {
  display: flex;
  gap: 8px;
  align-items: center;
}

.skipped {
  margin-top: 8px;
}
</style>
