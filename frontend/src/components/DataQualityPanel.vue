<script setup lang="ts">
/** 数据质量：每晚扫描行情异常（假跳变、价格不一致、复权因子异常、长期缺失、未更新），可重新下载或确认后忽略。 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { errorMessage } from '@/api/http'
import { qualityApi, costsApi, type DataIssue, type DataQuality, type IssueLevel } from '@/api/quant'
import { formatDateTime } from '@/utils/format'

const data = ref<DataQuality | null>(null)
const universe = ref('')
const showLevels = ref<IssueLevel[]>(['error', 'warn'])
const showIgnored = ref(false)
const repairing = ref<string | null>(null)
let timer: ReturnType<typeof setInterval> | undefined

const LEVELS: Record<IssueLevel, { label: string; type: 'danger' | 'warning' | 'info' }> = {
  error: { label: '错误', type: 'danger' },
  warn: { label: '提示', type: 'warning' },
  info: { label: '参考', type: 'info' },
}
const KINDS: Record<DataIssue['kind'], string> = {
  jump: '异常跳变',
  ohlc: '价格不一致',
  factor: '复权因子',
  gap: '长期无行情',
  stale: '未更新',
}

async function load() {
  try {
    data.value = await qualityApi.get()
    const names = Object.keys(data.value.universes ?? {})
    if (!names.includes(universe.value)) universe.value = names[0] ?? ''
  } catch (err) {
    ElMessage.error(errorMessage(err))
  }
}

const busy = computed(() => !!(data.value?.task?.running || data.value?.reevaluate_task?.running))
onMounted(async () => {
  await load()
  timer = setInterval(() => busy.value && void load(), 5000)
})
onBeforeUnmount(() => clearInterval(timer))

const current = computed(() => data.value?.universes?.[universe.value])
const rows = computed(() =>
  (current.value?.issues ?? []).filter((x) => showLevels.value.includes(x.level) && (showIgnored.value || !x.ignored)),
)

async function scan() {
  try {
    await qualityApi.scan()
    ElMessage.success('已开始扫描')
    await load()
  } catch (err) {
    ElMessage.error(errorMessage(err))
  }
}

async function reevaluate() {
  try {
    await costsApi.reevaluate()
    ElMessage.success('已开始重新回测所有在用策略')
    await load()
  } catch (err) {
    ElMessage.error(errorMessage(err))
  }
}

async function repair(row: DataIssue) {
  try {
    await ElMessageBox.confirm(
      `重新下载 ${row.symbol} ${row.name} 的全部日线与分红送转数据（覆盖本地缓存），之后在后台重新回测所有在用策略并重新扫描。`,
      '重新下载数据',
      { type: 'warning' },
    )
  } catch {
    return
  }
  repairing.value = row.key
  try {
    const r = await qualityApi.repair(row.symbol)
    ElMessage.success(r.message)
    await load()
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    repairing.value = null
  }
}

async function toggleIgnore(row: DataIssue) {
  try {
    await qualityApi.ignore(row.key, !row.ignored)
    row.ignored = !row.ignored
  } catch (err) {
    ElMessage.error(errorMessage(err))
  }
}
</script>

<template>
  <div>
    <p class="muted">
      每晚数据更新后自动扫描在用股票池的行情，找出可能让回测出现假收益的异常；新发现的「错误」会推送到微信（异常提醒）。
      复权后单日涨跌超过涨跌停限制多半是复权或份额折算出错，可以重新下载该标的数据；确认是真实情况（如恢复上市首日）的可以忽略。
    </p>
    <div class="toolbar">
      <el-select v-if="data?.universes" v-model="universe" size="small" style="width: 200px">
        <el-option v-for="(_, name) in data.universes" :key="name" :label="name" :value="name" />
      </el-select>
      <el-checkbox-group v-model="showLevels" size="small">
        <el-checkbox-button v-for="(lv, key) in LEVELS" :key="key" :value="key">
          {{ lv.label }} {{ current?.counts?.[key] ?? 0 }}
        </el-checkbox-button>
      </el-checkbox-group>
      <el-checkbox v-model="showIgnored" size="small">显示已忽略</el-checkbox>
      <span class="spacer" />
      <el-button size="small" :loading="data?.task?.running" @click="scan">重新扫描</el-button>
      <el-button size="small" :loading="data?.reevaluate_task?.running" @click="reevaluate">重新回测全部策略</el-button>
    </div>
    <p v-if="data?.checked_at" class="muted">
      最近扫描：{{ formatDateTime(data.checked_at) }}
      <template v-if="current?.symbols">
        ；{{ current.symbols }} 个标的、{{ current.days }} 个交易日（{{ current.start }} ～ {{ current.end }}）
      </template>
      <template v-if="data.task">；{{ data.task.message }}</template>
      <template v-if="data.reevaluate_task?.running">；重新回测：{{ data.reevaluate_task.message }}</template>
    </p>
    <el-alert v-if="current?.error" type="error" :closable="false" :title="current.error" />
    <el-table :data="rows" size="small" max-height="420" :empty-text="data?.checked_at ? '没有异常' : '还没有扫描过'">
      <el-table-column label="级别" width="70">
        <template #default="{ row }">
          <el-tag size="small" :type="LEVELS[row.level as IssueLevel].type">{{
            LEVELS[row.level as IssueLevel].label
          }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="标的" width="150">
        <template #default="{ row }">{{ row.symbol }} {{ row.name }}</template>
      </el-table-column>
      <el-table-column prop="date" label="日期" width="100" />
      <el-table-column label="类型" width="100">
        <template #default="{ row }">{{ KINDS[row.kind as DataIssue['kind']] ?? row.kind }}</template>
      </el-table-column>
      <el-table-column prop="detail" label="说明" min-width="300" show-overflow-tooltip />
      <el-table-column width="150">
        <template #default="{ row }">
          <el-button size="small" text type="primary" :loading="repairing === row.key" @click="repair(row)"
            >重新下载</el-button
          >
          <el-button size="small" text @click="toggleIgnore(row)">{{ row.ignored ? '取消忽略' : '忽略' }}</el-button>
        </template>
      </el-table-column>
    </el-table>
  </div>
</template>

<style scoped>
.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 1.8;
  margin: 0 0 8px;
}

.toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}

.spacer {
  flex: 1;
}
</style>
