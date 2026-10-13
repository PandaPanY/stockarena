<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { errorMessage } from '@/api/http'
import {
  accountsApi,
  type ReflectionDetail,
  type ReflectionSummary,
  type StrategyInfo,
  type StrategyVersion,
} from '@/api/arena'
import { diffLines } from '@/utils/diff'
import { formatDateTime } from '@/utils/format'

const props = defineProps<{ accountId: number }>()

const info = ref<StrategyInfo | null>(null)
const loading = ref(false)
let timer: number | undefined

const SOURCE_LABELS: Record<StrategyVersion['source'], string> = {
  initial: '初始策略',
  reflection: 'AI 复盘',
  manual: '手动编辑',
  rollback: '回滚',
}
const STATUS: Record<ReflectionSummary['status'], { label: string; type: 'success' | 'warning' | 'danger' }> = {
  ok: { label: '完成', type: 'success' },
  invalid: { label: '无法解析', type: 'warning' },
  timeout: { label: '超时', type: 'danger' },
  error: { label: '出错', type: 'danger' },
}

const current = computed(() => info.value?.versions[0] ?? null)
const byVersion = computed(() => new Map((info.value?.versions ?? []).map((v) => [v.version, v])))

async function load() {
  loading.value = !info.value
  try {
    info.value = await accountsApi.strategy(props.accountId)
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    loading.value = false
  }
  // 复盘进行中时轮询，完成后停止
  window.clearTimeout(timer)
  if (info.value?.reflecting) timer = window.setTimeout(load, 5000)
}

async function reflectNow() {
  try {
    const r = await accountsApi.reflect(props.accountId)
    ElMessage.success(r.message)
    await load()
  } catch (err) {
    ElMessage.error(errorMessage(err))
  }
}

// ---------- 编辑与回滚
const editVisible = ref(false)
const editText = ref('')
const saving = ref(false)

function openEdit() {
  editText.value = current.value?.content ?? ''
  editVisible.value = true
}

async function saveEdit() {
  saving.value = true
  try {
    await accountsApi.editStrategy(props.accountId, editText.value)
    ElMessage.success('已保存为新版本，下一轮决策开始使用')
    editVisible.value = false
    await load()
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    saving.value = false
  }
}

async function rollback(v: StrategyVersion) {
  await ElMessageBox.confirm(`将第 ${v.version} 版的内容复制为新版本并立即生效。确定？`, '回滚策略笔记', {
    type: 'warning',
  })
  try {
    await accountsApi.rollbackStrategy(props.accountId, v.version)
    ElMessage.success('已回滚')
    await load()
  } catch (err) {
    ElMessage.error(errorMessage(err))
  }
}

// ---------- 版本对比
const diffVersion = ref<StrategyVersion | null>(null)
const diff = computed(() => {
  const v = diffVersion.value
  if (!v) return []
  return diffLines(byVersion.value.get(v.version - 1)?.content ?? '', v.content)
})

// ---------- 复盘详情
const detail = ref<ReflectionDetail | null>(null)
const detailTab = ref('prompt')

async function openReflection(id: number) {
  try {
    detail.value = await accountsApi.reflection(id)
    detailTab.value = 'prompt'
  } catch (err) {
    ElMessage.error(errorMessage(err))
  }
}

function pctClass(text: string | undefined) {
  if (!text || text === '-') return ''
  return text.startsWith('-') ? 'sa-down' : 'sa-up'
}

watch(() => props.accountId, load)
onMounted(load)
onBeforeUnmount(() => window.clearTimeout(timer))
</script>

<template>
  <div v-loading="loading" class="panel">
    <template v-if="info">
      <div class="toolbar">
        <div>
          <el-tag v-if="info.reflect_enabled" type="success">自我进化：每 {{ info.reflect_every }} 个交易日复盘</el-tag>
          <el-tag v-else type="info">未开启自动复盘</el-tag>
          <span class="muted small gap">
            {{
              info.reflect_enabled
                ? '收盘结算后自动复盘，新策略从当天决策开始生效。'
                : '可在「设置 → AI 选手」中开启；也可以手动复盘。'
            }}
          </span>
        </div>
        <div>
          <el-button :loading="info.reflecting" @click="reflectNow">{{
            info.reflecting ? '复盘中…' : '立即复盘'
          }}</el-button>
          <el-button @click="openEdit">编辑策略</el-button>
        </div>
      </div>
      <el-alert
        v-if="info.last_message && !info.reflecting"
        :title="info.last_message"
        type="info"
        :closable="false"
        class="mb"
      />

      <!-- 当前策略 -->
      <h4>当前策略笔记</h4>
      <el-empty
        v-if="!current"
        :image-size="60"
        description="还没有策略笔记。开启自我进化后，AI 会在第一次复盘时写出第一版。"
      />
      <div v-else class="current">
        <div class="muted small mb-s">
          第 {{ current.version }} 版 · {{ SOURCE_LABELS[current.source] }} ·
          {{ current.data_date ? `${current.data_date} 收盘后` : formatDateTime(current.created_at, 'Asia/Shanghai') }}
          <template v-if="current.changes"> · {{ current.changes }}</template>
        </div>
        <pre class="block">{{ current.content }}</pre>
      </div>

      <!-- 复盘记录 -->
      <h4>复盘记录（{{ info.reflections.length }}）</h4>
      <el-empty v-if="!info.reflections.length" :image-size="60" description="还没有复盘记录" />
      <el-timeline v-else>
        <el-timeline-item
          v-for="r in info.reflections"
          :key="r.id"
          placement="top"
          :timestamp="`${r.data_date} 收盘后${r.trigger === 'manual' ? '（手动）' : ''}`"
        >
          <el-card shadow="never">
            <div class="r-head">
              <el-tag size="small" :type="STATUS[r.status].type">{{ STATUS[r.status].label }}</el-tag>
              <span v-if="r.status === 'ok'" class="small">
                <template v-if="r.new_version"
                  >策略 v{{ r.base_version ?? 0 }} → <b>v{{ r.new_version }}</b></template
                >
                <template v-else>策略未修改（v{{ r.base_version ?? 0 }}）</template>
              </span>
              <span class="muted small">
                {{ r.period_start }} ~ {{ r.data_date }}，{{ r.stats.days ?? '-' }} 个交易日 · 区间收益
                <span :class="pctClass(r.stats.period_return)">{{ r.stats.period_return }}</span> （基准
                {{ r.stats.bench_period_return }}，超额
                <span :class="pctClass(r.stats.period_excess)">{{ r.stats.period_excess }}</span
                >）· 成交 {{ r.stats.trades ?? 0 }} 笔
              </span>
              <el-button link type="primary" class="right" @click="openReflection(r.id)">详情</el-button>
            </div>
            <p v-if="r.error" class="err small">{{ r.error }}</p>
            <template v-else>
              <p class="pre">{{ r.review || '（无复盘结论）' }}</p>
              <p v-if="r.changes" class="small"><b>修改：</b>{{ r.changes }}</p>
            </template>
            <div class="muted small">
              {{ (r.latency_ms / 1000).toFixed(0) }} 秒 · {{ r.tokens_in + r.tokens_out }} tokens
            </div>
          </el-card>
        </el-timeline-item>
      </el-timeline>

      <!-- 版本历史 -->
      <h4>版本历史（{{ info.versions.length }}）</h4>
      <el-table :data="info.versions" size="small" empty-text="暂无版本">
        <el-table-column label="版本" width="70"
          ><template #default="{ row }">v{{ row.version }}</template></el-table-column
        >
        <el-table-column label="来源" width="90"
          ><template #default="{ row }">{{
            SOURCE_LABELS[row.source as StrategyVersion['source']]
          }}</template></el-table-column
        >
        <el-table-column label="生效时间" width="150">
          <template #default="{ row }">{{
            row.data_date ? `${row.data_date} 收盘后` : formatDateTime(row.created_at, 'Asia/Shanghai').slice(0, 16)
          }}</template>
        </el-table-column>
        <el-table-column prop="changes" label="修改说明" min-width="240" show-overflow-tooltip />
        <el-table-column width="150" align="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="diffVersion = row">{{
              row.version > 1 ? '对比上一版' : '查看'
            }}</el-button>
            <el-button v-if="row.version !== current?.version" link @click="rollback(row)">回滚</el-button>
          </template>
        </el-table-column>
      </el-table>
    </template>

    <!-- 编辑 -->
    <el-dialog v-model="editVisible" title="编辑策略笔记" width="680px" destroy-on-close>
      <p class="muted small">
        保存后生成新版本（来源记为「手动编辑」），从下一轮决策开始生效。比赛中手动修改会影响公平性，请谨慎。
      </p>
      <el-input v-model="editText" type="textarea" :rows="14" :maxlength="info?.max_chars ?? 2000" show-word-limit />
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" :disabled="!editText.trim()" @click="saveEdit">保存</el-button>
      </template>
    </el-dialog>

    <!-- 版本对比 -->
    <el-dialog
      :model-value="!!diffVersion"
      :title="diffVersion ? `策略笔记 v${diffVersion.version}` : ''"
      width="720px"
      @close="diffVersion = null"
    >
      <template v-if="diffVersion">
        <p class="muted small">
          {{ SOURCE_LABELS[diffVersion.source]
          }}<template v-if="diffVersion.changes"> · {{ diffVersion.changes }}</template>
          <template v-if="diffVersion.version > 1">
            · 与 v{{ diffVersion.version - 1 }} 对比：<span class="add">新增</span>
            <span class="del">删除</span></template
          >
        </p>
        <div class="block diff">
          <div v-for="(l, i) in diff" :key="i" :class="l.type">
            {{ l.type === 'add' ? '+ ' : l.type === 'del' ? '− ' : '  ' }}{{ l.text }}
          </div>
        </div>
      </template>
    </el-dialog>

    <!-- 复盘详情 -->
    <el-drawer
      :model-value="!!detail"
      size="62%"
      :title="detail ? `复盘 · ${detail.data_date}` : ''"
      @close="detail = null"
    >
      <template v-if="detail">
        <p class="muted small">
          模型 {{ detail.model ?? '—' }} · 复盘模板 v{{ detail.template_version ?? '-' }} · {{ detail.attempts }} 次请求
          · {{ (detail.latency_ms / 1000).toFixed(1) }} 秒 · 输入 {{ detail.tokens_in }} / 输出
          {{ detail.tokens_out }} tokens
        </p>
        <el-tabs v-model="detailTab">
          <el-tab-pane label="输入 Prompt" name="prompt">
            <h4>System</h4>
            <pre class="block">{{ detail.prompt_system }}</pre>
            <h4>User</h4>
            <pre class="block">{{ detail.prompt_user }}</pre>
          </el-tab-pane>
          <el-tab-pane v-if="detail.reasoning" label="推理过程" name="reasoning">
            <pre class="block">{{ detail.reasoning }}</pre>
          </el-tab-pane>
          <el-tab-pane v-if="detail.raw_response" label="原始输出" name="raw">
            <pre class="block">{{ detail.raw_response }}</pre>
          </el-tab-pane>
        </el-tabs>
      </template>
    </el-drawer>
  </div>
</template>

<style scoped>
.panel {
  min-height: 120px;
}

.toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}

h4 {
  margin: 18px 0 8px;
}

.r-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.right {
  margin-left: auto;
}

.pre {
  white-space: pre-wrap;
  line-height: 1.7;
  margin: 8px 0;
}

.block {
  white-space: pre-wrap;
  word-break: break-all;
  background: var(--el-fill-color-light);
  padding: 12px;
  border-radius: 6px;
  font-size: 13px;
  line-height: 1.7;
  max-height: 60vh;
  overflow: auto;
  margin: 0;
}

.diff div {
  white-space: pre-wrap;
}

.diff .add,
span.add {
  background: var(--el-color-primary-light-8);
  color: var(--el-color-primary-dark-2);
}

.diff .del,
span.del {
  color: var(--el-text-color-secondary);
  text-decoration: line-through;
}

.err {
  color: var(--el-color-danger);
}

.muted {
  color: var(--el-text-color-secondary);
}

.small {
  font-size: 12px;
}

.gap {
  margin-left: 8px;
}

.mb {
  margin-bottom: 12px;
}

.mb-s {
  margin-bottom: 6px;
}
</style>
