<script setup lang="ts">
/** 升级情况：当前冠军以来的候选为什么都没升级（按原因汇总）、下一轮的门槛、每晚自动优化是否已因门槛过严暂停。 */
import { computed } from 'vue'
import type { UpgradeStatus } from '@/api/quant'
import { formatDateTime } from '@/utils/format'

const props = defineProps<{ status: UpgradeStatus; mine: boolean }>()
const emit = defineEmits<{ resume: [] }>()

const paused = computed(() => props.status.auto_pause && !props.status.auto_pause.resumed)
const fmtP = (p: number) => (p >= 0.01 ? p.toFixed(2) : p.toPrecision(2))
</script>

<template>
  <el-alert :type="status.hopeless ? 'warning' : 'info'" :closable="false" show-icon class="upgrade">
    <template #title>
      <span v-if="status.hopeless">升级门槛已经很严：继续优化几乎不可能升级</span>
      <span v-else>升级情况</span>
    </template>
    <div class="body">
      <p>
        冠军 v{{ status.champion_number }} 以来检验了 <b>{{ status.candidates }}</b> 个候选，都没有升级为冠军：
        <span v-for="(r, i) in status.reasons" :key="r.code"
          >{{ i ? ' · ' : '' }}{{ r.label }} <b>{{ r.count }}</b></span
        >
        <span class="muted">（一个候选可能同时有几条原因）</span>。
      </p>
      <p v-if="status.tested">
        做了月度配对检验的 {{ status.tested }} 个候选中，p &lt; 0.05 的 <b>{{ status.p_below_05 }}</b> 个，最小 p =
        {{ status.best_p === null ? '-' : fmtP(status.best_p) }}；只差显著性这一条的 {{ status.only_significance }} 个。
      </p>
      <p>
        下一轮的候选需要 <b>p ≤ {{ fmtP(status.p_need) }}</b
        >（按累计 {{ status.tests }} 个候选加本轮校正）。
        <template v-if="status.hopeless">
          门槛严于 {{ status.pause_p }} 时，月度超额要非常大且稳定才可能通过；候选多数连开发期夏普都没超过冠军时，
          说明在现有规则和因子上 AI 很难再找到可靠的改进。
        </template>
      </p>
      <p v-if="paused" class="pause">
        每晚自动优化已于 {{ formatDateTime(status.auto_pause!.at) }} 暂停（避免白白消耗模型额度）。
        <el-button v-if="mine" size="small" @click="emit('resume')">仍然继续每晚自动优化</el-button>
      </p>
      <p v-else-if="status.auto_pause?.resumed" class="muted">
        门槛过严后你选择了继续每晚自动优化；换了新冠军后检验次数会重新开始。
      </p>
    </div>
  </el-alert>
</template>

<style scoped>
.upgrade {
  margin-bottom: 12px;
}

.body p {
  margin: 4px 0;
  line-height: 1.7;
}

.muted {
  color: var(--el-text-color-secondary);
}

.pause {
  font-weight: 500;
}
</style>
