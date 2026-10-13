<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { errorMessage } from '@/api/http'
import {
  agentsApi,
  metaApi,
  providersApi,
  type Agent,
  type Persona,
  type PromptTemplate,
  type Provider,
} from '@/api/arena'

const agents = ref<Agent[]>([])
const providers = ref<Provider[]>([])
const templates = ref<PromptTemplate[]>([])
const personas = ref<Persona[]>([])
const loading = ref(false)

const dialogVisible = ref(false)
const editingId = ref<number | null>(null)
const saving = ref(false)
const form = reactive({
  name: '',
  provider_id: null as number | null,
  model: '',
  temperature: null as number | null,
  max_tokens: null as number | null,
  extra: '',
  prompt_template_id: null as number | null,
  persona: '',
  memory_rounds: 5,
  description: '',
  reflect_enabled: false,
  reflect_every: 5,
  initial_strategy: '',
})

const llmAgents = computed(() => agents.value.filter((a) => a.kind === 'llm'))
const baselines = computed(() => agents.value.filter((a) => a.kind === 'baseline'))
const selectedProvider = computed(() => providers.value.find((p) => p.id === form.provider_id))

async function load() {
  loading.value = true
  try {
    ;[agents.value, providers.value, templates.value, personas.value] = await Promise.all([
      agentsApi.list(),
      providersApi.list(),
      metaApi.templates(),
      metaApi.personas(),
    ])
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    loading.value = false
  }
}

function openCreate() {
  if (!providers.value.length) return ElMessage.warning('请先在「模型供应商」中添加一个供应商')
  editingId.value = null
  Object.assign(form, {
    name: '',
    provider_id: providers.value[0].id,
    model: '',
    temperature: 0.3,
    max_tokens: null,
    extra: '',
    prompt_template_id: null,
    persona: '',
    memory_rounds: 5,
    description: '',
    reflect_enabled: false,
    reflect_every: 5,
    initial_strategy: '',
  })
  dialogVisible.value = true
}

function openEdit(a: Agent) {
  editingId.value = a.id
  const { temperature, max_tokens, ...rest } = a.params as { temperature?: number; max_tokens?: number }
  Object.assign(form, {
    name: a.name,
    provider_id: a.provider_id,
    model: a.model ?? '',
    temperature: temperature ?? null,
    max_tokens: max_tokens ?? null,
    extra: Object.keys(rest).length ? JSON.stringify(rest, null, 2) : '',
    prompt_template_id: a.prompt_template_id,
    persona: a.persona,
    memory_rounds: a.memory_rounds,
    description: a.description,
    reflect_enabled: a.reflect_enabled,
    reflect_every: a.reflect_every,
    initial_strategy: a.initial_strategy,
  })
  dialogVisible.value = true
}

async function save() {
  if (!form.name.trim() || !form.provider_id) return ElMessage.warning('请填写名称并选择供应商')
  let extra: Record<string, unknown> = {}
  if (form.extra.trim()) {
    try {
      extra = JSON.parse(form.extra)
    } catch {
      return ElMessage.error('高级参数不是合法的 JSON')
    }
  }
  const params: Record<string, unknown> = { ...extra }
  if (form.temperature !== null) params.temperature = form.temperature
  if (form.max_tokens) params.max_tokens = form.max_tokens
  const body = {
    name: form.name,
    provider_id: form.provider_id,
    model: form.model || null,
    params,
    prompt_template_id: form.prompt_template_id,
    persona: form.persona,
    memory_rounds: form.memory_rounds,
    description: form.description,
    reflect_enabled: form.reflect_enabled,
    reflect_every: form.reflect_every,
    initial_strategy: form.initial_strategy,
  }
  saving.value = true
  try {
    if (editingId.value) await agentsApi.update(editingId.value, body)
    else await agentsApi.create(body)
    ElMessage.success('已保存')
    dialogVisible.value = false
    await load()
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    saving.value = false
  }
}

function applyPersona(name: string) {
  const p = personas.value.find((x) => x.name === name)
  if (!p) return
  form.persona = p.persona
  if (!form.description) form.description = p.title
}

async function archive(a: Agent) {
  await ElMessageBox.confirm(`归档后「${a.name}」不再出现在列表中，历史比赛记录会保留。确定？`, '归档选手', {
    type: 'warning',
  })
  try {
    await agentsApi.archive(a.id)
    await load()
  } catch (err) {
    ElMessage.error(errorMessage(err))
  }
}

onMounted(load)
</script>

<template>
  <div>
    <div class="toolbar">
      <span class="hint">选手 = 模型 + 参数 + prompt 模板 + 投资风格。同一个模型配不同风格就是不同的选手。</span>
      <el-button type="primary" icon="Plus" @click="openCreate">添加 AI 选手</el-button>
    </div>

    <h4 class="section">AI 选手</h4>
    <el-table v-loading="loading" :data="llmAgents" empty-text="还没有 AI 选手">
      <el-table-column prop="name" label="名称" min-width="140" />
      <el-table-column prop="provider_name" label="供应商" min-width="110" />
      <el-table-column prop="model" label="模型" min-width="180" show-overflow-tooltip />
      <el-table-column label="参数" min-width="140">
        <template #default="{ row }">
          <span class="mono">{{ Object.keys(row.params).length ? JSON.stringify(row.params) : '默认' }}</span>
        </template>
      </el-table-column>
      <el-table-column prop="prompt_template" label="模板" min-width="140" />
      <el-table-column label="风格" min-width="160" show-overflow-tooltip>
        <template #default="{ row }">{{ row.persona || '—' }}</template>
      </el-table-column>
      <el-table-column label="自我进化" width="120">
        <template #default="{ row }">
          <el-tag v-if="row.reflect_enabled" size="small" type="success">每 {{ row.reflect_every }} 日复盘</el-tag>
          <span v-else class="hint">—</span>
        </template>
      </el-table-column>
      <el-table-column prop="track_count" label="参赛数" width="80" />
      <el-table-column label="操作" width="130" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
          <el-button link type="danger" @click="archive(row)">归档</el-button>
        </template>
      </el-table-column>
    </el-table>

    <h4 class="section">基线选手（系统预置，用于对照）</h4>
    <el-table :data="baselines">
      <el-table-column prop="name" label="名称" width="200" />
      <el-table-column prop="description" label="策略" />
    </el-table>

    <el-dialog
      v-model="dialogVisible"
      :title="editingId ? '编辑 AI 选手' : '添加 AI 选手'"
      width="640px"
      destroy-on-close
    >
      <el-form label-width="110px" @submit.prevent>
        <el-form-item label="名称" required><el-input v-model="form.name" maxlength="64" /></el-form-item>
        <el-form-item label="供应商" required>
          <el-select v-model="form.provider_id" style="width: 100%">
            <el-option v-for="p in providers" :key="p.id" :label="p.name" :value="p.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="模型">
          <el-input
            v-model="form.model"
            :placeholder="`留空使用供应商默认模型：${selectedProvider?.default_model || '未设置'}`"
          />
        </el-form-item>
        <el-form-item label="temperature">
          <el-input-number v-model="form.temperature" :min="0" :max="2" :step="0.1" :precision="2" />
          <span class="hint inline">新一代 Claude 不支持该参数，请清空</span>
        </el-form-item>
        <el-form-item label="max_tokens">
          <el-input-number v-model="form.max_tokens" :min="256" :max="128000" :step="1024" placeholder="默认" />
          <span class="hint inline">推理模型建议 8000 以上</span>
        </el-form-item>
        <el-form-item label="高级参数">
          <el-input
            v-model="form.extra"
            type="textarea"
            :rows="3"
            class="mono"
            placeholder='JSON，如 {"effort": "medium"} 或 {"extra_body": {"enable_thinking": true}}'
          />
        </el-form-item>
        <el-form-item label="prompt 模板">
          <el-select v-model="form.prompt_template_id" clearable placeholder="默认模板（最新版本）" style="width: 100%">
            <el-option v-for="t in templates" :key="t.id" :label="`${t.title} v${t.version}`" :value="t.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="投资风格">
          <el-select
            placeholder="从风格库套用（可再修改）"
            size="small"
            style="width: 100%; margin-bottom: 6px"
            :model-value="null"
            @change="applyPersona"
          >
            <el-option v-for="p in personas" :key="p.name" :value="p.name" :label="`${p.title}：${p.description}`" />
          </el-select>
          <el-input
            v-model="form.persona"
            type="textarea"
            :rows="4"
            placeholder="可选。如：价值投资，偏好低估值高股息的蓝筹，持仓周期较长，极少追涨"
          />
        </el-form-item>
        <el-form-item label="记忆轮数">
          <el-input-number v-model="form.memory_rounds" :min="0" :max="20" />
          <span class="hint inline">把最近几轮自己的决策摘要回传给 AI</span>
        </el-form-item>
        <el-form-item label="自我进化">
          <el-switch v-model="form.reflect_enabled" />
          <template v-if="form.reflect_enabled">
            <span class="hint inline">每</span>
            <el-input-number v-model="form.reflect_every" :min="1" :max="60" size="small" class="gap" />
            <span class="hint">个交易日复盘一次</span>
          </template>
          <div class="hint block">
            开启后，AI
            会定期查看自己的收益、交易和排名，总结经验并改写「策略笔记」；之后每次决策都会带上最新的策略笔记。
            每个赛道里的笔记独立演进，可在账户页查看每一版的变化并回滚。
          </div>
        </el-form-item>
        <el-form-item label="初始策略">
          <el-input
            v-model="form.initial_strategy"
            type="textarea"
            :rows="4"
            maxlength="2000"
            show-word-limit
            placeholder="可选。赛道启动时作为策略笔记第 1 版；留空则由 AI 在第一次复盘时自己写"
          />
        </el-form-item>
        <el-form-item label="备注"><el-input v-model="form.description" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
}

.hint {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}

.hint.inline {
  margin-left: 12px;
}

.hint.block {
  display: block;
  width: 100%;
  line-height: 1.6;
  margin-top: 4px;
}

.gap {
  margin: 0 6px;
}

.section {
  margin: 20px 0 8px;
  font-size: 14px;
  color: var(--el-text-color-regular);
}

.mono {
  font-family: ui-monospace, Consolas, monospace;
  font-size: 12px;
}
</style>
