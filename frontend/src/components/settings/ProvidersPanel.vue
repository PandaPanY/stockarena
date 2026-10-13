<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { errorMessage } from '@/api/http'
import {
  providersApi,
  type Protocol,
  type Provider,
  type ProviderInput,
  type ProviderPreset,
  type ProviderTestResult,
} from '@/api/arena'

const PROTOCOL_LABELS: Record<Protocol, string> = {
  openai_compatible: 'OpenAI 兼容',
  anthropic: 'Anthropic',
  gemini: 'Google Gemini',
}

const providers = ref<Provider[]>([])
const presets = ref<ProviderPreset[]>([])
const loading = ref(false)

const dialogVisible = ref(false)
const editingId = ref<number | null>(null)
const saving = ref(false)
const form = reactive<ProviderInput & { api_key_input: string; keep_key: boolean }>({
  name: '',
  protocol: 'openai_compatible',
  base_url: '',
  api_key: null,
  api_key_input: '',
  keep_key: false,
  default_model: '',
  timeout_s: 600,
  price_in_per_m: '0',
  price_out_per_m: '0',
  supports_json_mode: true,
  knowledge_cutoff: null,
})
const modelOptions = ref<string[]>([])
const fetchingModels = ref(false)

const testing = ref<number | null>(null)
const testResult = ref<{ id: number; result: ProviderTestResult } | null>(null)

const dialogTitle = computed(() => (editingId.value ? '编辑模型供应商' : '添加模型供应商'))

async function load() {
  loading.value = true
  try {
    ;[providers.value, presets.value] = await Promise.all([providersApi.list(), providersApi.presets()])
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    loading.value = false
  }
}

function openCreate() {
  editingId.value = null
  Object.assign(form, {
    name: '',
    protocol: 'openai_compatible',
    base_url: '',
    api_key: null,
    api_key_input: '',
    keep_key: false,
    default_model: '',
    timeout_s: 600,
    price_in_per_m: '0',
    price_out_per_m: '0',
    supports_json_mode: true,
    knowledge_cutoff: null,
  })
  modelOptions.value = []
  dialogVisible.value = true
}

function openEdit(p: Provider) {
  editingId.value = p.id
  Object.assign(form, { ...p, base_url: p.base_url ?? '', api_key_input: '', keep_key: p.has_api_key })
  modelOptions.value = []
  dialogVisible.value = true
}

function applyPreset(name: string) {
  const preset = presets.value.find((p) => p.name === name)
  if (!preset) return
  Object.assign(form, {
    name: preset.name.replace(/（.*）/, ''),
    protocol: preset.protocol,
    base_url: preset.base_url,
    default_model: preset.default_model,
    price_in_per_m: preset.price_in_per_m ?? '0',
    price_out_per_m: preset.price_out_per_m ?? '0',
  })
}

function payload(): ProviderInput {
  // keep_key：编辑时未填写新密钥则保持原密钥（api_key = null）
  const apiKey = form.api_key_input ? form.api_key_input : form.keep_key ? null : ''
  return {
    name: form.name,
    protocol: form.protocol,
    base_url: form.base_url || null,
    api_key: apiKey,
    default_model: form.default_model,
    timeout_s: form.timeout_s,
    price_in_per_m: String(form.price_in_per_m || '0'),
    price_out_per_m: String(form.price_out_per_m || '0'),
    supports_json_mode: form.supports_json_mode,
    knowledge_cutoff: form.knowledge_cutoff || null,
  }
}

async function save() {
  if (!form.name.trim()) return ElMessage.warning('请填写名称')
  saving.value = true
  try {
    if (editingId.value) await providersApi.update(editingId.value, payload())
    else await providersApi.create(payload())
    ElMessage.success('已保存')
    dialogVisible.value = false
    await load()
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    saving.value = false
  }
}

async function fetchModels() {
  if (!editingId.value) {
    // 新建时需要先保存才能用服务器端的密钥请求模型列表
    return ElMessage.info('请先保存，再获取模型列表')
  }
  fetchingModels.value = true
  try {
    const res = await providersApi.models(editingId.value)
    if (res.error) ElMessage.warning(res.error)
    modelOptions.value = res.models
    if (res.models.length && !form.default_model) form.default_model = res.models[0]
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    fetchingModels.value = false
  }
}

async function test(p: Provider) {
  testing.value = p.id
  testResult.value = null
  try {
    testResult.value = { id: p.id, result: await providersApi.test(p.id) }
  } catch (err) {
    testResult.value = { id: p.id, result: { ok: false, error: errorMessage(err), latency_ms: 0 } }
  } finally {
    testing.value = null
  }
}

async function remove(p: Provider) {
  await ElMessageBox.confirm(`确定删除「${p.name}」？`, '删除供应商', { type: 'warning' })
  try {
    await providersApi.remove(p.id)
    ElMessage.success('已删除')
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
      <span class="hint"
        >支持任意 OpenAI 兼容接口（含 Ollama、LM Studio、vLLM、llama.cpp 等本地服务）、Anthropic 与 Gemini。</span
      >
      <el-button type="primary" icon="Plus" @click="openCreate">添加供应商</el-button>
    </div>

    <el-table v-loading="loading" :data="providers" empty-text="还没有模型供应商，点击右上角添加">
      <el-table-column prop="name" label="名称" min-width="120" />
      <el-table-column label="协议" width="130">
        <template #default="{ row }">{{ PROTOCOL_LABELS[row.protocol as Protocol] }}</template>
      </el-table-column>
      <el-table-column prop="base_url" label="接口地址" min-width="200" show-overflow-tooltip />
      <el-table-column prop="default_model" label="默认模型" min-width="160" show-overflow-tooltip />
      <el-table-column label="API Key" width="110">
        <template #default="{ row }">
          <el-tooltip v-if="row.api_key_error" :content="row.api_key_error">
            <span class="key-error">{{ row.api_key_masked }}</span>
          </el-tooltip>
          <template v-else>{{ row.has_api_key ? row.api_key_masked : '未设置' }}</template>
        </template>
      </el-table-column>
      <el-table-column label="单价（每百万 token）" width="160">
        <template #default="{ row }">入 {{ row.price_in_per_m }} / 出 {{ row.price_out_per_m }}</template>
      </el-table-column>
      <el-table-column label="操作" width="220" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" :loading="testing === row.id" @click="test(row)">测试连接</el-button>
          <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
          <el-button link type="danger" @click="remove(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-alert
      v-if="testResult"
      class="mt"
      :type="testResult.result.ok ? 'success' : 'error'"
      :closable="true"
      show-icon
      @close="testResult = null"
    >
      <template #title>
        <span v-if="testResult.result.ok">
          连接成功（{{ (testResult.result.latency_ms / 1000).toFixed(1) }} 秒）：{{ testResult.result.reply }}
          <span v-if="testResult.result.has_reasoning">· 模型返回了推理过程</span>
        </span>
        <span v-else>连接失败：{{ testResult.result.error }}</span>
      </template>
    </el-alert>

    <el-dialog v-model="dialogVisible" :title="dialogTitle" width="620px" destroy-on-close>
      <el-form label-width="120px" @submit.prevent>
        <el-form-item v-if="!editingId" label="从模板填充">
          <el-select placeholder="选择常用供应商" style="width: 100%" @change="applyPreset">
            <el-option v-for="p in presets" :key="p.name" :label="p.name" :value="p.name" />
          </el-select>
        </el-form-item>
        <el-form-item label="名称" required>
          <el-input v-model="form.name" maxlength="64" />
        </el-form-item>
        <el-form-item label="协议" required>
          <el-radio-group v-model="form.protocol">
            <el-radio-button v-for="(label, key) in PROTOCOL_LABELS" :key="key" :value="key">{{
              label
            }}</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="接口地址">
          <el-input
            v-model="form.base_url"
            placeholder="如 http://192.168.1.10:8000/v1；Anthropic / Gemini 官方接口可留空"
          />
        </el-form-item>
        <el-form-item label="API Key">
          <el-input
            v-model="form.api_key_input"
            type="password"
            show-password
            :placeholder="editingId && form.keep_key ? '已设置（留空表示不修改）' : '本地模型通常不需要'"
          />
          <el-checkbox v-if="editingId && form.keep_key" v-model="form.keep_key" class="keep-key"
            >保留原密钥</el-checkbox
          >
        </el-form-item>
        <el-form-item label="默认模型">
          <div class="model-row">
            <el-select
              v-model="form.default_model"
              filterable
              allow-create
              default-first-option
              placeholder="输入或选择模型名"
            >
              <el-option v-for="m in modelOptions" :key="m" :label="m" :value="m" />
            </el-select>
            <el-button v-if="form.protocol === 'openai_compatible'" :loading="fetchingModels" @click="fetchModels">
              获取列表
            </el-button>
          </div>
        </el-form-item>
        <el-form-item label="超时（秒）">
          <el-input-number v-model="form.timeout_s" :min="10" :max="3600" />
        </el-form-item>
        <el-form-item label="单价（元或美元）">
          <div class="price-row">
            <el-input v-model="form.price_in_per_m"><template #prepend>输入/百万</template></el-input>
            <el-input v-model="form.price_out_per_m"><template #prepend>输出/百万</template></el-input>
          </div>
        </el-form-item>
        <el-form-item label="JSON 输出模式">
          <el-switch v-model="form.supports_json_mode" />
          <span class="hint inline">模型不支持 response_format 时关闭</span>
        </el-form-item>
        <el-form-item label="知识截止日期">
          <el-date-picker
            v-model="form.knowledge_cutoff"
            type="date"
            value-format="YYYY-MM-DD"
            placeholder="可选，用于前视偏差提示"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.key-error {
  color: var(--el-color-danger);
}

.toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
  gap: 12px;
}

.hint {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}

.hint.inline {
  margin-left: 12px;
}

.mt {
  margin-top: 12px;
}

.model-row,
.price-row {
  display: flex;
  gap: 8px;
  width: 100%;
}

.model-row .el-select {
  flex: 1;
}

.keep-key {
  margin-top: 4px;
}
</style>
