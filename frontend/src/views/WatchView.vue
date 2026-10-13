<script setup lang="ts">
/**
 * 自选股：AI 每天晚上盯重大事件（公告、新闻、异常波动），重要的推送微信；自选权重用于「自选倾斜」账户。
 * AI 只读资料、写说明，不预测涨跌、不给买卖建议。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { providersApi, type Provider } from '@/api/arena'
import { errorMessage } from '@/api/http'
import { stocksApi, type StockHit } from '@/api/stocks'
import {
  DIRECTION_TEXT,
  LEVEL_TEXT,
  LEVEL_TYPE,
  watchApi,
  type WatchEvent,
  type WatchItem,
  type WatchOverview,
} from '@/api/watch'
import StockLink from '@/components/StockLink.vue'
import { formatDateTime, formatPercent } from '@/utils/format'

const data = ref<WatchOverview | null>(null)
const events = ref<WatchEvent[]>([])
const providers = ref<Provider[]>([])
const loading = ref(false)
const adding = ref('')
const addWeight = ref(1)
const filterItem = ref<number | null>(null)
let poll: ReturnType<typeof setInterval> | undefined

async function load(silent = false) {
  if (!silent) loading.value = true
  try {
    ;[data.value, events.value] = await Promise.all([watchApi.overview(), watchApi.events({ limit: 200 })])
  } catch (err) {
    if (!silent) ElMessage.error(errorMessage(err))
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  await load()
  try {
    providers.value = await providersApi.list()
  } catch {
    /* 模型列表只用于设置 */
  }
  if (data.value?.task.running) startPoll()
})
onBeforeUnmount(() => clearInterval(poll))

function startPoll() {
  clearInterval(poll)
  poll = setInterval(async () => {
    const t = await watchApi.checkStatus().catch(() => null)
    if (t && !t.running) {
      clearInterval(poll)
      await load(true)
      if (t.error) ElMessage.error(`检查失败：${t.error}`)
      else if (t.result)
        ElMessage.success(
          `检查完成：AI 看了 ${t.result.checked} 只，${t.result.skipped} 只没有新资料，发现 ${t.result.events} 件事`,
        )
    }
  }, 3000)
}

async function suggest(q: string, cb: (items: (StockHit & { value: string })[]) => void) {
  if (!q.trim()) return cb([])
  try {
    cb((await stocksApi.search(q)).filter((h) => h.type !== 'index').map((h) => ({ ...h, value: h.symbol })))
  } catch {
    cb([])
  }
}

async function add(item?: Record<string, unknown>) {
  const sym = (item?.symbol as string | undefined) ?? adding.value.trim()
  if (!sym) return
  try {
    await watchApi.add(sym, addWeight.value)
    adding.value = ''
    ElMessage.success('已加入自选股，今晚 21:15 起每天检查')
    await load(true)
  } catch (err) {
    ElMessage.error(errorMessage(err))
  }
}

async function patch(row: WatchItem, body: Partial<Pick<WatchItem, 'weight' | 'note' | 'monitor'>>) {
  try {
    Object.assign(row, await watchApi.update(row.id, body))
  } catch (err) {
    ElMessage.error(errorMessage(err))
    await load(true)
  }
}

async function editNote(row: WatchItem) {
  try {
    const { value } = await ElMessageBox.prompt('写下关注它的原因，AI 检查时会参考', `备注：${row.name}`, {
      inputValue: row.note,
      inputType: 'textarea',
    })
    await patch(row, { note: value ?? '' })
  } catch {
    /* 取消 */
  }
}

async function remove(row: WatchItem) {
  try {
    await ElMessageBox.confirm(`从自选股中删除「${row.name}」？它的事件记录也会删除。`, '删除自选股', {
      type: 'warning',
    })
    await watchApi.remove(row.id)
    await load(true)
  } catch (err) {
    if (err !== 'cancel') ElMessage.error(errorMessage(err))
  }
}

async function check(ids?: number[]) {
  try {
    await watchApi.check(ids)
    data.value!.task = { running: true }
    ElMessage.info('已开始检查，AI 逐只阅读新资料，可能需要几分钟')
    startPoll()
  } catch (err) {
    ElMessage.error(errorMessage(err))
  }
}

async function saveSettings(body: Partial<WatchOverview['settings']>) {
  try {
    data.value!.settings = await watchApi.saveSettings(body)
  } catch (err) {
    ElMessage.error(errorMessage(err))
  }
}

async function markAllRead() {
  await watchApi.markRead()
  await load(true)
}

const shownEvents = computed(() =>
  events.value.filter((e) => filterItem.value == null || e.item_id === filterItem.value),
)

function lastCheck(row: WatchItem): string {
  const r = row.last_result
  if (!row.last_check_at) return '还没检查过'
  const when = formatDateTime(row.last_check_at).slice(5, 16)
  if (r.error) return `${when} AI 失败：${r.error}（下次重试）`
  if (!r.ai) return `${when} 没有新资料`
  return `${when} 看了 ${r.new ?? 0} 条新资料，发现 ${r.events ?? 0} 件事`
}

const cls = (v: number | null | undefined) => (v == null ? '' : v >= 0 ? 'sa-up' : 'sa-down')
</script>

<template>
  <div v-loading="loading && !data" class="watch">
    <div class="page-head">
      <h2>自选股</h2>
      <span class="muted small"
        >AI 每天晚上 21:15 检查新公告、新闻和异常波动，重要事件推送微信；只说明发生了什么，不预测涨跌</span
      >
    </div>

    <template v-if="data">
      <el-card shadow="never" class="card">
        <div class="toolbar">
          <el-autocomplete
            v-model="adding"
            :fetch-suggestions="suggest"
            :debounce="250"
            :trigger-on-focus="false"
            placeholder="搜索添加：代码或名称"
            prefix-icon="Search"
            style="width: 260px"
            @select="add"
          >
            <template #default="{ item }">
              {{ item.name }} <span class="muted small">{{ item.symbol }}</span>
            </template>
          </el-autocomplete>
          <span class="muted small">权重</span>
          <el-input-number
            v-model="addWeight"
            :min="data.limits.weight_min"
            :max="data.limits.weight_max"
            :step="0.5"
            size="small"
          />
          <span class="spacer" />
          <el-button :loading="data.task.running" :disabled="!data.items.length" @click="check()">
            {{ data.task.running ? '检查中…' : '立即检查全部' }}
          </el-button>
        </div>

        <el-alert type="info" :closable="false" class="tip">
          <b>权重</b>只作用于「自选倾斜」账户（与原组合并行对比，不影响现有账户）：0 = 策略选中也不买；1 = 默认； 大于 1
          = 策略选中时多配一些（最多 {{ data.limits.weight_max }} 倍，单只有上限）。<b>不会买入策略没有选中的股票。</b>
          不在自选股里的股票权重都是 1。
        </el-alert>

        <el-table :data="data.items" size="small" empty-text="还没有自选股，在上面搜索添加">
          <el-table-column label="股票" min-width="150">
            <template #default="{ row }">
              <StockLink :symbol="row.symbol" :name="row.name" />
              <el-badge v-if="row.unread" :value="row.unread" class="badge" />
              <div v-if="row.note" class="muted small note" :title="row.note">{{ row.note }}</div>
            </template>
          </el-table-column>
          <el-table-column label="现价" width="120" align="right">
            <template #default="{ row }">
              <div>{{ row.quote?.price ?? '-' }}</div>
              <div :class="['small', cls(row.quote?.change_pct)]">{{ formatPercent(row.quote?.change_pct, 2) }}</div>
            </template>
          </el-table-column>
          <el-table-column label="权重" width="130">
            <template #default="{ row }">
              <el-input-number
                :model-value="row.weight"
                :min="data.limits.weight_min"
                :max="data.limits.weight_max"
                :step="0.5"
                size="small"
                controls-position="right"
                style="width: 100px"
                @change="(v: number | undefined) => v != null && patch(row, { weight: v })"
              />
            </template>
          </el-table-column>
          <el-table-column label="在平台中" min-width="150">
            <template #default="{ row }">
              <el-tag v-for="p in row.picks" :key="p" size="small" class="tag">{{ p }} 选中</el-tag>
              <el-tag v-if="row.held?.length" size="small" type="success" class="tag">跟踪账户持有</el-tag>
              <span v-if="!row.picks?.length && !row.held?.length" class="muted small">策略未选中</span>
            </template>
          </el-table-column>
          <el-table-column label="最近事件" min-width="220">
            <template #default="{ row }">
              <template v-if="row.last_event">
                <el-tag :type="LEVEL_TYPE[row.last_event.level]" size="small">{{
                  LEVEL_TEXT[row.last_event.level]
                }}</el-tag>
                <span class="small"> {{ row.last_event.date.slice(5) }} {{ row.last_event.title }}</span>
              </template>
              <div class="muted small">{{ lastCheck(row) }}</div>
            </template>
          </el-table-column>
          <el-table-column label="监控" width="70" align="center">
            <template #default="{ row }">
              <el-switch :model-value="row.monitor" size="small" @change="(v: boolean) => patch(row, { monitor: v })" />
            </template>
          </el-table-column>
          <el-table-column label="操作" width="150">
            <template #default="{ row }">
              <el-button size="small" text :disabled="data.task.running" @click="check([row.id])">检查</el-button>
              <el-button size="small" text @click="editNote(row)">备注</el-button>
              <el-button size="small" text type="danger" @click="remove(row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>

        <div class="settings">
          <span class="muted small">每天自动检查</span>
          <el-switch
            :model-value="data.settings.enabled"
            size="small"
            @change="(v: boolean) => saveSettings({ enabled: v })"
          />
          <span class="muted small">模型</span>
          <el-select
            :model-value="data.settings.provider_id"
            size="small"
            style="width: 200px"
            placeholder="选择模型"
            @change="(v: number) => saveSettings({ provider_id: v })"
          >
            <el-option v-for="p in providers" :key="p.id" :label="p.name" :value="p.id" />
          </el-select>
          <span class="muted small">推送微信</span>
          <el-select
            :model-value="data.settings.push_level"
            size="small"
            style="width: 140px"
            @change="(v: number) => saveSettings({ push_level: v })"
          >
            <el-option :value="1" label="一般关注及以上" />
            <el-option :value="2" label="重要及以上" />
            <el-option :value="3" label="只推重大" />
          </el-select>
        </div>
      </el-card>

      <el-card shadow="never" class="card">
        <template #header>
          <div class="card-head">
            <b>事件</b>
            <span>
              <el-select v-model="filterItem" size="small" clearable placeholder="全部自选股" style="width: 160px">
                <el-option v-for="it in data.items" :key="it.id" :label="it.name" :value="it.id" />
              </el-select>
              <el-button size="small" text @click="markAllRead">全部标为已读</el-button>
            </span>
          </div>
        </template>
        <div v-for="e in shownEvents" :key="e.id" :class="['event', { unread: !e.read }]">
          <div class="event-head">
            <el-tag :type="LEVEL_TYPE[e.level]" size="small">{{ LEVEL_TEXT[e.level] }}</el-tag>
            <el-tag
              size="small"
              :type="e.direction === 'bad' ? 'success' : e.direction === 'good' ? 'danger' : 'info'"
              effect="plain"
              >{{ DIRECTION_TEXT[e.direction] }}</el-tag
            >
            <StockLink :symbol="e.symbol" :name="e.name" />
            <b>{{ e.title }}</b>
            <span class="muted small">{{ e.date }}{{ e.notified ? ' · 已推送' : '' }}</span>
          </div>
          <div class="event-body">{{ e.summary }}</div>
          <div v-if="e.impact" class="event-body muted">影响：{{ e.impact }}</div>
          <div v-for="(src, i) in e.sources" :key="i" class="small">
            <a :href="src.url" target="_blank" rel="noopener noreferrer"
              >{{ src.kind }} {{ src.date }} {{ src.title }}</a
            >
          </div>
        </div>
        <el-empty
          v-if="!shownEvents.length"
          :image-size="60"
          description="还没有事件：AI 只在发现值得提醒的事情时才记录"
        />
        <p class="muted small">以上由 AI 根据公告和新闻整理，可能有遗漏或理解偏差，请以原文为准；不构成投资建议。</p>
      </el-card>
    </template>
  </div>
</template>

<style scoped>
.page-head {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 12px;
  margin-bottom: 12px;
}

.page-head h2 {
  margin: 0;
  font-size: 20px;
}

.card {
  margin-bottom: 16px;
}

.card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.toolbar,
.settings {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.settings {
  margin-top: 12px;
}

.spacer {
  flex: 1;
}

.tip {
  margin: 10px 0;
}

.muted {
  color: var(--el-text-color-secondary);
}

.small {
  font-size: 12px;
}

.note {
  max-width: 220px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.badge {
  margin-left: 6px;
}

.tag {
  margin: 0 4px 2px 0;
}

.event {
  padding: 10px 0;
  border-bottom: 1px solid var(--el-border-color-extra-light);
}

.event.unread {
  border-left: 3px solid var(--el-color-warning);
  padding-left: 8px;
}

.event-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.event-body {
  margin: 4px 0;
  font-size: 13px;
  line-height: 1.7;
}

.event a {
  color: var(--el-color-primary);
  text-decoration: none;
}
</style>
