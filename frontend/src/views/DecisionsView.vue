<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { errorMessage } from '@/api/http'
import { decisionsApi, tracksApi, type DecisionDetail, type DecisionSummary, type Track } from '@/api/arena'
import { formatDateTime } from '@/utils/format'
import { orderExtraText, orderTypeText, sideClass, sideText } from '@/utils/orders'

const route = useRoute()
const router = useRouter()

const tracks = ref<Track[]>([])
const items = ref<DecisionSummary[]>([])
const more = ref(false)
const loading = ref(false)
const filters = ref({
  track_id: route.query.track_id ? Number(route.query.track_id) : (undefined as number | undefined),
  account_id: route.query.account_id ? Number(route.query.account_id) : (undefined as number | undefined),
  status: undefined as string | undefined,
})

const detail = ref<DecisionDetail | null>(null)
const drawer = ref(false)
const detailTab = ref('summary')

const STATUS: Record<DecisionSummary['status'], { label: string; type: 'success' | 'info' | 'warning' | 'danger' }> = {
  ok: { label: '有效', type: 'success' },
  baseline: { label: '基线', type: 'info' },
  invalid: { label: '无法解析', type: 'warning' },
  timeout: { label: '超时', type: 'danger' },
  error: { label: '出错', type: 'danger' },
}
const ORDER_STATUS: Record<string, string> = {
  pending: '待撮合',
  filled: '已成交',
  partially_filled: '部分成交',
  rejected: '已拒绝',
  expired: '未成交',
  cancelled: '已撤销',
}

function timelineLabel(d: DecisionSummary): string {
  if (d.trigger === 'interval' || d.trigger === 'event') {
    return `${formatDateTime(d.created_at, 'Asia/Shanghai').slice(0, 16)}（北京时间）盘中${d.trigger === 'event' ? '异动' : '定时'}决策`
  }
  return `${d.data_date} 收盘后 → ${d.fill_date} 撮合`
}

const accounts = computed(() => tracks.value.find((t) => t.id === filters.value.track_id)?.accounts ?? [])

async function load(append = false) {
  loading.value = true
  try {
    const res = await decisionsApi.list({
      track_id: filters.value.track_id,
      account_id: filters.value.account_id,
      status: filters.value.status,
      limit: 30,
      offset: append ? items.value.length : 0,
    })
    items.value = append ? [...items.value, ...res.items] : res.items
    more.value = res.more
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    loading.value = false
  }
}

async function openDetail(id: number) {
  try {
    detail.value = await decisionsApi.get(id)
    detailTab.value = 'summary'
    drawer.value = true
  } catch (err) {
    ElMessage.error(errorMessage(err))
  }
}

watch(
  filters,
  (f) => {
    router.replace({ query: { track_id: f.track_id, account_id: f.account_id } })
    load()
  },
  { deep: true },
)

onMounted(async () => {
  try {
    const list = await tracksApi.list()
    // 列表接口不含选手明细，逐个获取（赛道数量很少）
    tracks.value = await Promise.all(list.filter((t) => t.status !== 'draft').map((t) => tracksApi.get(t.id)))
  } catch (err) {
    ElMessage.error(errorMessage(err))
  }
  await load()
  if (route.query.decision) openDetail(Number(route.query.decision))
})
</script>

<template>
  <div>
    <h2 class="sa-page-title">决策回放</h2>

    <div class="filters">
      <el-select
        v-model="filters.track_id"
        placeholder="全部赛道"
        clearable
        style="width: 220px"
        @change="filters.account_id = undefined"
      >
        <el-option v-for="t in tracks" :key="t.id" :label="t.name" :value="t.id" />
      </el-select>
      <el-select
        v-model="filters.account_id"
        placeholder="全部选手"
        clearable
        style="width: 200px"
        :disabled="!filters.track_id"
      >
        <el-option v-for="a in accounts" :key="a.id" :label="a.agent_name" :value="a.id" />
      </el-select>
      <el-select v-model="filters.status" placeholder="全部状态" clearable style="width: 140px">
        <el-option v-for="(s, k) in STATUS" :key="k" :label="s.label" :value="k" />
      </el-select>
    </div>

    <el-empty v-if="!loading && !items.length" description="还没有决策记录" />
    <el-timeline v-else v-loading="loading && !items.length">
      <el-timeline-item v-for="d in items" :key="d.id" :timestamp="timelineLabel(d)" placement="top">
        <el-card shadow="hover" class="decision" @click="openDetail(d.id)">
          <div class="d-head">
            <b>{{ d.agent_name }}</b>
            <el-tag size="small" :type="STATUS[d.status].type">{{ STATUS[d.status].label }}</el-tag>
            <span class="muted small">
              订单 {{ d.orders }}（成交 {{ d.filled }}，拒绝 {{ d.rejected }}）
              <template v-if="d.agent_kind === 'llm'">
                · {{ (d.latency_ms / 1000).toFixed(0) }} 秒 · {{ d.tokens_in + d.tokens_out }} tokens</template
              >
            </span>
          </div>
          <p class="analysis">{{ d.analysis || d.error || '（无分析内容）' }}</p>
        </el-card>
      </el-timeline-item>
    </el-timeline>
    <div v-if="more" class="more"><el-button :loading="loading" @click="load(true)">加载更多</el-button></div>

    <el-drawer v-model="drawer" size="62%" :title="detail ? `${detail.agent_name} · ${detail.data_date}` : ''">
      <template v-if="detail">
        <el-descriptions :column="3" size="small" border class="mb">
          <el-descriptions-item label="状态"
            ><el-tag size="small" :type="STATUS[detail.status].type">{{
              STATUS[detail.status].label
            }}</el-tag></el-descriptions-item
          >
          <el-descriptions-item label="模型">{{ detail.model ?? '—' }}</el-descriptions-item>
          <el-descriptions-item label="模板">
            {{ detail.template ?? '—'
            }}<template v-if="detail.strategy_version"> · 策略笔记 v{{ detail.strategy_version }}</template>
          </el-descriptions-item>
          <el-descriptions-item label="耗时"
            >{{ (detail.latency_ms / 1000).toFixed(1) }} 秒（{{ detail.attempts }} 次请求）</el-descriptions-item
          >
          <el-descriptions-item label="Token"
            >输入 {{ detail.tokens_in }} / 输出 {{ detail.tokens_out }}</el-descriptions-item
          >
          <el-descriptions-item label="费用">{{ detail.cost }}</el-descriptions-item>
          <el-descriptions-item label="快照哈希" :span="3"
            ><span class="mono">{{ detail.snapshot_hash }}</span></el-descriptions-item
          >
        </el-descriptions>

        <el-tabs v-model="detailTab">
          <el-tab-pane label="分析与订单" name="summary">
            <el-alert v-if="detail.error" type="error" :title="detail.error" :closable="false" class="mb" />
            <h4>分析</h4>
            <p class="pre">{{ detail.analysis || '（无）' }}</p>
            <template v-if="detail.notes.length">
              <h4>系统提示</h4>
              <ul class="notes">
                <li v-for="n in detail.notes" :key="n">{{ n }}</li>
              </ul>
            </template>
            <h4>订单</h4>
            <el-table :data="detail.order_items" empty-text="本轮不操作" size="small">
              <el-table-column label="方向" width="60">
                <template #default="{ row }"
                  ><span :class="sideClass(row.side)">{{ sideText(row.side) }}</span></template
                >
              </el-table-column>
              <el-table-column prop="symbol" label="标的" width="100" />
              <el-table-column label="类型" width="110">
                <template #default="{ row }">{{ orderTypeText(row) }}</template>
              </el-table-column>
              <el-table-column prop="qty" label="数量" width="80" align="right" />
              <el-table-column label="结果" width="150">
                <template #default="{ row }">
                  {{ ORDER_STATUS[row.status] }}
                  <span v-if="row.fills?.length" class="muted small">@ {{ row.fills[0].price }}</span>
                </template>
              </el-table-column>
              <el-table-column label="理由 / 说明" min-width="200">
                <template #default="{ row }">
                  {{ row.reason }}
                  <div v-if="orderExtraText(row)" class="muted small">{{ orderExtraText(row) }}</div>
                  <div v-if="row.note" class="muted small">{{ row.note }}</div>
                </template>
              </el-table-column>
            </el-table>
          </el-tab-pane>
          <el-tab-pane v-if="detail.tool_trace?.length" :label="`工具调用（${detail.tool_trace.length}）`" name="tools">
            <el-timeline>
              <el-timeline-item
                v-for="(t, i) in detail.tool_trace"
                :key="i"
                :type="t.tool === 'submit_decision' ? 'success' : 'primary'"
                :timestamp="`${i + 1}. ${t.tool}${t.ms ? ` · ${(t.ms / 1000).toFixed(1)} 秒` : ''}`"
                placement="top"
              >
                <div class="mono small">{{ JSON.stringify(t.args) }}</div>
                <pre class="block small-block">{{ t.result }}</pre>
              </el-timeline-item>
            </el-timeline>
          </el-tab-pane>
          <el-tab-pane v-if="detail.reasoning" label="推理过程" name="reasoning">
            <pre class="block">{{ detail.reasoning }}</pre>
          </el-tab-pane>
          <el-tab-pane v-if="detail.raw_response" label="原始输出" name="raw">
            <pre class="block">{{ detail.raw_response }}</pre>
          </el-tab-pane>
          <el-tab-pane v-if="detail.prompt_user" label="输入 Prompt" name="prompt">
            <h4>System</h4>
            <pre class="block">{{ detail.prompt_system }}</pre>
            <h4>User</h4>
            <pre class="block">{{ detail.prompt_user }}</pre>
          </el-tab-pane>
        </el-tabs>
        <p class="muted small">记录时间：{{ formatDateTime(detail.created_at, 'Asia/Shanghai') }}</p>
      </template>
    </el-drawer>
  </div>
</template>

<style scoped>
.filters {
  display: flex;
  gap: 8px;
  margin-bottom: 16px;
  flex-wrap: wrap;
}

.decision {
  cursor: pointer;
}

.d-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.analysis {
  margin: 8px 0 0;
  color: var(--el-text-color-regular);
  line-height: 1.6;
  display: -webkit-box;
  -webkit-line-clamp: 3;
  line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
  white-space: pre-wrap;
}

.more {
  text-align: center;
}

.mb {
  margin-bottom: 12px;
}

.pre {
  white-space: pre-wrap;
  line-height: 1.7;
}

.block {
  white-space: pre-wrap;
  word-break: break-all;
  background: var(--el-fill-color-light);
  padding: 12px;
  border-radius: 6px;
  font-size: 12px;
  line-height: 1.6;
  max-height: 60vh;
  overflow: auto;
}

.small-block {
  max-height: 200px;
  margin-top: 6px;
}

.notes {
  margin: 0;
  padding-left: 20px;
  color: var(--el-text-color-secondary);
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
