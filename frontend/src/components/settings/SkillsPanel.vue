<script setup lang="ts">
/**
 * 技能：安装外部技能包（说明 + 脚本 / 程序），审批后在隔离环境中供 AI 按需使用。
 * 安全边界：每次安装都要用户批准；命令在一次性容器中运行，看不到平台数据与密钥，只能访问声明的域名。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { errorMessage } from '@/api/http'
import {
  skillsApi,
  splitArgs,
  type RunResult,
  type SandboxStatus,
  type Skill,
  type SkillCall,
  requestsApi,
  type SkillRequest,
} from '@/api/skills'
import { formatDateTime } from '@/utils/format'
import SkillMarketPanel from '@/components/settings/SkillMarketPanel.vue'

const skills = ref<Skill[]>([])
const sandbox = ref<SandboxStatus | null>(null)
const sandboxTask = ref<{ running: boolean; message: string; ok?: boolean } | null>(null)
const loading = ref(false)
let timer: ReturnType<typeof setInterval> | undefined

const STATUS: Record<string, { label: string; type: 'info' | 'warning' | 'success' | 'danger' }> = {
  pending: { label: '待审批', type: 'warning' },
  installing: { label: '安装中', type: 'info' },
  installed: { label: '已安装', type: 'success' },
  failed: { label: '安装失败', type: 'danger' },
  rejected: { label: '已拒绝', type: 'info' },
}
const KIND: Record<string, string> = { doc: '文档', script: '脚本', binary: '程序', other: '其他' }

async function load() {
  loading.value = true
  try {
    const r = await skillsApi.list()
    skills.value = r.skills
    sandbox.value = r.sandbox
    sandboxTask.value = r.sandbox_task
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    loading.value = false
  }
}
onMounted(async () => {
  await load()
  timer = setInterval(() => {
    if (sandboxTask.value?.running || skills.value.some((s) => s.status === 'installing')) void load()
  }, 4000)
})
onBeforeUnmount(() => clearInterval(timer))

async function setupSandbox() {
  try {
    await skillsApi.setupSandbox()
    await load()
  } catch (err) {
    ElMessage.error(errorMessage(err))
  }
}

// ---------------- 添加 ----------------
const add = reactive({ open: false, url: '', busy: false })
const fileInput = ref<HTMLInputElement | null>(null)

async function stageGithub() {
  add.busy = true
  try {
    const sk = await skillsApi.stageGithub(add.url.trim())
    add.open = false
    add.url = ''
    await load()
    await openDetail(sk.id)
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    add.busy = false
  }
}

async function onUpload(e: Event) {
  const f = (e.target as HTMLInputElement).files?.[0]
  if (!f) return
  add.busy = true
  try {
    const b64 = await new Promise<string>((resolve, reject) => {
      const r = new FileReader()
      r.onload = () => resolve(String(r.result).split(',', 2)[1] ?? '')
      r.onerror = () => reject(r.error)
      r.readAsDataURL(f)
    })
    const sk = await skillsApi.stageUpload(f.name, b64)
    add.open = false
    await load()
    await openDetail(sk.id)
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    add.busy = false
    if (fileInput.value) fileInput.value.value = ''
  }
}

// ---------------- AI 安装申请 ----------------
const requests = ref<SkillRequest[]>([])
const aiRequests = ref(true)
const pendingRequests = computed(() => requests.value.filter((r) => r.status === 'pending'))

async function loadRequests() {
  try {
    const r = await requestsApi.list()
    requests.value = r.requests
    aiRequests.value = r.settings.ai_requests
  } catch (err) {
    ElMessage.error(errorMessage(err))
  }
}
onMounted(loadRequests)

async function setAiRequests(v: boolean) {
  try {
    await requestsApi.setAiRequests(v)
    aiRequests.value = v
  } catch (err) {
    ElMessage.error(errorMessage(err))
  }
}

async function rejectRequest(r: SkillRequest) {
  try {
    await requestsApi.reject(r.id)
    await Promise.all([load(), loadRequests()])
  } catch (err) {
    ElMessage.error(errorMessage(err))
  }
}

async function onStaged(id: number) {
  await load()
  await openDetail(id)
}

// ---------------- 审阅 / 详情 ----------------
const detail = ref<Skill | null>(null)
const drawer = ref(false)
const policy = reactive({
  allow_domains: [] as string[],
  commands: [] as string[],
  user_only: [] as string[],
  setup: '',
})
const calls = ref<SkillCall[]>([])
const viewer = reactive({ open: false, path: '', content: '' })

async function openDetail(id: number) {
  try {
    detail.value = await skillsApi.get(id)
    Object.assign(policy, {
      allow_domains: [...detail.value.allow_domains],
      commands: [...detail.value.commands],
      user_only: [...detail.value.user_only],
      setup: detail.value.setup_command,
    })
    calls.value = detail.value.status === 'pending' ? [] : await skillsApi.calls(id)
    drawer.value = true
  } catch (err) {
    ElMessage.error(errorMessage(err))
  }
}

async function viewFile(path: string) {
  if (!detail.value) return
  try {
    const r = await skillsApi.file(detail.value.id, path)
    Object.assign(viewer, { open: true, path, content: r.content })
  } catch (err) {
    ElMessage.error(errorMessage(err))
  }
}

async function savePolicy(): Promise<boolean> {
  if (!detail.value) return false
  try {
    detail.value = await skillsApi.update(detail.value.id, {
      allow_domains: policy.allow_domains,
      commands: policy.commands,
      user_only: policy.user_only,
      setup_command: policy.setup,
    })
    ElMessage.success('已保存')
    await load()
    return true
  } catch (err) {
    ElMessage.error(errorMessage(err))
    return false
  }
}

async function approve() {
  if (!detail.value) return
  try {
    await ElMessageBox.confirm(
      `批准安装「${detail.value.display_name || detail.value.name}」？` +
        (policy.setup ? `将在隔离环境中运行安装命令：${policy.setup}` : '') +
        `；之后 AI 可以调用：${policy.commands.join('、') || '（无命令，只读说明）'}，可访问：${policy.allow_domains.join('、') || '（不联网）'}。`,
      '批准安装',
      { type: 'warning', confirmButtonText: '批准安装' },
    )
  } catch {
    return
  }
  if (!(await savePolicy())) return
  try {
    await skillsApi.approve(detail.value.id)
    ElMessage.success('已批准，正在隔离环境中安装')
    drawer.value = false
    await load()
  } catch (err) {
    ElMessage.error(errorMessage(err))
  }
}

async function reject() {
  if (!detail.value) return
  try {
    await skillsApi.reject(detail.value.id)
    drawer.value = false
    await load()
  } catch (err) {
    ElMessage.error(errorMessage(err))
  }
}

async function toggle(sk: Skill, enabled: boolean) {
  try {
    await skillsApi.update(sk.id, { enabled })
    await load()
  } catch (err) {
    ElMessage.error(errorMessage(err))
  }
}

async function remove(sk: Skill) {
  try {
    await ElMessageBox.confirm(
      `卸载「${sk.display_name || sk.name}」？包文件与它的工作目录（含鉴权信息）会被删除。`,
      '卸载技能',
      {
        type: 'warning',
      },
    )
    await skillsApi.remove(sk.id)
    await load()
  } catch (err) {
    if (err !== 'cancel') ElMessage.error(errorMessage(err))
  }
}

// ---------------- 技能终端 ----------------
const term = reactive({
  open: false,
  skill: null as Skill | null,
  line: '',
  busy: false,
  history: [] as { line: string; result: RunResult }[],
})
const termLog = ref<HTMLElement>()

function openTerminal(sk: Skill) {
  Object.assign(term, { open: true, skill: sk, line: sk.commands[0] ? `${sk.commands[0]} --help` : '', history: [] })
}

async function runTerminal() {
  if (!term.skill || !term.line.trim()) return
  term.busy = true
  const line = term.line.trim()
  try {
    const result = await skillsApi.run(term.skill.id, splitArgs(line))
    term.history.push({ line, result })
    await nextTick()
    termLog.value?.scrollTo({ top: termLog.value.scrollHeight })
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    term.busy = false
  }
}

// 关闭终端即结束会话容器（下次打开是全新的环境；技能保存在工作目录里的文件不受影响）
function closeTerminal() {
  if (term.skill) skillsApi.endSession(term.skill.id).catch(() => undefined)
}

const sandboxReady = computed(() => !!sandbox.value?.ready)
const fmtSize = (n: number) =>
  n > 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`
</script>

<template>
  <div v-loading="loading">
    <p class="muted">
      技能是外部的「说明 + 脚本 / 程序」包（与 Claude、WorkBuddy 等使用的 SKILL.md 格式相同）。安装后，AI
      复核员、研究员可以按需阅读说明、调用技能命令查询资料。<b>每次安装都需要你批准</b>；技能命令在一次性的隔离容器中运行，
      看不到平台的数据库、密钥和配置，只能访问你允许的域名；涉及鉴权的子命令只能由你本人在「技能终端」中执行，AI
      看不到密钥。
    </p>

    <el-alert
      v-if="sandbox"
      :type="sandboxReady ? 'success' : 'warning'"
      :closable="false"
      class="block"
      :title="sandbox.message"
    >
      <template v-if="!sandboxReady">
        <span v-if="sandbox.docker">
          首次使用需要初始化：构建运行镜像（基于 Debian，约几分钟）、创建没有外网出口的内部网络、启动出网过滤代理。
        </span>
        <el-button
          v-if="sandbox.docker"
          size="small"
          type="primary"
          :loading="sandboxTask?.running"
          class="ml"
          @click="setupSandbox"
          >初始化沙箱</el-button
        >
        <div v-if="sandboxTask && !sandboxTask.running" class="muted pre">{{ sandboxTask.message }}</div>
      </template>
    </el-alert>

    <el-alert v-if="pendingRequests.length" type="warning" :closable="false" class="block">
      <template #title>AI 申请安装 {{ pendingRequests.length }} 个技能，等待你审批</template>
      <div v-for="r in pendingRequests" :key="r.id" class="req">
        <b>{{ r.skill_name || r.source_url }}</b
        >（{{ r.requester }}，{{ formatDateTime(r.created_at) }}）：{{ r.reason }}
        <el-button v-if="r.skill_id" size="small" text type="primary" @click="openDetail(r.skill_id)">审阅</el-button>
        <el-button size="small" text type="danger" @click="rejectRequest(r)">拒绝</el-button>
      </div>
    </el-alert>

    <div class="toolbar">
      <el-switch
        :model-value="aiRequests"
        active-text="允许 AI 搜索技能市场并提交安装申请"
        @change="(v: string | number | boolean) => setAiRequests(!!v)"
      />
      <span class="muted">（AI 只能申请，安装始终需要你批准；申请会推送到微信）</span>
    </div>

    <el-card shadow="never" class="block">
      <template #header>技能市场</template>
      <SkillMarketPanel @staged="onStaged" />
    </el-card>

    <div class="toolbar">
      <el-button type="primary" @click="add.open = true">添加技能</el-button>
      <el-button @click="load">刷新</el-button>
    </div>

    <el-table :data="skills" size="small" empty-text="还没有技能">
      <el-table-column label="技能" min-width="220">
        <template #default="{ row }">
          <div class="name">{{ row.display_name || row.name }}</div>
          <div class="muted">{{ row.name }} {{ row.version }}</div>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="110">
        <template #default="{ row }">
          <el-tag :type="STATUS[row.status]?.type" size="small">{{ STATUS[row.status]?.label ?? row.status }}</el-tag>
          <el-tag v-if="row.request_id" size="small" type="info" class="ml">AI 申请</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="AI 可用" width="80">
        <template #default="{ row }">
          <el-switch
            :model-value="row.enabled"
            :disabled="row.status !== 'installed'"
            size="small"
            @change="(v: string | number | boolean) => toggle(row, !!v)"
          />
        </template>
      </el-table-column>
      <el-table-column label="命令 / 域名" min-width="220">
        <template #default="{ row }">
          <div>{{ row.commands.join('、') || '—' }}</div>
          <div class="muted">{{ row.allow_domains.join('、') || '不联网' }}</div>
        </template>
      </el-table-column>
      <el-table-column label="包含" width="150">
        <template #default="{ row }">
          {{ row.summary.files }} 文件 · {{ row.summary.scripts }} 脚本 · {{ row.summary.binaries }} 程序
        </template>
      </el-table-column>
      <el-table-column width="200">
        <template #default="{ row }">
          <el-button size="small" text type="primary" @click="openDetail(row.id)">{{
            row.status === 'pending' ? '审阅' : '详情'
          }}</el-button>
          <el-button v-if="row.status === 'installed'" size="small" text @click="openTerminal(row)">终端</el-button>
          <el-button size="small" text type="danger" @click="remove(row)">卸载</el-button>
        </template>
      </el-table-column>
    </el-table>

    <!-- 添加 -->
    <el-dialog v-model="add.open" title="添加技能" width="560px">
      <p class="muted">只下载、解压并生成审阅清单，<b>不会执行任何文件</b>；确认后才会安装。</p>
      <el-form label-width="90px">
        <el-form-item label="GitHub 地址">
          <el-input v-model="add.url" placeholder="https://github.com/owner/repo/tree/main/skills/xxx" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="add.busy" :disabled="!add.url.trim()" @click="stageGithub"
            >获取并审阅</el-button
          >
        </el-form-item>
        <el-form-item label="或上传 zip">
          <input ref="fileInput" type="file" accept=".zip" @change="onUpload" />
        </el-form-item>
      </el-form>
    </el-dialog>

    <!-- 审阅 / 详情 -->
    <el-drawer v-model="drawer" :title="detail ? detail.display_name || detail.name : ''" size="720px">
      <template v-if="detail">
        <p>{{ detail.description }}</p>
        <p class="muted">
          来源：{{ detail.source_url }}；包摘要 {{ detail.package_sha256.slice(0, 16) }}…； 状态：{{
            STATUS[detail.status]?.label ?? detail.status
          }}
        </p>
        <el-alert
          v-for="w in detail.warnings"
          :key="w"
          type="warning"
          :closable="false"
          :title="w"
          class="block small"
        />

        <h4>文件（点击查看内容）</h4>
        <el-table
          :data="detail.manifest?.files ?? []"
          size="small"
          max-height="240"
          @row-click="(r: { path: string }) => viewFile(r.path)"
        >
          <el-table-column prop="path" label="路径" min-width="260" />
          <el-table-column label="类型" width="70">
            <template #default="{ row }">
              <el-tag
                size="small"
                :type="row.kind === 'binary' ? 'danger' : row.kind === 'script' ? 'warning' : 'info'"
                >{{ KIND[row.kind] }}</el-tag
              >
            </template>
          </el-table-column>
          <el-table-column label="大小" width="80">
            <template #default="{ row }">{{ fmtSize(row.size) }}</template>
          </el-table-column>
        </el-table>

        <h4>运行策略</h4>
        <el-form label-width="120px" size="small">
          <el-form-item label="允许访问的域名">
            <el-select
              v-model="policy.allow_domains"
              multiple
              filterable
              allow-create
              default-first-option
              style="width: 100%"
            >
              <el-option v-for="d in detail.manifest?.domains ?? []" :key="d" :label="d" :value="d" />
            </el-select>
            <p class="muted help">
              包含子域名（填 qq.com 即允许 x.qq.com）。为空表示完全不联网。说明中出现过的域名可从下拉中选择。
            </p>
          </el-form-item>
          <el-form-item label="AI 可调用的命令">
            <el-select
              v-model="policy.commands"
              multiple
              filterable
              allow-create
              default-first-option
              style="width: 100%"
            />
            <p class="muted help">命令入口名（如 westock），不经过 shell 直接执行；为空表示只读说明。</p>
          </el-form-item>
          <el-form-item label="仅限本人的子命令">
            <el-select
              v-model="policy.user_only"
              multiple
              filterable
              allow-create
              default-first-option
              style="width: 100%"
            />
            <p class="muted help">如 auth（鉴权）：只能你在技能终端里执行，AI 调用会被拒绝。</p>
          </el-form-item>
          <el-form-item label="安装命令">
            <el-input v-model="policy.setup" placeholder="如 bash scripts/setup.sh（在隔离环境中执行；可留空）" />
          </el-form-item>
          <el-form-item>
            <template v-if="detail.status === 'pending' || detail.status === 'failed'">
              <el-button type="primary" :disabled="!sandboxReady" @click="approve">批准安装</el-button>
              <el-button @click="savePolicy">保存</el-button>
              <el-button type="danger" text @click="reject">拒绝</el-button>
              <span v-if="!sandboxReady" class="muted ml">沙箱未就绪，请先初始化</span>
            </template>
            <el-button v-else @click="savePolicy">保存设置</el-button>
          </el-form-item>
        </el-form>

        <template v-if="detail.install_log">
          <h4>安装记录</h4>
          <pre class="log">{{ detail.install_log }}</pre>
        </template>

        <template v-if="calls.length">
          <h4>调用记录</h4>
          <el-table :data="calls" size="small" max-height="300">
            <el-table-column label="时间" width="140">
              <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
            </el-table-column>
            <el-table-column prop="caller" label="调用方" width="90" />
            <el-table-column label="命令" min-width="200" show-overflow-tooltip>
              <template #default="{ row }">{{ row.argv.join(' ') }}</template>
            </el-table-column>
            <el-table-column label="结果" width="120">
              <template #default="{ row }">
                {{ row.exit_code === null ? '超时' : `退出 ${row.exit_code}` }} · {{ row.seconds }}s
                <el-tooltip v-if="row.blocked.length" :content="`被拦截：${row.blocked.join('、')}`">
                  <el-tag size="small" type="danger">拦截</el-tag>
                </el-tooltip>
              </template>
            </el-table-column>
          </el-table>
        </template>
      </template>
    </el-drawer>

    <el-dialog v-model="viewer.open" :title="viewer.path" width="820px">
      <pre class="log tall">{{ viewer.content }}</pre>
    </el-dialog>

    <!-- 终端 -->
    <el-dialog
      v-model="term.open"
      :title="`技能终端：${term.skill?.display_name || term.skill?.name || ''}`"
      width="820px"
      @closed="closeTerminal"
    >
      <p class="muted">
        在隔离环境中执行技能命令（不经过 shell）。用于完成技能自己的鉴权（例如
        <code>westock auth status</code> 后按提示在浏览器中授权，再执行
        <code>westock auth fetch</code>），或手动试用。密钥只保存在该技能的隔离工作目录里，AI 无法读取。
      </p>
      <p class="muted">
        同一个终端窗口里的命令在同一个隔离环境中依次执行；关闭窗口或闲置 15 分钟后环境销毁。鉴权要在同一个窗口里做完。
      </p>
      <div v-if="term.history.length" ref="termLog" class="term-log">
        <div v-for="(h, i) in term.history" :key="i" class="term-entry">
          <div class="term-cmd">
            <code>$ {{ h.line }}</code>
            <span class="muted">
              {{ h.result.timed_out ? '超时' : `退出码 ${h.result.exit_code}` }}，{{ h.result.seconds }} 秒
              <span v-if="h.result.blocked.length"
                >；被拦截的域名：{{ h.result.blocked.join('、') }}（如需访问请在详情中加入允许域名）</span
              >
            </span>
          </div>
          <pre class="log">{{ h.result.output }}</pre>
        </div>
      </div>
      <div class="toolbar">
        <el-input v-model="term.line" placeholder="westock auth status" @keyup.enter="runTerminal" />
        <el-button type="primary" :loading="term.busy" @click="runTerminal">执行</el-button>
      </div>
    </el-dialog>
  </div>
</template>

<style scoped>
.term-log {
  max-height: 52vh;
  overflow-y: auto;
  margin-bottom: 10px;
}

.term-entry + .term-entry {
  margin-top: 10px;
}

.term-cmd {
  display: flex;
  gap: 12px;
  align-items: baseline;
  flex-wrap: wrap;
}

.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 1.8;
}

.help {
  margin: 2px 0 0;
}

.toolbar {
  display: flex;
  gap: 8px;
  margin: 8px 0;
}

.block {
  margin-bottom: 8px;
}

.small :deep(.el-alert__title) {
  font-size: 12px;
}

.ml {
  margin-left: 6px;
}

.name {
  font-weight: 600;
}

.req {
  line-height: 2;
}

.pre {
  white-space: pre-wrap;
}

h4 {
  margin: 14px 0 6px;
}

.log {
  white-space: pre-wrap;
  word-break: break-all;
  font-size: 12px;
  background: var(--el-fill-color-light);
  padding: 8px;
  border-radius: 4px;
  max-height: 260px;
  overflow: auto;
  margin: 0;
}

.tall {
  max-height: 480px;
}
</style>
