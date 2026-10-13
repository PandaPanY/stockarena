<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { errorMessage } from '@/api/http'
import { tracksApi, type Track } from '@/api/arena'
import TrackCreateDialog from '@/components/TrackCreateDialog.vue'
import { formatPercent, marketLabel } from '@/utils/format'

const router = useRouter()
const tracks = ref<Track[]>([])
const loading = ref(false)
const showCreate = ref(false)

const STATUS_TYPES = { draft: 'info', running: 'success', paused: 'warning', finished: 'primary' } as const

async function load() {
  loading.value = true
  try {
    tracks.value = await tracksApi.list()
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    loading.value = false
  }
}

function open(id: number) {
  router.push({ name: 'track', params: { id } })
}

onMounted(load)
</script>

<template>
  <div>
    <div class="title-row">
      <h2 class="sa-page-title">赛道</h2>
      <el-button type="primary" icon="Plus" @click="showCreate = true">创建赛道</el-button>
    </div>

    <el-table
      v-loading="loading"
      :data="tracks"
      empty-text="还没有赛道"
      class="clickable"
      @row-click="(row: Track) => open(row.id)"
    >
      <el-table-column prop="name" label="赛道" min-width="180" />
      <el-table-column label="状态" width="150">
        <template #default="{ row }">
          <el-tag :type="STATUS_TYPES[row.status as Track['status']]" size="small">{{ row.status_label }}</el-tag>
          <el-tag v-if="row.mode === 'replay'" size="small" type="warning" effect="plain" class="ml">回放</el-tag>
          <el-icon v-if="row.busy" class="is-loading busy"><Loading /></el-icon>
        </template>
      </el-table-column>
      <el-table-column label="市场" width="70">
        <template #default="{ row }">{{ marketLabel(row.market) }}</template>
      </el-table-column>
      <el-table-column label="股票池 / 基准" min-width="160">
        <template #default="{ row }">{{ row.symbol_count || '—' }} 个标的 · {{ row.benchmark_name }}</template>
      </el-table-column>
      <el-table-column prop="account_count" label="选手" width="70" />
      <el-table-column label="领先者" min-width="200">
        <template #default="{ row }">
          <template v-if="row.leader">
            {{ row.leader.agent_name }}
            <span :class="(row.leader.cumulative_return ?? 0) >= 0 ? 'sa-up' : 'sa-down'">
              {{ formatPercent(row.leader.cumulative_return) }}
            </span>
          </template>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>
      <el-table-column label="开始 / 最近结算" min-width="180">
        <template #default="{ row }">{{ row.start_date ?? '未启动' }} / {{ row.last_settled_date ?? '—' }}</template>
      </el-table-column>
    </el-table>

    <TrackCreateDialog v-model="showCreate" @created="open" />
  </div>
</template>

<style scoped>
.title-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.clickable :deep(.el-table__row) {
  cursor: pointer;
}

.ml {
  margin-left: 4px;
}

.busy {
  margin-left: 6px;
  vertical-align: middle;
}

.muted {
  color: var(--el-text-color-secondary);
}
</style>
