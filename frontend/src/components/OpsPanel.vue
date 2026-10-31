<script setup lang="ts">
/** 运行状况：服务停机、定时任务、每日完整性检查、数据库备份与磁盘、模型与推送通道（管理员）。 */
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { errorMessage } from '@/api/http'
import { opsApi, type OpsStatus } from '@/api/system'
import { formatDateTime } from '@/utils/format'

const st = ref<OpsStatus | null>(null)
const loading = ref(false)
const backingUp = ref(false)
const checking = ref(false)

const gb = (n: number) => `${(n / 2 ** 30).toFixed(1)} GB`
const mb = (n: number) => `${(n / 2 ** 20).toFixed(0)} MB`
const bj = (t: string | null | undefined) => (t ? formatDateTime(t, 'Asia/Shanghai') : '-')
const dur = (m: number) => (m >= 60 ? `${Math.floor(m / 60)} 小时 ${m % 60} 分` : `${m} 分钟`)

async function load() {
  loading.value = true
  try {
    st.value = await opsApi.status()
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    loading.value = false
  }
}
onMounted(load)
defineExpose({ load })

async function backupNow() {
  backingUp.value = true
  try {
    await opsApi.backup()
    ElMessage.success('已备份')
    await load()
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    backingUp.value = false
  }
}

async function checkNow() {
  checking.value = true
  try {
    const r = await opsApi.check()
    if (r.ok) ElMessage.success(`${r.day} 的数据完整`)
    else ElMessage.warning(`${r.day} 有 ${r.problems.length} 项需要注意`)
    await load()
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    checking.value = false
  }
}

const badJobs = computed(() => (st.value?.jobs ?? []).filter((j) => j.status && j.status !== 'ok'))
const downModels = computed(() => (st.value?.models ?? []).filter((m) => (m.fails ?? 0) > 0))
const diskLow = computed(() => !!st.value && (st.value.disk.free_share < 0.05 || st.value.disk.free < 10 * 2 ** 30))
</script>

<template>
  <div v-loading="loading">
    <template v-if="st">
      <div class="grid">
        <div class="item">
          <div class="label">
            数据完整性
            <el-button link type="primary" size="small" :loading="checking" @click="checkNow">立即检查</el-button>
          </div>
          <template v-if="st.integrity">
            <el-tag :type="st.integrity.ok ? 'success' : 'warning'" size="small">
              {{ st.integrity.ok ? '完整' : `${st.integrity.problems.length} 项需要注意` }}
            </el-tag>
            <span class="muted">{{ st.integrity.day }}（检查于 {{ bj(st.integrity.checked_at) }}）</span>
            <ul v-if="!st.integrity.ok" class="problems">
              <li v-for="p in st.integrity.problems" :key="p">{{ p }}</li>
            </ul>
          </template>
          <span v-else class="muted">还没有检查过（每个交易日 23:50 自动检查）</span>
        </div>

        <div class="item">
          <div class="label">最近一次停机</div>
          <template v-if="st.downtime">
            <el-tag :type="st.downtime.missed.length ? 'warning' : 'info'" size="small">
              {{ dur(st.downtime.minutes)
              }}{{ st.downtime.missed.length ? `，错过 ${st.downtime.missed.length} 次任务` : '' }}
            </el-tag>
            <span class="muted">{{ bj(st.downtime.from) }} ～ {{ bj(st.downtime.to) }}</span>
            <div v-if="st.downtime.missed.length" class="muted small">
              {{
                st.downtime.missed
                  .slice(0, 6)
                  .map((m) => `${bj(m.at)} ${m.name}`)
                  .join('；')
              }}{{ st.downtime.missed.length > 6 ? '……' : '' }}
            </div>
          </template>
          <span v-else class="muted">没有记录</span>
        </div>

        <div class="item">
          <div class="label">
            数据库备份
            <el-button link type="primary" size="small" :loading="backingUp" @click="backupNow">立即备份</el-button>
          </div>
          <template v-if="st.backup.last">
            <el-tag :type="st.backup.last_error ? 'danger' : 'success'" size="small">
              {{ st.backup.last_error ? '最近一次失败' : '正常' }}
            </el-tag>
            <span class="muted"
              >最近成功 {{ bj(st.backup.last.at) }}，{{ mb(st.backup.last.size) }}，保留 {{ st.backup.keep }} 份</span
            >
          </template>
          <span v-else class="muted">还没有备份（每天 02:30 自动备份）</span>
          <div v-if="st.backup.last_error" class="err small">{{ st.backup.last_error }}</div>
          <div class="muted small">
            异地副本：{{ st.backup.extra_dir ?? '未配置' }}
            <template v-if="!st.backup.extra_dir"
              >（在 config.yaml 设置 <code>backup_dir</code>，指向另一块硬盘或 NAS，见部署指南）</template
            >
            <span v-if="st.backup.last?.extra_error" class="err">{{ st.backup.last.extra_error }}</span>
          </div>
        </div>

        <div class="item">
          <div class="label">磁盘</div>
          <el-tag :type="diskLow ? 'danger' : 'success'" size="small">
            剩余 {{ gb(st.disk.free) }}（{{ (st.disk.free_share * 100).toFixed(0) }}%）
          </el-tag>
          <span class="muted">共 {{ gb(st.disk.total) }}；数据目录 {{ st.disk.path }}</span>
        </div>

        <div class="item">
          <div class="label">模型</div>
          <template v-if="downModels.length">
            <div v-for="m in downModels" :key="m.id">
              <el-tag type="danger" size="small">{{ m.name }} 连续失败 {{ m.fails }} 次</el-tag>
              <span class="muted small">{{ m.last_error }}</span>
            </div>
          </template>
          <span v-else class="muted">{{ st.models.length ? '全部正常' : '没有配置模型' }}</span>
        </div>

        <div class="item">
          <div class="label">推送</div>
          <template v-if="st.failed_sends.length">
            <el-tag type="danger" size="small">最近 7 天 {{ st.failed_sends.length }} 次发送失败</el-tag>
            <div class="muted small">{{ st.failed_sends[0].title }}：{{ st.failed_sends[0].error }}</div>
          </template>
          <span v-else class="muted">最近 7 天没有发送失败</span>
        </div>
      </div>

      <h4>
        定时任务<span v-if="badJobs.length" class="err">（{{ badJobs.length }} 个最近一次没有成功）</span>
      </h4>
      <el-table :data="st.jobs" size="small" empty-text="没有定时任务">
        <el-table-column prop="name" label="任务" min-width="220" />
        <el-table-column label="上次" width="90">
          <template #default="{ row }">
            <el-tag
              v-if="row.status"
              size="small"
              :type="row.status === 'ok' ? 'success' : row.status === 'error' ? 'danger' : 'warning'"
            >
              {{ row.status === 'ok' ? '完成' : row.status === 'error' ? '出错' : '错过' }}
            </el-tag>
            <span v-else class="muted">-</span>
          </template>
        </el-table-column>
        <el-table-column label="上次完成" width="170">
          <template #default="{ row }">{{ bj(row.finished_at) }}</template>
        </el-table-column>
        <el-table-column label="耗时" width="80" align="right">
          <template #default="{ row }">{{
            row.seconds == null ? '-' : row.seconds >= 60 ? `${Math.round(row.seconds / 60)} 分` : `${row.seconds} 秒`
          }}</template>
        </el-table-column>
        <el-table-column label="下次运行" width="170">
          <template #default="{ row }">{{ bj(row.next_run_time) }}</template>
        </el-table-column>
        <el-table-column prop="error" label="错误" min-width="160" show-overflow-tooltip />
      </el-table>
    </template>
  </div>
</template>

<style scoped>
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(360px, 1fr));
  gap: 12px 24px;
}

.item {
  line-height: 1.8;
}

.label {
  font-weight: 500;
  margin-bottom: 2px;
}

.muted {
  color: var(--el-text-color-secondary);
  margin-left: 6px;
  font-size: 13px;
}

.small {
  font-size: 12px;
  margin-left: 0;
}

.err {
  color: var(--el-color-danger);
  font-size: 12px;
}

.problems {
  margin: 4px 0;
  padding-left: 18px;
  font-size: 13px;
}

h4 {
  margin: 16px 0 8px;
}
</style>
