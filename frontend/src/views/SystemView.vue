<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { errorMessage } from '@/api/http'
import { fetchJobs, type JobInfo } from '@/api/system'
import { systemApi, type DataSourceStatus, type EventItem } from '@/api/arena'
import DataQualityPanel from '@/components/DataQualityPanel.vue'
import { useSystemStore } from '@/stores/system'
import { formatDateTime, marketLabel } from '@/utils/format'

const system = useSystemStore()
const jobs = ref<JobInfo[]>([])
const jobsError = ref<string | null>(null)
const sources = ref<DataSourceStatus[]>([])
const events = ref<EventItem[]>([])
const SOURCE_LABELS: Record<string, string> = {
  'CN.calendar': 'A股交易日历',
  'CN.daily_bars': 'A股日线行情',
  'CN.corporate_actions': 'A股分红送转',
}

async function refreshAll() {
  await system.refresh()
  try {
    ;[jobs.value, sources.value, events.value] = await Promise.all([
      fetchJobs(),
      systemApi.datasources(),
      systemApi.events(50),
    ])
    jobsError.value = null
  } catch (err) {
    jobsError.value = errorMessage(err)
  }
}

onMounted(refreshAll)
</script>

<template>
  <div>
    <div class="title-row">
      <h2 class="sa-page-title">系统状态</h2>
      <el-button :icon="'Refresh'" :loading="system.loading" @click="refreshAll">刷新</el-button>
    </div>

    <el-alert v-if="system.error" type="error" :title="system.error" show-icon :closable="false" />

    <template v-else-if="system.health">
      <el-card shadow="never" header="服务">
        <el-descriptions :column="3" border>
          <el-descriptions-item label="整体状态">
            <el-tag :type="system.health.status === 'ok' ? 'success' : 'warning'">
              {{ system.health.status === 'ok' ? '正常' : '部分异常' }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="版本">
            {{ system.health.app }} {{ system.health.version }}（{{ system.health.env }}）
          </el-descriptions-item>
          <el-descriptions-item label="调度器">
            <el-tag :type="system.health.scheduler_running ? 'success' : 'danger'">
              {{ system.health.scheduler_running ? '运行中' : '未运行' }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="数据库">
            <el-tag :type="system.health.database.ok ? 'success' : 'danger'">
              {{ system.health.database.ok ? '正常' : '异常' }}
            </el-tag>
            <span class="muted">{{ system.health.database.dialect }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="迁移版本">
            {{ system.health.database.revision ?? '-' }}
          </el-descriptions-item>
          <el-descriptions-item label="服务器时间（UTC）">
            {{ formatDateTime(system.health.time_utc, 'UTC') }}
          </el-descriptions-item>
        </el-descriptions>
        <el-alert
          v-if="system.health.database.error"
          class="mt"
          type="error"
          :title="system.health.database.error"
          :closable="false"
        />
      </el-card>

      <el-card shadow="never" header="市场时钟" class="mt">
        <el-row :gutter="16">
          <el-col v-for="m in system.health.markets" :key="m.market" :xs="24" :sm="8">
            <div class="clock-label">{{ marketLabel(m.market) }}（{{ m.timezone }}）</div>
            <div class="clock-time">{{ formatDateTime(m.local_time, m.timezone) }}</div>
          </el-col>
        </el-row>
      </el-card>
    </template>

    <el-card shadow="never" header="数据源" class="mt">
      <el-table :data="sources" empty-text="尚未访问过数据源（赛道启动后出现）">
        <el-table-column label="数据" width="160">
          <template #default="{ row }">{{ SOURCE_LABELS[row.name] ?? row.name }}</template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag
              size="small"
              :type="row.consecutive_failures === 0 ? 'success' : row.consecutive_failures < 3 ? 'warning' : 'danger'"
            >
              {{ row.consecutive_failures === 0 ? '正常' : `连续失败 ${row.consecutive_failures} 次` }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="最近成功（北京时间）" width="190">
          <template #default="{ row }">{{ formatDateTime(row.last_success_at, 'Asia/Shanghai') }}</template>
        </el-table-column>
        <el-table-column label="最近错误" show-overflow-tooltip>
          <template #default="{ row }">{{ row.last_error ?? '—' }}</template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-card shadow="never" header="数据质量" class="mt">
      <DataQualityPanel />
    </el-card>

    <el-card shadow="never" header="调度任务" class="mt">
      <el-alert v-if="jobsError" type="error" :title="jobsError" :closable="false" />
      <el-table v-else :data="jobs" empty-text="暂无调度任务（赛道启动后出现）">
        <el-table-column prop="name" label="任务" />
        <el-table-column prop="trigger" label="触发规则" />
        <el-table-column label="下次运行（北京时间）">
          <template #default="{ row }">{{ formatDateTime(row.next_run_time, 'Asia/Shanghai') }}</template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-card shadow="never" header="最近事件" class="mt">
      <el-table :data="events" empty-text="暂无事件" max-height="420">
        <el-table-column label="时间" width="170">
          <template #default="{ row }">{{ formatDateTime(row.created_at, 'Asia/Shanghai') }}</template>
        </el-table-column>
        <el-table-column label="级别" width="80">
          <template #default="{ row }">
            <el-tag
              size="small"
              :type="row.level === 'error' ? 'danger' : row.level === 'warning' ? 'warning' : 'info'"
            >
              {{ row.level === 'error' ? '错误' : row.level === 'warning' ? '警告' : '信息' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="message" label="内容" />
      </el-table>
    </el-card>
  </div>
</template>

<style scoped>
.title-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.mt {
  margin-top: 16px;
}

.clock-label {
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

.clock-time {
  margin-top: 4px;
  font-size: 18px;
  font-variant-numeric: tabular-nums;
}

.muted {
  margin-left: 8px;
  color: var(--el-text-color-secondary);
}
</style>
