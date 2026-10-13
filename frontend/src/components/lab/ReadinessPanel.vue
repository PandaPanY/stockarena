<script setup lang="ts">
/**
 * 「可实盘」检查（PRD 3.9）：历史检验（开发期、留出期、逐年）+ 实时跟踪 ≥ 3 个月 + 资金容量，全部满足才标记「可实盘」。
 * 实盘之后在「实盘对照」页录入成交，持续检验实盘与模拟是否一致。
 */
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { errorMessage } from '@/api/http'
import { readinessApi, type Kind, type Readiness } from '@/api/quant'

const props = defineProps<{ kind: Kind; id: number }>()
const data = ref<Readiness | null>(null)
const loading = ref(false)

async function load(refresh = false) {
  loading.value = true
  try {
    data.value = await readinessApi.get(props.kind, props.id, refresh)
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    loading.value = false
  }
}
onMounted(() => load(false))

const STATUS_TYPE: Record<string, 'success' | 'warning' | 'danger'> = {
  可实盘: 'success',
  待观察: 'warning',
  未通过: 'danger',
}
const LEVEL: Record<string, { text: string; type: 'success' | 'danger' | 'warning' | 'info' }> = {
  pass: { text: '通过', type: 'success' },
  fail: { text: '未通过', type: 'danger' },
  pending: { text: '待观察', type: 'warning' },
  info: { text: '参考', type: 'info' },
}
const time = (s?: string) => (s ? new Date(s).toLocaleString('zh-CN', { hour12: false }) : '-')
</script>

<template>
  <div v-loading="loading" class="ready">
    <p class="muted">
      同时满足以下条件的版本才标记为「可实盘」：开发期与留出期风险调整后跑赢基准、逐年稳定、当前版本实时跟踪满 3
      个月且与回测一致、计划资金在容量范围内。每晚自动刷新（仅跟踪中的策略 / 组合），首次计算约需 10～60 秒。
    </p>
    <template v-if="data">
      <div class="head">
        <el-tag :type="STATUS_TYPE[data.status] ?? 'info'" size="large" effect="dark">{{ data.status }}</el-tag>
        <span>{{ data.message }}</span>
        <span class="muted">检查时间：{{ time(data.checked_at) }}</span>
        <el-button size="small" @click="load(true)">重新检查</el-button>
      </div>
      <el-table :data="data.items" size="small" border>
        <el-table-column label="条件" width="220">
          <template #default="{ row }">{{ row.name }}</template>
        </el-table-column>
        <el-table-column label="结果" width="90" align="center">
          <template #default="{ row }">
            <el-tag :type="LEVEL[row.level].type" size="small">{{ LEVEL[row.level].text }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="说明" min-width="360">
          <template #default="{ row }"
            ><span class="detail">{{ row.detail }}</span></template
          >
        </el-table-column>
      </el-table>
      <p class="muted">
        「可实盘」只说明模拟检验通过，不保证未来收益；本工具不构成投资建议。实盘后请在「实盘对照」录入成交，对比执行损耗。
      </p>
    </template>
    <el-button v-else-if="!loading" type="primary" @click="load(true)">开始检查</el-button>
  </div>
</template>

<style scoped>
.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 1.8;
}

.head {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin: 8px 0 12px;
}

.detail {
  white-space: pre-wrap;
  line-height: 1.6;
}
</style>
