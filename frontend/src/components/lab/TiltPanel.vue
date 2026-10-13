<script setup lang="ts">
/**
 * 自选倾斜：与规则账户并行的账户，策略选中的股票按自选权重多配 / 少配 / 不买，比较两条净值看倾斜是加分还是减分。
 */
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { errorMessage } from '@/api/http'
import { tiltApi, type TiltReport } from '@/api/quant'
import LabCurve from '@/components/lab/LabCurve.vue'
import StockLink from '@/components/StockLink.vue'
import { formatPercent } from '@/utils/format'

const props = defineProps<{ id: number; tracking: boolean }>()

const rep = ref<TiltReport | null>(null)
const loading = ref(false)
const saving = ref(false)

async function load() {
  loading.value = true
  try {
    rep.value = await tiltApi.get(props.id)
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    loading.value = false
  }
}
onMounted(load)

async function enable() {
  saving.value = true
  try {
    await tiltApi.enable(props.id)
    ElMessage.success('已开启：自选倾斜账户从下一个交易日收盘后开始决策')
    await load()
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    saving.value = false
  }
}

async function disable() {
  try {
    await ElMessageBox.confirm('停止后倾斜账户不再交易（已有记录保留），之后可以重新开启。', '停止自选倾斜', {
      type: 'warning',
    })
    await tiltApi.disable(props.id)
    await load()
  } catch (err) {
    if (err !== 'cancel') ElMessage.error(errorMessage(err))
  }
}

const curves = computed(() => {
  const c = rep.value?.curve
  if (!c?.length) return null
  return { dates: c.map((x) => x.date), nav: c.map((x) => x.tilt), bench: c.map((x) => x.rule) }
})
const moved = computed(() => (rep.value?.last_changes?.changes ?? []).filter((c) => Math.abs(c.tilted - c.rule) > 1e-4))
</script>

<template>
  <div v-loading="loading">
    <el-alert type="info" :closable="false" class="intro">
      <template #title>自选倾斜：规则选中的股票，按你的自选权重多配、少配或不买</template>
      倾斜账户与规则账户在同一个赛道中运行、本金相同、调仓时间相同；每次调仓把规则的目标仓位乘以自选权重（不在自选股里的是
      1），再按比例缩放回原来的总仓位。<b>不会买入策略没有选中的股票</b>；单只最多是原仓位的 3 倍、且不超过 10%。
      两条净值的差就是你的偏好带来的加分或减分。自选权重在「自选股」页面设置，下一次调仓时生效。
    </el-alert>

    <template v-if="rep && !rep.enabled">
      <p v-if="!tracking" class="muted">请先在「信号与实时跟踪」中开始实时跟踪，倾斜账户会加入同一个赛道。</p>
      <el-button type="primary" size="small" :disabled="!tracking" :loading="saving" @click="enable"
        >开启自选倾斜</el-button
      >
      <p class="muted">
        说明：自选权重是你现在的偏好，不能拿去回测历史（那等于用了后见之明），所以只能从开启之后实时比较。
      </p>
    </template>

    <template v-else-if="rep">
      <div class="toolbar">
        <el-tag type="success">进行中</el-tag>
        <span class="muted">开启于 {{ rep.settings.since }}</span>
        <span class="spacer" />
        <el-button size="small" @click="load">刷新</el-button>
        <el-button size="small" type="danger" text @click="disable">停止自选倾斜</el-button>
      </div>
      <el-descriptions :column="3" border size="small" class="summary">
        <el-descriptions-item label="比较起点">{{ rep.since ?? '尚无净值' }}</el-descriptions-item>
        <el-descriptions-item label="纯规则">{{ formatPercent(rep.rule_return, 2) }}</el-descriptions-item>
        <el-descriptions-item label="自选倾斜">{{ formatPercent(rep.tilt_return, 2) }}</el-descriptions-item>
      </el-descriptions>
      <LabCurve v-if="curves" :curves="curves" :height="280" name="自选倾斜" bench-name="纯规则" />
      <p v-else class="muted">两个账户都有净值后显示对比曲线。</p>

      <h4>最近一次调整{{ rep.last_changes ? `（${rep.last_changes.date} 收盘）` : '' }}</h4>
      <el-table :data="moved" size="small" empty-text="最近一次调仓没有自选股被调整">
        <el-table-column label="股票" min-width="140">
          <template #default="{ row }"><StockLink :symbol="row.symbol" /></template>
        </el-table-column>
        <el-table-column label="自选权重" width="90" align="right">
          <template #default="{ row }">{{ row.weight }}</template>
        </el-table-column>
        <el-table-column label="规则仓位" width="100" align="right">
          <template #default="{ row }">{{ formatPercent(row.rule, 2, false) }}</template>
        </el-table-column>
        <el-table-column label="倾斜后" width="100" align="right">
          <template #default="{ row }">{{ formatPercent(row.tilted, 2, false) }}</template>
        </el-table-column>
      </el-table>
    </template>

    <h4>当前自选权重（不是 1 的）</h4>
    <div v-if="rep?.weights.length" class="weights">
      <el-tag v-for="w in rep.weights" :key="w.symbol" :type="w.weight === 0 ? 'info' : 'warning'" class="w">
        {{ w.name }} × {{ w.weight }}
      </el-tag>
    </div>
    <p v-else class="muted">所有股票都是默认权重 1，倾斜账户与规则账户完全相同。在「自选股」页面调整。</p>
  </div>
</template>

<style scoped>
.intro {
  margin-bottom: 12px;
}

.toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 8px 0;
}

.spacer {
  flex: 1;
}

.summary {
  margin: 8px 0;
}

.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 1.8;
}

h4 {
  margin: 14px 0 8px;
}

.w {
  margin: 0 6px 6px 0;
}
</style>
