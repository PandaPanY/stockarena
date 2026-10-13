<script setup lang="ts">
/** 新建自定义策略：从空白或风格模板开始，用表单编辑规则，可试算（只看开发期），再选择是否让 AI 优化。 */
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { errorMessage } from '@/api/http'
import type { Provider, Universe } from '@/api/arena'
import { quantApi, type FactorInfo, type PreviewResult, type Seed, type StrategyDraft } from '@/api/quant'
import AiOptimizeFields from '@/components/lab/AiOptimizeFields.vue'
import PreviewPanel from '@/components/lab/PreviewPanel.vue'
import SpecEditor from '@/components/lab/SpecEditor.vue'
import { blankSpec, fromForm, toForm, type SpecForm } from '@/utils/spec'

const open = defineModel<boolean>({ required: true })
const props = defineProps<{
  providers: Provider[]
  factors: FactorInfo[]
  industries: string[]
  seeds: Seed[]
  universes: Universe[]
  /** 完整（全时点）数据是否已准备好；没有时只能用快速体验股票池 */
  fullReady?: boolean
  /** 快速体验数据是否已准备好 */
  quickReady?: boolean
}>()
const emit = defineEmits<{ created: [id: number] }>()

const form = reactive({
  name: '',
  description: '',
  template: 'blank',
  universe_name: 'cn_csi300_hist',
  provider_id: null as number | null,
  model: '',
  auto_optimize: false,
})
const spec = ref<SpecForm>(blankSpec())
const tab = ref('rules')
const editor = ref<InstanceType<typeof SpecEditor> | null>(null)
const previewing = ref(false)
const saving = ref(false)
const result = ref<PreviewResult | null>(null)
/** 每次试算的开发期夏普：创建时一并提交，计入测试次数 */
const trials = ref<(number | null)[]>([])

// ---------------------------------------------------------------- 一句话创建
const draftText = ref('')
const draftProvider = ref<number | null>(null)
const drafting = ref(false)
const draft = ref<StrategyDraft | null>(null)
/** 已经有规则（模板或上一次生成）时，默认在当前规则上修改 */
const draftOnCurrent = ref(false)

async function generate() {
  if (!draftText.value.trim()) {
    ElMessage.warning('请先描述你的选股想法')
    return
  }
  if (!draftProvider.value) {
    ElMessage.warning('请选择用来生成规则的模型')
    return
  }
  if (draftOnCurrent.value && !editor.value?.sync()) return
  drafting.value = true
  try {
    const d = await quantApi.draft({
      text: draftText.value.trim(),
      provider_id: draftProvider.value,
      model: props.providers.find((p) => p.id === draftProvider.value)?.default_model || null,
      base_spec: draftOnCurrent.value ? fromForm(spec.value) : undefined,
    })
    draft.value = d
    spec.value = toForm(d.spec)
    if (!form.name.trim() && d.name) form.name = d.name
    if (d.description) form.description = d.description
    result.value = null
    tab.value = 'rules'
    draftOnCurrent.value = true
    ElMessage.success(
      d.unsupported.length ? '已生成规则，有部分条件没能实现，请看下面的说明' : '已生成规则，可以检查后试算',
    )
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    drafting.value = false
  }
}

const choices = computed(() =>
  props.universes.filter((u) => (u.history && props.fullReady !== false) || (u.quick && props.quickReady)),
)
const isQuick = computed(() => !!props.universes.find((u) => u.name === form.universe_name)?.quick)

watch(open, (v) => {
  if (!v) return
  Object.assign(form, {
    name: '',
    description: '',
    template: 'blank',
    universe_name: choices.value[0]?.name ?? 'cn_csi300_hist',
  })
  Object.assign(form, { provider_id: null, model: '', auto_optimize: false })
  spec.value = blankSpec()
  result.value = null
  trials.value = []
  draft.value = null
  draftText.value = ''
  draftOnCurrent.value = false
  draftProvider.value = props.providers[0]?.id ?? null
  tab.value = 'rules'
})

function applyTemplate(name: string) {
  const seed = props.seeds.find((s) => s.style === name)
  spec.value = seed ? toForm(seed.spec) : blankSpec()
  if (seed && !form.description) form.description = seed.description
  result.value = null
  draftOnCurrent.value = !!seed
}

async function preview() {
  if (!editor.value?.sync()) return
  previewing.value = true
  try {
    result.value = await quantApi.preview({ spec: fromForm(spec.value), universe_name: form.universe_name })
    trials.value.push(result.value.dev.sharpe ?? null)
    tab.value = 'preview'
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    previewing.value = false
  }
}

async function create() {
  if (!form.name.trim()) {
    ElMessage.warning('请填写策略名称')
    return
  }
  if (!editor.value?.sync()) return
  saving.value = true
  try {
    const st = await quantApi.create({
      name: form.name.trim(),
      style: 'custom',
      description: form.description,
      spec: fromForm(spec.value),
      universe_name: form.universe_name,
      provider_id: form.provider_id,
      model: form.model || null,
      auto_optimize: form.auto_optimize,
      trial_scores: trials.value,
    })
    ElMessage.success(`已创建「${st.name}」`)
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
  <el-dialog v-model="open" title="新建策略" width="900px" top="4vh" :close-on-click-modal="false">
    <el-form label-width="110px" size="small">
      <el-form-item label="一句话描述">
        <div class="draft">
          <el-input
            v-model="draftText"
            type="textarea"
            :rows="2"
            maxlength="500"
            placeholder="例如：低估值、高股息、过去一年回撤小的大盘股，每月调仓，同一行业不超过 3 只"
          />
          <div class="draft-actions">
            <template v-if="providers.length">
              <el-select v-model="draftProvider" placeholder="用哪个模型生成" style="width: 200px">
                <el-option v-for="p in providers" :key="p.id" :label="p.name" :value="p.id" />
              </el-select>
              <el-checkbox v-model="draftOnCurrent">在当前规则上修改</el-checkbox>
              <el-button type="primary" plain :loading="drafting" @click="generate">AI 生成规则</el-button>
            </template>
            <span v-else class="hint">
              用一句话生成规则需要 AI 模型：先在「配置中心 → 模型供应商」添加。不用 AI
              也可以从下面的模板或空白开始编辑。
            </span>
          </div>
          <span class="hint">AI 只把描述翻译成规则，不看回测结果；生成后请检查规则，再点「试算」。</span>
        </div>
      </el-form-item>
      <el-form-item v-if="draft" label="AI 说明">
        <div class="draft">
          <el-alert
            v-if="draft.unsupported.length"
            type="warning"
            :closable="false"
            title="描述里这些条件没有实现（平台的规则表达不了）："
          >
            <ul class="draft-list">
              <li v-for="(u, i) in draft.unsupported" :key="i">{{ u }}</li>
            </ul>
          </el-alert>
          <el-alert v-else type="success" :closable="false" title="描述里的条件都已用规则表达" />
          <p v-if="draft.notes" class="hint draft-notes">{{ draft.notes }}</p>
        </div>
      </el-form-item>
      <el-form-item label="名称" required>
        <el-input v-model="form.name" maxlength="64" placeholder="如：低波红利" style="width: 320px" />
      </el-form-item>
      <el-form-item label="风格说明">
        <el-input
          v-model="form.description"
          type="textarea"
          :rows="2"
          placeholder="这条策略想抓住什么（AI 优化时会参考）"
        />
      </el-form-item>
      <el-form-item label="起点">
        <el-select v-model="form.template" style="width: 220px" @change="applyTemplate">
          <el-option label="空白（一个动量因子）" value="blank" />
          <el-option v-for="s in seeds" :key="s.style" :label="`模板：${s.title}`" :value="s.style" />
        </el-select>
        <span class="hint">想在现有策略上改，可以在该策略页「另存为新策略」</span>
      </el-form-item>
      <el-form-item label="股票池">
        <el-select v-model="form.universe_name" style="width: 320px" @change="result = null">
          <el-option v-for="u in choices" :key="u.name" :label="u.title" :value="u.name" />
        </el-select>
        <span v-if="!isQuick" class="hint">全时点：含历史上被剔除、已退市的股票，回测不偏乐观</span>
        <el-alert
          v-else
          type="warning"
          :closable="false"
          class="quick-warn"
          title="快速体验股票池只用今天的上证50 成分回看历史，有幸存者偏差，回测偏乐观，不能通过「可实盘检查」；完整数据准备好后，建议在全时点股票池上重新创建。"
        />
      </el-form-item>
      <AiOptimizeFields
        v-model:provider-id="form.provider_id"
        v-model:model="form.model"
        v-model:auto="form.auto_optimize"
        :providers="providers"
      />
    </el-form>

    <el-tabs v-model="tab">
      <el-tab-pane label="规则" name="rules">
        <SpecEditor ref="editor" v-model="spec" :factors="factors" :industries="industries" />
      </el-tab-pane>
      <el-tab-pane label="试算结果" name="preview" :disabled="!result">
        <PreviewPanel v-if="result" :result="result" />
      </el-tab-pane>
    </el-tabs>

    <template #footer>
      <span class="foot-hint">
        <template v-if="trials.length">已试算 {{ trials.length }} 次，创建后计入测试次数；</template>
        创建时按完整流程回测（含稳健性检验），约需十几秒到一分钟
      </span>
      <el-button @click="open = false">取消</el-button>
      <el-button :loading="previewing" @click="preview">试算</el-button>
      <el-button type="primary" :loading="saving" @click="create">创建</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.draft {
  display: flex;
  flex-direction: column;
  gap: 6px;
  width: 100%;
}

.draft-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.draft-list {
  margin: 4px 0 0;
  padding-left: 18px;
}

.draft-notes {
  margin: 0;
}

.quick-warn {
  margin-top: 6px;
}

.hint {
  margin-left: 8px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.foot-hint {
  float: left;
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 32px;
}
</style>
