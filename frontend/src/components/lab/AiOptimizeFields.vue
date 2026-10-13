<script setup lang="ts">
/** 新建 / 另存策略时的「AI 优化」设置：研究模型与每晚自动优化（默认关闭）。 */
import { watch } from 'vue'
import type { Provider } from '@/api/arena'

const providerId = defineModel<number | null>('providerId', { required: true })
const model = defineModel<string>('model', { required: true })
const auto = defineModel<boolean>('auto', { required: true })
defineProps<{ providers: Provider[] }>()

watch(providerId, (v) => {
  if (v === null) auto.value = false
})
</script>

<template>
  <el-form-item label="研究模型">
    <el-select v-model="providerId" clearable placeholder="不使用 AI" style="width: 180px">
      <el-option v-for="p in providers" :key="p.id" :label="p.name" :value="p.id" />
    </el-select>
    <el-input
      v-model="model"
      :disabled="providerId === null"
      :placeholder="providers.find((p) => p.id === providerId)?.default_model ?? '默认模型'"
      style="width: 220px; margin-left: 8px"
    />
  </el-form-item>
  <el-form-item label="AI 优化">
    <el-switch v-model="auto" :disabled="providerId === null" active-text="每晚自动优化" />
    <p class="hint">
      设置研究模型后，可以随时在策略页手动让 AI 优化，也可以开启每晚自动优化。AI
      提出的修改要通过严格的升级检验才会替换冠军； 实测 AI 修改单个策略的规则在样本外没有可靠的提升（见使用手册第 10
      章），所以默认关闭。
    </p>
  </el-form-item>
</template>

<style scoped>
.hint {
  margin: 4px 0 0;
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 1.5;
}
</style>
