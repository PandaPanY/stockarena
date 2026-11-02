<script setup lang="ts">
/**
 * AI 助手：问答（AI 查自己账户的数据后回答）、每周点评、偏差分析。
 * AI 只解释跟踪结果和策略规则，不给买卖建议、不预测走势；数字都来自程序计算。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { providersApi, type Provider } from '@/api/arena'
import { errorMessage } from '@/api/http'
import { insightApi, TOOL_TEXT, type AiNote, type AskResult, type ChatTurn, type InsightOverview } from '@/api/insight'
import { formatDateTime } from '@/utils/format'
import { parseGuide } from '@/utils/guide'

const data = ref<InsightOverview | null>(null)
const providers = ref<Provider[]>([])
const loading = ref(false)
const tab = ref<'chat' | 'weekly' | 'deviation' | 'settings'>('chat')
let poll: ReturnType<typeof setInterval> | undefined

async function load(silent = false) {
  if (!silent) loading.value = true
  try {
    data.value = await insightApi.overview()
  } catch (err) {
    if (!silent) ElMessage.error(errorMessage(err))
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  restoreChat()
  await load()
  try {
    providers.value = await providersApi.list()
  } catch {
    /* 模型列表只用于设置 */
  }
  if (data.value?.running) startPoll()
})
onBeforeUnmount(() => clearInterval(poll))

function startPoll() {
  clearInterval(poll)
  poll = setInterval(async () => {
    await load(true)
    if (!data.value?.running) clearInterval(poll)
  }, 5000)
}

const md = (text: string) => parseGuide(text).html
const noModel = computed(() => !!data.value && !data.value.settings.provider_id)

// ---------------------------------------------------------------- 问答

interface Bubble extends ChatTurn {
  trace?: AskResult['trace']
  meta?: string
  error?: boolean
}

const CHAT_KEY = 'sa-insight-chat'
const chat = ref<Bubble[]>([])
const question = ref('')
const asking = ref(false)
const chatBox = ref<HTMLElement | null>(null)
const EXAMPLES = [
  '我在跟踪哪些组合？最近一周表现怎么样？',
  '我的组合现在占比最大的持仓是哪几只？',
  '最近一次调仓卖了什么，为什么卖？',
  '组合里国债、黄金这类 ETF 现在各占多少？',
]

function restoreChat() {
  try {
    chat.value = JSON.parse(sessionStorage.getItem(CHAT_KEY) || '[]')
  } catch {
    chat.value = []
  }
}

function saveChat() {
  try {
    sessionStorage.setItem(CHAT_KEY, JSON.stringify(chat.value.slice(-30)))
  } catch {
    /* 存不了就只在本页有效 */
  }
}

async function scrollDown() {
  await nextTick()
  chatBox.value?.scrollTo({ top: chatBox.value.scrollHeight, behavior: 'smooth' })
}

async function ask(text?: string) {
  const q = (text ?? question.value).trim()
  if (!q || asking.value) return
  const history: ChatTurn[] = chat.value.filter((b) => !b.error).map((b) => ({ role: b.role, content: b.content }))
  chat.value.push({ role: 'user', content: q })
  question.value = ''
  asking.value = true
  scrollDown()
  try {
    const r = await insightApi.ask(q, history)
    if (r.status === 'ok') {
      chat.value.push({
        role: 'assistant',
        content: r.answer,
        trace: r.trace,
        meta: `${r.model} · ${r.seconds} 秒`,
      })
    } else {
      chat.value.push({ role: 'assistant', content: `没有得到回答：${r.error || r.status}`, error: true })
    }
  } catch (err) {
    chat.value.push({ role: 'assistant', content: `没有得到回答：${errorMessage(err)}`, error: true })
  } finally {
    asking.value = false
    saveChat()
    scrollDown()
  }
}

function onKey(e: KeyboardEvent) {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
    e.preventDefault()
    ask()
  }
}

function clearChat() {
  chat.value = []
  saveChat()
}

// ---------------------------------------------------------------- 点评与分析

const refFilter = ref<string>('')
const refKey = (kind: string, id: number) => `${kind}:${id}`

function notesOf(kind: 'weekly' | 'deviation'): AiNote[] {
  return (data.value?.notes ?? []).filter(
    (n) => n.kind === kind && (!refFilter.value || refKey(n.ref_kind, n.ref_id) === refFilter.value),
  )
}

const generating = ref(false)

async function generate(kind: 'weekly' | 'deviation') {
  const [refKind, refId] = refFilter.value ? refFilter.value.split(':') : []
  generating.value = true
  try {
    const r = await insightApi.run({ kind, ...(refKind ? { ref_kind: refKind, ref_id: Number(refId) } : {}) })
    ElMessage[r.started ? 'success' : 'warning'](r.message)
    await load(true)
    startPoll()
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    generating.value = false
  }
}

const detail = ref<AiNote | null>(null)
const showDetail = computed({
  get: () => !!detail.value,
  set: (v: boolean) => {
    if (!v) detail.value = null
  },
})

async function showMaterial(n: AiNote) {
  try {
    detail.value = await insightApi.note(n.id)
  } catch (err) {
    ElMessage.error(errorMessage(err))
  }
}

const period = (n: AiNote) => (n.period_start ? `${n.period_start} ～ ${n.period_end}` : '')

// ---------------------------------------------------------------- 设置

const DEFAULT_MODEL = 0
const providerChoice = computed({
  get: () => (data.value?.settings.own_model ? (data.value.settings.provider_id ?? DEFAULT_MODEL) : DEFAULT_MODEL),
  set: (v: number) => saveSettings(v === DEFAULT_MODEL ? { use_default_model: true } : { provider_id: v }),
})

async function saveSettings(body: Parameters<typeof insightApi.saveSettings>[0]) {
  try {
    const s = await insightApi.saveSettings(body)
    if (data.value) data.value.settings = s
    ElMessage.success('已保存')
  } catch (err) {
    ElMessage.error(errorMessage(err))
  }
}

const currentModel = computed(() => {
  const id = data.value?.settings.provider_id
  const p = providers.value.find((x) => x.id === id)
  return p ? `${p.name}（${data.value?.settings.model || p.default_model}）` : '未选择'
})
</script>

<template>
  <div v-loading="loading && !data" class="insight">
    <div class="page-head">
      <h2>AI 助手</h2>
      <span class="muted small">AI 读取你跟踪中的策略和组合的数据，用大白话解释；只解释，不给买卖建议、不预测走势</span>
    </div>

    <template v-if="data">
      <el-alert v-if="noModel" type="warning" :closable="false" class="tip">
        还没有可用的模型：先在「配置中心 → 模型供应商」添加，再到本页「设置」里选择。
      </el-alert>
      <el-alert v-else-if="!data.followed.length" type="info" :closable="false" class="tip">
        还没有实时跟踪的策略或组合。在策略库或多风格组合页点「开始实时跟踪」后，这里才有数据可以分析。
      </el-alert>

      <el-tabs v-model="tab">
        <el-tab-pane label="问答" name="chat">
          <el-card shadow="never" class="card chat-card">
            <div ref="chatBox" class="chat">
              <div v-if="!chat.length" class="examples">
                <div class="muted small">可以这样问（AI 会先查数据再回答，本地模型可能要一两分钟）：</div>
                <el-button v-for="q in EXAMPLES" :key="q" size="small" round :disabled="noModel" @click="ask(q)">
                  {{ q }}
                </el-button>
              </div>
              <div v-for="(b, k) in chat" :key="k" class="bubble" :class="[b.role, { error: b.error }]">
                <div v-if="b.role === 'user'" class="text">{{ b.content }}</div>
                <template v-else>
                  <!-- 模型输出先转义再渲染（parseGuide），不会被当成 HTML 执行 -->
                  <div class="text md" v-html="md(b.content)" />
                  <div v-if="b.trace?.length || b.meta" class="trace muted small">
                    <el-tag v-for="(t, i) in b.trace" :key="i" size="small" type="info" effect="plain">
                      {{ TOOL_TEXT[t.tool] || t.tool }}
                    </el-tag>
                    <span v-if="b.meta">{{ b.meta }}</span>
                  </div>
                </template>
              </div>
              <div v-if="asking" class="bubble assistant">
                <div class="text muted">正在查数据、组织回答…</div>
              </div>
            </div>
            <div class="composer">
              <el-input
                v-model="question"
                type="textarea"
                :autosize="{ minRows: 1, maxRows: 4 }"
                resize="none"
                maxlength="2000"
                placeholder="问问你的组合，例如：最近一次为什么卖出招商银行？（Enter 发送，Shift+Enter 换行）"
                :disabled="noModel"
                @keydown="onKey"
              />
              <el-button type="primary" :loading="asking" :disabled="noModel || !question.trim()" @click="ask()">
                发送
              </el-button>
              <el-button v-if="chat.length" text @click="clearChat">清空</el-button>
            </div>
          </el-card>
        </el-tab-pane>

        <el-tab-pane v-for="kind in ['weekly', 'deviation'] as const" :key="kind" :name="kind">
          <template #label>
            {{ kind === 'weekly' ? '每周点评' : '偏差分析' }}
          </template>
          <el-card shadow="never" class="card">
            <div class="toolbar">
              <el-select v-model="refFilter" placeholder="全部策略 / 组合" clearable style="width: 220px">
                <el-option
                  v-for="f in data.followed"
                  :key="refKey(f.kind, f.id)"
                  :label="f.name"
                  :value="refKey(f.kind, f.id)"
                />
              </el-select>
              <span class="spacer" />
              <span class="muted small">
                {{
                  kind === 'weekly'
                    ? '每周六 09:00 自动生成并推送'
                    : '交易日 21:40 检查：健康度为「提醒」时自动分析（每个账户每周最多一次）'
                }}
              </span>
              <el-button
                :loading="generating || data.running"
                :disabled="noModel || !data.followed.length || (kind === 'deviation' && !refFilter)"
                :title="kind === 'deviation' && !refFilter ? '先在左边选一个策略或组合' : ''"
                @click="generate(kind)"
              >
                {{ data.running ? '生成中…' : kind === 'weekly' ? '立即生成' : '立即分析' }}
              </el-button>
            </div>
            <el-empty v-if="!notesOf(kind).length" :description="kind === 'weekly' ? '还没有点评' : '还没有分析'" />
            <div v-for="n in notesOf(kind)" :key="n.id" class="note">
              <div class="note-head">
                <strong>{{ n.ref_name }}</strong>
                <span class="muted small">{{ period(n) }}</span>
                <el-tag v-if="n.status !== 'ok'" type="danger" size="small">生成失败</el-tag>
                <span class="spacer" />
                <el-button link size="small" @click="showMaterial(n)">看数据</el-button>
              </div>
              <div v-if="n.status === 'ok'" class="md" v-html="md(n.content)" />
              <div v-else class="err small">{{ n.error }}</div>
              <div class="muted small">
                {{ formatDateTime(n.created_at) }} · {{ n.model }} · {{ n.tokens_in + n.tokens_out }} token
              </div>
            </div>
          </el-card>
        </el-tab-pane>

        <el-tab-pane label="设置" name="settings">
          <el-card shadow="never" class="card">
            <el-form label-width="110px">
              <el-form-item label="使用的模型">
                <el-select v-model="providerChoice" style="width: 300px">
                  <el-option :value="DEFAULT_MODEL" label="沿用自选股监控的模型" />
                  <el-option
                    v-for="p in providers"
                    :key="p.id"
                    :value="p.id"
                    :label="`${p.name}（${p.default_model}）`"
                  />
                </el-select>
                <span class="muted small hint"
                  >当前：{{ currentModel }}。需要支持工具调用（DeepSeek、Qwen 等都可以）</span
                >
              </el-form-item>
              <el-form-item label="每周点评">
                <el-switch
                  :model-value="data.settings.weekly"
                  @update:model-value="(v: string | number | boolean) => saveSettings({ weekly: !!v })"
                />
                <span class="muted small hint"
                  >每周六 09:00 为每个跟踪中的策略 / 组合写一段点评，推送事件「AI 每周点评」</span
                >
              </el-form-item>
              <el-form-item label="偏差分析">
                <el-switch
                  :model-value="data.settings.deviation"
                  @update:model-value="(v: string | number | boolean) => saveSettings({ deviation: !!v })"
                />
                <span class="muted small hint">健康度为「提醒」时分析可能的原因，随「健康度与可实盘」推送</span>
              </el-form-item>
            </el-form>
            <el-alert type="info" :closable="false">
              推送：在「配置中心 → 消息推送」的通道里勾选「AI 每周点评」才会收到每周点评（新建的通道默认勾选）。
            </el-alert>
          </el-card>
        </el-tab-pane>
      </el-tabs>
    </template>

    <el-dialog v-model="showDetail" :title="detail ? `交给模型的数据：${detail.ref_name}` : ''" width="760px">
      <div class="muted small">AI 写的内容都应能在这些数据里找到依据；收益、权重是小数（0.012 = 1.2%）</div>
      <pre class="material">{{ JSON.stringify(detail?.material, null, 2) }}</pre>
    </el-dialog>
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

.tip,
.card {
  margin-bottom: 16px;
}

.toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
}

.spacer {
  flex: 1;
}

.chat-card :deep(.el-card__body) {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.chat {
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-height: 320px;
  max-height: 60vh;
  overflow-y: auto;
  padding-right: 4px;
}

.examples {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 8px;
}

.examples .el-button + .el-button {
  margin-left: 0;
}

.bubble {
  max-width: 85%;
}

.bubble.user {
  align-self: flex-end;
}

.bubble .text {
  padding: 8px 12px;
  border-radius: 8px;
  background: var(--el-fill-color-light);
  white-space: pre-wrap;
  word-break: break-word;
}

.bubble.user .text {
  background: var(--el-color-primary-light-9);
}

.bubble.error .text {
  color: var(--el-color-danger);
}

.bubble .text.md {
  white-space: normal;
}

.trace {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px;
  margin-top: 4px;
}

.composer {
  display: flex;
  align-items: flex-end;
  gap: 8px;
}

.note {
  padding: 12px 0;
  border-bottom: 1px solid var(--el-border-color-lighter);
}

.note-head {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 8px;
}

.md :deep(p),
.md :deep(ul),
.md :deep(ol) {
  margin: 6px 0;
}

.md :deep(table) {
  border-collapse: collapse;
  margin: 6px 0;
}

.md :deep(th),
.md :deep(td) {
  border: 1px solid var(--el-border-color);
  padding: 2px 8px;
}

.hint {
  margin-left: 8px;
}

.err {
  color: var(--el-color-danger);
}

.material {
  max-height: 60vh;
  overflow: auto;
  font-size: 12px;
  background: var(--el-fill-color-light);
  padding: 8px;
}

@media (max-width: 640px) {
  .bubble {
    max-width: 100%;
  }
}
</style>
