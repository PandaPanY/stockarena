<script setup lang="ts">
/** MCP 接入：管理个人 API 令牌，给出 Claude Code、Cursor 等客户端的配置方法。 */
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { errorMessage } from '@/api/http'
import { authApi, type ApiToken } from '@/api/auth'
import { formatDateTime } from '@/utils/format'

const list = ref<ApiToken[]>([])
const loading = ref(false)
const form = reactive({ open: false, name: '', canWrite: false, saving: false })
const created = ref<ApiToken | null>(null)

const endpoint = `${window.location.origin}/api/mcp`
const insecure = window.location.protocol === 'http:' && !['localhost', '127.0.0.1'].includes(window.location.hostname)

const shownToken = computed(() => created.value?.token ?? '<你的令牌>')
const claudeCmd = computed(
  () => `claude mcp add --transport http stockarena ${endpoint} --header "Authorization: Bearer ${shownToken.value}"`,
)
const jsonConfig = computed(() =>
  JSON.stringify(
    {
      mcpServers: {
        stockarena: { type: 'http', url: endpoint, headers: { Authorization: `Bearer ${shownToken.value}` } },
      },
    },
    null,
    2,
  ),
)

async function load() {
  loading.value = true
  try {
    list.value = await authApi.tokens()
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    loading.value = false
  }
}

function openCreate() {
  Object.assign(form, { open: true, name: '', canWrite: false, saving: false })
}

async function create() {
  if (!form.name.trim()) return ElMessage.warning('请填写名称，例如用在哪个客户端')
  form.saving = true
  try {
    created.value = await authApi.createToken(form.name.trim(), form.canWrite)
    form.open = false
    await load()
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    form.saving = false
  }
}

async function revoke(t: ApiToken) {
  try {
    await ElMessageBox.confirm(`撤销后使用「${t.name}」的客户端立即无法访问。`, '撤销令牌', { type: 'warning' })
    await authApi.revokeToken(t.id)
    if (created.value?.id === t.id) created.value = null
    await load()
  } catch (err) {
    if (err !== 'cancel') ElMessage.error(errorMessage(err))
  }
}

async function copy(text: string) {
  try {
    await navigator.clipboard.writeText(text)
    ElMessage.success('已复制')
  } catch {
    ElMessage.warning('浏览器不允许自动复制，请手动选中复制')
  }
}

onMounted(load)
</script>

<template>
  <div>
    <p class="muted">
      通过 <a href="https://modelcontextprotocol.io" target="_blank" rel="noopener">MCP</a>，可以让 Claude Code、Cursor
      等 AI 客户端直接查询你的策略、看优化时间线、试算规则；令牌勾选「允许修改」后，还能新建策略、提交新版本、启动 AI
      优化。权限与网页一致：只能看到自己的策略和公共策略。
    </p>
    <ul class="muted rules">
      <li>只提供开发期结果，不提供留出期和实时跟踪，避免 AI 按留出期挑选策略（这些请在网页上看）。</li>
      <li>通过 MCP 试算的次数会计入下次新建策略的测试次数，与网页新建对话框相同。</li>
      <li>令牌只能访问 MCP 服务，不能登录网页或调用其他接口；只在创建时显示一次，请妥善保存。</li>
    </ul>
    <el-alert
      v-if="insecure"
      type="warning"
      :closable="false"
      show-icon
      class="gap"
      title="当前通过 HTTP 访问：令牌会以明文经过网络。只在可信的局域网里这样用；从公网访问请先配置 HTTPS。"
    />

    <div class="bar">
      <span
        >服务地址：<code>{{ endpoint }}</code></span
      >
      <el-button type="primary" size="small" @click="openCreate">新建令牌</el-button>
    </div>

    <el-card v-if="created?.token" shadow="never" class="gap created">
      <template #header>
        <b>令牌「{{ created.name }}」已创建</b>
        <span class="muted">（只显示这一次，关闭后无法再查看）</span>
      </template>
      <div class="row">
        <el-input :model-value="created.token" readonly class="mono" />
        <el-button @click="copy(created.token)">复制令牌</el-button>
      </div>
      <p class="label">Claude Code：</p>
      <div class="row">
        <el-input :model-value="claudeCmd" readonly type="textarea" :rows="2" class="mono" />
        <el-button @click="copy(claudeCmd)">复制</el-button>
      </div>
      <p class="label">Cursor 等支持 HTTP 传输的客户端（mcp.json）：</p>
      <div class="row">
        <el-input :model-value="jsonConfig" readonly type="textarea" :rows="9" class="mono" />
        <el-button @click="copy(jsonConfig)">复制</el-button>
      </div>
      <el-button text type="primary" @click="created = null">我已保存，关闭</el-button>
    </el-card>

    <el-table v-loading="loading" :data="list" size="small" class="gap" empty-text="还没有令牌">
      <el-table-column prop="name" label="名称" min-width="140" />
      <el-table-column label="令牌" width="140">
        <template #default="{ row }"
          ><code>{{ row.prefix }}…</code></template
        >
      </el-table-column>
      <el-table-column label="权限" width="110">
        <template #default="{ row }">
          <el-tag v-if="row.can_write" type="warning" size="small">可修改</el-tag>
          <el-tag v-else size="small">只读</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="创建时间" width="170">
        <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="最近使用" width="170">
        <template #default="{ row }">{{ row.last_used_at ? formatDateTime(row.last_used_at) : '从未使用' }}</template>
      </el-table-column>
      <el-table-column width="80">
        <template #default="{ row }">
          <el-button text type="danger" size="small" @click="revoke(row)">撤销</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="form.open" title="新建 API 令牌" width="460px">
      <el-form label-width="80px" @submit.prevent="create">
        <el-form-item label="名称">
          <el-input v-model="form.name" maxlength="64" placeholder="例如：笔记本上的 Claude Code" />
        </el-form-item>
        <el-form-item label="权限">
          <el-checkbox v-model="form.canWrite">允许修改</el-checkbox>
          <div class="muted">
            不勾选时只能查询和试算。勾选后可以新建策略、提交新版本、启动 AI 优化（会消耗你配置的模型额度）。
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="form.open = false">取消</el-button>
        <el-button type="primary" :loading="form.saving" @click="create">创建</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 1.7;
}

.rules {
  margin: 4px 0 12px;
  padding-left: 18px;
}

.gap {
  margin-top: 12px;
}

.bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.row {
  display: flex;
  gap: 8px;
  align-items: flex-start;
}

.label {
  margin: 12px 0 4px;
  font-size: 13px;
}

.mono :deep(input),
.mono :deep(textarea) {
  font-family: ui-monospace, Consolas, monospace;
  font-size: 12px;
}
</style>
