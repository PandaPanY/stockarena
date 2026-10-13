<script setup lang="ts">
/** 交易成本：佣金按自己券商的费率填写，红利税按 A股持有期规则扣除；对所有策略回测与实时跟踪生效。 */
import { onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { errorMessage } from '@/api/http'
import { costsApi, type CostsStatus } from '@/api/quant'
import { formatDateTime } from '@/utils/format'

const status = ref<CostsStatus | null>(null)
const form = reactive({ rateWan: 2.5, min: 5, dividendTax: true })
const saving = ref(false)
let timer: ReturnType<typeof setInterval> | undefined

async function load(fillForm = false) {
  try {
    status.value = await costsApi.get()
    if (fillForm) {
      form.rateWan = +(status.value.costs.commission_rate * 1e4).toFixed(4)
      form.min = status.value.costs.commission_min
      form.dividendTax = status.value.costs.dividend_tax
    }
  } catch (err) {
    ElMessage.error(errorMessage(err))
  }
}

onMounted(async () => {
  await load(true)
  timer = setInterval(() => {
    if (status.value?.task?.running || (status.value && !status.value.up_to_date)) void load()
  }, 5000)
})
onBeforeUnmount(() => clearInterval(timer))

async function save() {
  saving.value = true
  try {
    status.value = await costsApi.save({
      commission_rate: form.rateWan / 1e4,
      commission_min: form.min,
      dividend_tax: form.dividendTax,
    })
    ElMessage.success(
      `已保存，并同步到 ${status.value.synced_tracks ?? 0} 个跟踪中的赛道；所有在用策略正在后台重新回测`,
    )
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <div>
    <p class="muted">
      按自己券商的实际费率填写，模拟结果才能和实盘对得上。修改后会同步到正在实时跟踪的赛道（之后的成交按新费率计算），
      并在后台按新设置重新回测所有在用策略的全部版本（约十几分钟，期间页面上的回测指标会陆续更新）。 印花税（卖出
      0.05%）与过户费（0.001%）按国家标准计算，不需要填写。
    </p>
    <el-form label-width="120px" class="form" @submit.prevent="save">
      <el-form-item label="佣金费率">
        <el-input-number v-model="form.rateWan" :min="0" :max="30" :step="0.1" :precision="2" />
        <span class="unit">万分之（双边）</span>
        <p class="muted help">常见：万 2.5；不少券商可以谈到万 1 左右。</p>
      </el-form-item>
      <el-form-item label="单笔最低佣金">
        <el-input-number v-model="form.min" :min="0" :max="50" :step="1" :precision="2" />
        <span class="unit">元</span>
        <p class="muted help">一般为 5 元；「免五」的券商填 0。资金量较小、订单金额小时影响明显。</p>
      </el-form-item>
      <el-form-item label="红利税">
        <el-switch v-model="form.dividendTax" active-text="按持有期扣除" inactive-text="不扣" />
        <p class="muted help">
          A股股票分红按持股时间差别化征税：≤1 个月 20%，1 个月～1 年 10%，超过 1 年免征；ETF 分红免税。
          模拟在除息日按建仓至今的持有期扣除（之后继续持有超过 1 年时会略多扣，偏保守）。
        </p>
      </el-form-item>
      <el-form-item>
        <el-button type="primary" native-type="submit" :loading="saving">保存并重新回测</el-button>
      </el-form-item>
    </el-form>

    <el-alert v-if="status" :closable="false" :type="status.up_to_date ? 'success' : 'warning'" class="state">
      <template #title>
        当前生效：{{ status.describe }}。
        <span v-if="status.up_to_date">所有在用策略的回测已按此设置计算。</span>
        <span v-else>回测指标尚未全部按此设置更新。</span>
      </template>
      <div v-if="status.task">
        {{ status.task.running ? '进行中：' : '最近一次：' }}{{ status.task.message }}
        <span v-if="status.task.finished_at" class="muted">（{{ formatDateTime(status.task.finished_at) }}）</span>
      </div>
    </el-alert>
  </div>
</template>

<style scoped>
.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 1.8;
}

.form {
  max-width: 720px;
  margin-top: 8px;
}

.unit {
  margin-left: 8px;
  color: var(--el-text-color-regular);
}

.help {
  margin: 2px 0 0;
  width: 100%;
}

.state {
  max-width: 720px;
}
</style>
