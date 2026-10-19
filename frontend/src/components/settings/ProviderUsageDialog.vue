<script setup lang="ts">
/** 用到某个模型的地方：模型失效、无法恢复时，把 AI 优化、自选股监控和选中的 AI 对比选手一次换成其他模型。
 *  AI 交易复核不在这里换：换复核模型会开始新的一段评估，要在组合页「更换复核模型」。 */
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { errorMessage } from '@/api/http'
import { providersApi, type Provider, type ProviderUsages } from '@/api/arena'
import { formatDateTime } from '@/utils/format'

const open = defineModel<boolean>({ required: true })
const props = defineProps<{ provider: Provider | null; providers: Provider[] }>()
const emit = defineEmits<{ changed: [] }>()

const usages = ref<ProviderUsages | null>(null)
const loading = ref(false)
const saving = ref(false)
const targetId = ref<number | null>(null)
const model = ref('')
const doStrategies = ref(true)
const doWatch = ref(true)
const agentIds = ref<number[]>([])

const others = computed(() => props.providers.filter((p) => p.id !== props.provider?.id))
const target = computed(() => props.providers.find((p) => p.id === targetId.value) ?? null)
const nothing = computed(
  () =>
    usages.value &&
    !usages.value.strategies.length &&
    !usages.value.reviews.length &&
    !usages.value.watch &&
    !usages.value.agents.length,
)

watch(open, async (v) => {
  if (!v || !props.provider) return
  usages.value = null
  targetId.value = others.value[0]?.id ?? null
  model.value = ''
  doStrategies.value = doWatch.value = true
  agentIds.value = []
  loading.value = true
  try {
    usages.value = await providersApi.usages(props.provider.id)
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    loading.value = false
  }
})

async function replace() {
  if (!props.provider || !targetId.value) return ElMessage.warning('请选择要换成的模型供应商')
  saving.value = true
  try {
    const res = await providersApi.replace(props.provider.id, {
      target_provider_id: targetId.value,
      model: model.value.trim() || null,
      strategies: doStrategies.value,
      watch: doWatch.value,
      agent_ids: agentIds.value,
    })
    const r = res.replaced
    ElMessage.success(
      `已更换：${r.strategies} 条策略的 AI 优化${r.watch ? '、自选股监控' : ''}${r.agents ? `、${r.agents} 个 AI 对比选手` : ''}`,
    )
    usages.value = { ...res.remaining, health: usages.value!.health }
    emit('changed')
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <el-dialog v-model="open" :title="`用到「${provider?.name ?? ''}」的地方`" width="680px">
    <div v-loading="loading">
      <el-alert
        v-if="usages?.health?.fails"
        type="error"
        :closable="false"
        show-icon
        class="gap"
        :title="`连续 ${usages.health.fails} 次调用失败`"
      >
        最近一次：{{ formatDateTime(usages.health.last_failure_at) }}，{{ usages.health.last_error }}
      </el-alert>

      <template v-if="usages">
        <p v-if="nothing" class="muted">目前没有功能在用这个模型，可以直接删除。</p>

        <template v-if="usages.strategies.length">
          <h4>AI 优化（{{ usages.strategies.length }} 条策略）</h4>
          <p class="muted">
            {{
              usages.strategies
                .map((x) => x.name + (x.auto_optimize ? '（每晚自动）' : '') + (x.model ? ` · ${x.model}` : ''))
                .join('、')
            }}
          </p>
        </template>
        <template v-if="usages.watch">
          <h4>自选股监控</h4>
          <p class="muted">
            {{ usages.watch.enabled ? '开启中' : '已关闭' }}{{ usages.watch.model ? ` · ${usages.watch.model}` : '' }}
          </p>
        </template>
        <template v-if="usages.agents.length">
          <h4>AI 对比选手</h4>
          <p class="muted">换了模型就不再是原来那个选手的比较，默认不换；确实要换的请勾选。</p>
          <el-checkbox-group v-model="agentIds">
            <el-checkbox v-for="a in usages.agents" :key="a.id" :value="a.id">
              {{ a.name }}<span class="muted">{{ a.tracks.length ? `（${a.tracks.join('、')}）` : '（未参赛）' }}</span>
            </el-checkbox>
          </el-checkbox-group>
        </template>
        <template v-if="usages.reviews.length">
          <h4>AI 交易复核</h4>
          <p class="muted">
            {{
              usages.reviews.map((x) => x.name).join('、')
            }}。不在这里更换：换复核模型会结束当前一段评估（之前的数据封存， 新模型重新计数），请到「多风格组合 → AI
            复核」点「更换复核模型」，并写明原因。
          </p>
        </template>

        <el-divider v-if="usages.strategies.length || usages.watch || usages.agents.length" />
        <el-form
          v-if="usages.strategies.length || usages.watch || usages.agents.length"
          label-width="90px"
          size="small"
        >
          <el-form-item label="换成">
            <el-select v-model="targetId" placeholder="选择模型供应商" style="width: 220px">
              <el-option v-for="p in others" :key="p.id" :label="p.name" :value="p.id" />
            </el-select>
            <el-input
              v-model="model"
              :placeholder="target?.default_model ? `默认 ${target.default_model}` : '模型名'"
              style="width: 220px; margin-left: 8px"
            />
          </el-form-item>
          <el-form-item label="更换范围">
            <el-checkbox v-if="usages.strategies.length" v-model="doStrategies">AI 优化</el-checkbox>
            <el-checkbox v-if="usages.watch" v-model="doWatch">自选股监控</el-checkbox>
            <span v-if="agentIds.length" class="muted">以及勾选的 {{ agentIds.length }} 个选手</span>
          </el-form-item>
        </el-form>
        <p v-if="!others.length" class="muted">还没有其他模型供应商，请先添加一个。</p>
      </template>
    </div>
    <template #footer>
      <el-button @click="open = false">关闭</el-button>
      <el-button
        v-if="usages && (usages.strategies.length || usages.watch || usages.agents.length)"
        type="primary"
        :loading="saving"
        :disabled="!targetId"
        @click="replace"
        >更换</el-button
      >
    </template>
  </el-dialog>
</template>

<style scoped>
h4 {
  margin: 12px 0 4px;
}

.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 1.7;
  margin: 0;
}

.gap {
  margin-bottom: 8px;
}
</style>
