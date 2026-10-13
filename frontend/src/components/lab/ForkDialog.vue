<script setup lang="ts">
/** 另存为新策略：以某个版本为初始版本，建立独立的策略（版本历史、优化、跟踪都各自独立）。 */
import { reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { errorMessage } from '@/api/http'
import type { Provider } from '@/api/arena'
import { quantApi, STATUS_TEXT, type StrategyDetail, type VersionBrief } from '@/api/quant'
import AiOptimizeFields from '@/components/lab/AiOptimizeFields.vue'

const open = defineModel<boolean>({ required: true })
const props = defineProps<{ strategy: StrategyDetail; version: VersionBrief | null; providers: Provider[] }>()
const emit = defineEmits<{ created: [id: number] }>()

const form = reactive({
  name: '',
  description: '',
  copy_notes: true,
  provider_id: null as number | null,
  model: '',
  auto_optimize: false,
})
const saving = ref(false)

watch(open, (v) => {
  if (!v) return
  const st = props.strategy
  Object.assign(form, {
    name: `${st.name}（副本）`.slice(0, 64),
    description: st.description,
    copy_notes: true,
    provider_id: st.provider_id,
    model: st.model ?? '',
    auto_optimize: false,
  })
})

async function save() {
  if (!form.name.trim()) {
    ElMessage.warning('请填写策略名称')
    return
  }
  saving.value = true
  try {
    const st = await quantApi.fork(props.strategy.id, {
      version_id: props.version?.id ?? null,
      name: form.name.trim(),
      description: form.description,
      copy_notes: form.copy_notes,
      provider_id: form.provider_id,
      model: form.model || null,
      auto_optimize: form.auto_optimize,
    })
    ElMessage.success(`已另存为「${st.name}」`)
    open.value = false
    emit('created', st.id)
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <el-dialog v-model="open" title="另存为新策略" width="640px">
    <p class="intro">
      以「{{ strategy.name }}」的
      <b v-if="version">v{{ version.number }}（{{ STATUS_TEXT[version.status] }}）</b>
      <b v-else>当前冠军</b>
      为初始版本，建立一条独立的策略：之后可以手动修改规则、让 AI 优化、开始实时跟踪，都不影响原策略。
    </p>
    <el-form label-width="110px" size="small">
      <el-form-item label="名称" required>
        <el-input v-model="form.name" maxlength="64" style="width: 320px" />
      </el-form-item>
      <el-form-item label="风格说明">
        <el-input v-model="form.description" type="textarea" :rows="3" />
      </el-form-item>
      <el-form-item label="研究笔记">
        <el-checkbox v-model="form.copy_notes">
          复制原策略的研究笔记（{{ strategy.research_notes.length }} 条，AI 优化时参考）
        </el-checkbox>
      </el-form-item>
      <AiOptimizeFields
        v-model:provider-id="form.provider_id"
        v-model:model="form.model"
        v-model:auto="form.auto_optimize"
        :providers="providers"
      />
    </el-form>
    <el-alert type="info" :closable="false" class="note">
      检验区间、本金与升级门槛沿用原策略；原策略用的是半时点股票池时，新策略改用全时点股票池并重新回测。
      为防止「另存一次就把检验次数清零」，新策略会继承原策略已检验过的候选数和尝试次数，升级门槛不会因此变松。
    </el-alert>
    <template #footer>
      <el-button @click="open = false">取消</el-button>
      <el-button type="primary" :loading="saving" @click="save">另存</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.intro {
  margin: 0 0 12px;
  line-height: 1.6;
}
.note {
  margin-top: 4px;
}
</style>
