<script setup lang="ts">
/** 导入策略文件（.stockarena.yaml）：新建自己的私有策略并按本平台的数据回测，测试历史从文件继承。 */
import { ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { errorMessage } from '@/api/http'
import { quantApi } from '@/api/quant'

const open = defineModel<boolean>({ required: true })
const emit = defineEmits<{ imported: [id: number] }>()

const text = ref('')
const name = ref('')
const saving = ref(false)

watch(open, (v) => {
  if (v) {
    text.value = ''
    name.value = ''
  }
})

async function onFile(e: Event) {
  const f = (e.target as HTMLInputElement).files?.[0]
  if (!f) return
  if (f.size > 500_000) {
    ElMessage.warning('文件太大（超过 500 KB），不像是策略文件')
    return
  }
  text.value = await f.text()
  const m = text.value.match(/^name:\s*(.+)$/m)
  if (m && !name.value) name.value = m[1].trim().replace(/^['"]|['"]$/g, '')
}

async function submit() {
  if (!text.value.trim()) {
    ElMessage.warning('请先选择策略文件，或把文件内容粘贴到下面')
    return
  }
  saving.value = true
  try {
    const st = await quantApi.importStrategy({ text: text.value, name: name.value.trim() || null })
    open.value = false
    if (st.warnings?.length) {
      await ElMessageBox.alert(st.warnings.join('\n'), `已导入「${st.name}」，请注意`, { type: 'warning' })
    } else {
      ElMessage.success(`已导入「${st.name}」`)
    }
    emit('imported', st.id)
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <el-dialog v-model="open" title="导入策略" width="640px" :close-on-click-modal="false">
    <p class="muted">
      选择别人分享的策略文件（<code>.stockarena.yaml</code>，在策略页「…」菜单里导出）。导入后成为你自己的私有策略，
      会按本平台的数据和你的交易成本重新回测；文件里的测试历史会继承下来（用来收紧升级门槛、计算折算可信度），
      不能靠「导出再导入」把测试次数清零。
    </p>
    <el-form label-width="90px" size="small">
      <el-form-item label="策略文件">
        <input type="file" accept=".yaml,.yml,text/yaml" @change="onFile" />
      </el-form-item>
      <el-form-item label="名称">
        <el-input v-model="name" maxlength="64" placeholder="默认用文件里的名称" style="width: 320px" />
      </el-form-item>
      <el-form-item label="文件内容">
        <el-input v-model="text" type="textarea" :rows="10" placeholder="也可以直接粘贴文件内容" class="mono" />
      </el-form-item>
    </el-form>
    <template #footer>
      <span class="muted foot">导入时会完整回测一次，约需十几秒到一分钟</span>
      <el-button @click="open = false">取消</el-button>
      <el-button type="primary" :loading="saving" @click="submit">导入</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 1.6;
}

.foot {
  margin-right: 12px;
}

.mono :deep(textarea) {
  font-family: ui-monospace, Consolas, monospace;
  font-size: 12px;
}
</style>
