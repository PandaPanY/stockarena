<script setup lang="ts">
/**
 * 消息推送：把每日调仓提醒、异常提醒推送到微信。
 * 密钥（SendKey / token / 机器人地址）只在这里填写，加密保存在服务器，接口只返回掩码。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { errorMessage } from '@/api/http'
import { notifyApi, type NotificationItem, type NotifyChannel, type NotifyEvent, type NotifyKind } from '@/api/notify'
import { formatDateTime } from '@/utils/format'

const channels = ref<NotifyChannel[]>([])
const history = ref<NotificationItem[]>([])
const loading = ref(false)

const KINDS: Record<NotifyKind, { name: string; label: string; help: string; placeholder: string }> = {
  serverchan: {
    name: 'Server 酱',
    label: 'SendKey',
    help: '在 sct.ftqq.com 用微信扫码登录，关注「方糖」服务号，在 SendKey 页面复制（SCT 开头）。免费版每天 5 条。',
    placeholder: 'SCT 开头',
  },
  pushplus: {
    name: 'PushPlus',
    label: 'token',
    help: '在 www.pushplus.plus 用微信扫码登录并关注公众号，在「一对一推送」页面复制 token（32 位）。',
    placeholder: '32 位 token',
  },
  wecom_bot: {
    name: '企业微信群机器人',
    label: 'Webhook 地址',
    help: '在企业微信群里添加「群机器人」，复制 Webhook 地址。微信里可以通过企业微信接收，没有条数限制。',
    placeholder: 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=…',
  },
}
const EVENTS: Record<NotifyEvent, string> = {
  orders: '调仓提醒',
  errors: '异常提醒',
  health: '健康度与可实盘',
  skills: '技能申请',
  watch: '自选股提醒',
}

async function load() {
  loading.value = true
  try {
    ;[channels.value, history.value] = await Promise.all([notifyApi.channels(), notifyApi.history()])
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    loading.value = false
  }
}
onMounted(load)

const dialog = reactive({
  open: false,
  id: null as number | null,
  name: '我的微信',
  kind: 'serverchan' as NotifyKind,
  secret: '',
  enabled: true,
  events: ['orders', 'errors', 'health', 'skills', 'watch'] as NotifyEvent[],
  masked: '',
})
const kindInfo = computed(() => KINDS[dialog.kind])

function openCreate() {
  Object.assign(dialog, {
    open: true,
    id: null,
    name: '我的微信',
    kind: 'serverchan',
    secret: '',
    enabled: true,
    events: ['orders', 'errors', 'health', 'skills', 'watch'],
    masked: '',
  })
}

function openEdit(c: NotifyChannel) {
  Object.assign(dialog, {
    open: true,
    id: c.id,
    name: c.name,
    kind: c.kind,
    secret: '',
    enabled: c.enabled,
    events: [...c.events],
    masked: c.secret_masked,
  })
}

async function save() {
  const body = {
    name: dialog.name,
    kind: dialog.kind,
    enabled: dialog.enabled,
    events: dialog.events,
    ...(dialog.secret ? { secret: dialog.secret } : {}),
  }
  try {
    if (dialog.id) await notifyApi.update(dialog.id, body)
    else await notifyApi.create(body)
    dialog.open = false
    dialog.secret = ''
    ElMessage.success('已保存，可以点「发送测试」确认能收到')
    load()
  } catch (err) {
    ElMessage.error(errorMessage(err))
  }
}

const testing = ref<number | null>(null)
async function test(c: NotifyChannel) {
  testing.value = c.id
  try {
    await notifyApi.test(c.id)
    ElMessage.success('测试消息已发出，请在微信中查看')
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    testing.value = null
  }
}

async function remove(c: NotifyChannel) {
  try {
    await ElMessageBox.confirm(`删除推送通道「${c.name}」？（发送记录一并删除）`, '删除推送通道', { type: 'warning' })
    await notifyApi.remove(c.id)
    load()
  } catch (err) {
    if (err !== 'cancel') ElMessage.error(errorMessage(err))
  }
}

const checking = ref(false)
async function checkNow() {
  checking.value = true
  try {
    const r = await notifyApi.check()
    ElMessage.success(
      r.results.length ? `已推送 ${r.results.length} 条` : '没有需要推送的调仓（已推送过的不会重复发送）',
    )
    load()
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    checking.value = false
  }
}
const channelName = (id: number) => channels.value.find((c) => c.id === id)?.name ?? id
</script>

<template>
  <div v-loading="loading">
    <p class="muted">
      每个交易日收盘后（16:20、17:00、18:40
      检查），实时跟踪中的组合或策略生成了下一交易日的订单时，把调仓清单推送到微信；同一天只推送一次，没有调仓的日子不打扰。
      21:00 仍没有完成当天决策或每晚数据扫描发现行情错误时推送异常提醒；跟踪表现明显差于回测（健康度「提醒」）、
      「可实盘」结论变化时推送健康度提醒。密钥加密保存在服务器上，只在这里填写，页面只显示末几位。
    </p>
    <div class="toolbar">
      <el-button type="primary" @click="openCreate">添加推送通道</el-button>
      <el-button :loading="checking" :disabled="!channels.length" @click="checkNow">立即检查调仓提醒</el-button>
    </div>
    <el-table :data="channels" size="small" empty-text="还没有推送通道">
      <el-table-column prop="name" label="名称" min-width="120" />
      <el-table-column prop="kind_name" label="方式" width="140" />
      <el-table-column prop="secret_masked" label="密钥" width="140" />
      <el-table-column label="推送内容" width="170">
        <template #default="{ row }">{{ row.events.map((e: NotifyEvent) => EVENTS[e]).join('、') || '-' }}</template>
      </el-table-column>
      <el-table-column label="状态" min-width="200">
        <template #default="{ row }">
          <el-tag v-if="!row.enabled" type="info" size="small">已停用</el-tag>
          <el-tag v-else-if="row.last_status === 'ok'" type="success" size="small">正常</el-tag>
          <el-tooltip v-else-if="row.last_status === 'failed'" :content="row.last_error">
            <el-tag type="danger" size="small">最近一次失败</el-tag>
          </el-tooltip>
          <span v-if="row.last_sent_at" class="muted"> {{ formatDateTime(row.last_sent_at) }}</span>
        </template>
      </el-table-column>
      <el-table-column width="200">
        <template #default="{ row }">
          <el-button size="small" text type="primary" :loading="testing === row.id" @click="test(row)"
            >发送测试</el-button
          >
          <el-button size="small" text @click="openEdit(row)">编辑</el-button>
          <el-button size="small" text type="danger" @click="remove(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <h4>最近推送</h4>
    <el-table :data="history" size="small" max-height="360" empty-text="还没有推送记录">
      <el-table-column label="时间" width="150">
        <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="通道" width="110">
        <template #default="{ row }">{{ channelName(row.channel_id) }}</template>
      </el-table-column>
      <el-table-column label="标题" min-width="260">
        <template #default="{ row }">
          <el-popover trigger="hover" width="420" placement="left">
            <template #reference
              ><span>{{ row.title }}</span></template
            >
            <pre class="body">{{ row.body }}</pre>
          </el-popover>
        </template>
      </el-table-column>
      <el-table-column label="结果" width="160">
        <template #default="{ row }">
          <el-tag v-if="row.status === 'ok'" type="success" size="small">已送达</el-tag>
          <el-tooltip v-else :content="row.error"
            ><el-tag type="danger" size="small">失败（会重试）</el-tag></el-tooltip
          >
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="dialog.open" :title="dialog.id ? '编辑推送通道' : '添加推送通道'" width="560px">
      <el-form label-width="100px">
        <el-form-item label="名称"><el-input v-model="dialog.name" /></el-form-item>
        <el-form-item label="推送方式">
          <el-radio-group v-model="dialog.kind">
            <el-radio-button v-for="(k, key) in KINDS" :key="key" :value="key">{{ k.name }}</el-radio-button>
          </el-radio-group>
          <p class="muted help">{{ kindInfo.help }}</p>
        </el-form-item>
        <el-form-item :label="kindInfo.label">
          <el-input
            v-model="dialog.secret"
            type="password"
            show-password
            autocomplete="off"
            :placeholder="dialog.id ? `已保存（${dialog.masked}），留空表示不变` : kindInfo.placeholder"
          />
        </el-form-item>
        <el-form-item label="推送内容">
          <el-checkbox-group v-model="dialog.events">
            <el-checkbox v-for="(label, key) in EVENTS" :key="key" :value="key">{{ label }}</el-checkbox>
          </el-checkbox-group>
        </el-form-item>
        <el-form-item label="启用"><el-switch v-model="dialog.enabled" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialog.open = false">取消</el-button>
        <el-button type="primary" :disabled="!dialog.id && !dialog.secret" @click="save">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 1.8;
}

.help {
  margin: 4px 0 0;
}

.toolbar {
  display: flex;
  gap: 8px;
  margin-bottom: 8px;
}

h4 {
  margin: 16px 0 8px;
}

.body {
  white-space: pre-wrap;
  font-size: 12px;
  margin: 0;
  max-height: 360px;
  overflow: auto;
}
</style>
