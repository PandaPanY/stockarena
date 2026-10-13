<script setup lang="ts">
/**
 * 策略规则编辑器：用表单编辑策略规格（选股范围、因子、过滤条件、持仓与权重、调仓、大盘择时、风控），
 * 也可以切换到 JSON 直接编辑。最终由后端校验。
 */
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import type { FactorInfo } from '@/api/quant'
import PctInput from '@/components/lab/PctInput.vue'
import {
  defaultParams,
  fromForm,
  MAX_FACTORS,
  MAX_FILTERS,
  OPS,
  toForm,
  type FactorTerm,
  type FilterRule,
  type SpecForm,
} from '@/utils/spec'

const model = defineModel<SpecForm>({ required: true })
const props = defineProps<{ factors: FactorInfo[]; industries: string[] }>()

const mode = ref<'form' | 'json'>('form')
const jsonText = ref('')

const byName = computed(() => Object.fromEntries(props.factors.map((f) => [f.name, f])))
const hasStock = computed(() => model.value.universe.types.includes('stock'))
const priceFactors = computed(() => props.factors.filter((f) => !f.fundamental))
const fundFactors = computed(() => props.factors.filter((f) => f.fundamental))
const timingFactors = computed(() => props.factors.filter((f) => f.timing))

function switchMode(next: string | number | boolean) {
  if (next === 'json') {
    jsonText.value = JSON.stringify(fromForm(model.value), null, 2)
    mode.value = 'json'
    return
  }
  try {
    model.value = toForm(JSON.parse(jsonText.value))
    mode.value = 'form'
  } catch {
    ElMessage.error('JSON 格式不正确，请先修正')
  }
}

/** 父组件提交前调用：JSON 模式下先把文本解析回表单；格式错误时返回 false */
function sync(): boolean {
  if (mode.value !== 'json') return true
  try {
    model.value = toForm(JSON.parse(jsonText.value))
    return true
  } catch {
    ElMessage.error('JSON 格式不正确，请先修正')
    return false
  }
}
defineExpose({ sync })

// 外部整体替换规格（如换模板）时，JSON 模式下同步文本
watch(
  () => model.value,
  (v) => {
    if (mode.value === 'json') jsonText.value = JSON.stringify(fromForm(v), null, 2)
  },
)

function onFactorChange(term: FactorTerm | FilterRule | SpecForm['timing']) {
  term.params = defaultParams(byName.value[term.name])
}

function addFactor() {
  model.value.factors.push({
    name: 'momentum',
    params: defaultParams(byName.value.momentum),
    weight: 1,
    direction: 'high',
    neutralize: 'none',
  })
}

function addFilter() {
  model.value.filters.push({ name: 'volatility', params: defaultParams(byName.value.volatility), op: '<', value: 0.5 })
}

const paramStep = (name: string, key: string) => (byName.value[name]?.params[key]?.integer === false ? 0.1 : 1)
const paramList = (name: string) => Object.entries(byName.value[name]?.params ?? {})

function toggleNullable(obj: Record<string, unknown>, key: string, on: boolean, value: unknown) {
  obj[key] = on ? value : null
}
</script>

<template>
  <div class="spec-editor">
    <el-radio-group :model-value="mode" size="small" class="mode" @change="switchMode">
      <el-radio-button value="form">表单</el-radio-button>
      <el-radio-button value="json">JSON</el-radio-button>
    </el-radio-group>

    <el-input v-if="mode === 'json'" v-model="jsonText" type="textarea" :rows="22" class="mono" />

    <el-form v-else label-width="110px" size="small" class="form">
      <!-- 选股范围 -->
      <h4>选股范围</h4>
      <el-form-item label="品种">
        <el-checkbox-group v-model="model.universe.types">
          <el-checkbox value="stock">股票</el-checkbox>
          <el-checkbox value="etf">ETF</el-checkbox>
        </el-checkbox-group>
      </el-form-item>
      <el-form-item v-if="hasStock" label="基本条件">
        <el-checkbox v-model="model.universe.exclude_st">排除 ST</el-checkbox>
        <span class="inline">上市满</span>
        <el-input-number v-model="model.universe.min_listed_days" :min="0" :max="1000" :step="20" />
        <span class="inline">个交易日</span>
      </el-form-item>
      <el-form-item label="成交额下限">
        <el-input-number v-model="model.universe.min_amount" :min="0" :max="50" :step="0.1" :precision="1" />
        <span class="inline">亿元（20 日平均）</span>
      </el-form-item>
      <el-form-item v-if="hasStock" label="只选行业">
        <el-select v-model="model.universe.industries" multiple filterable clearable placeholder="不限" class="wide">
          <el-option v-for="i in industries" :key="i" :label="i" :value="i" />
        </el-select>
      </el-form-item>
      <el-form-item v-if="hasStock" label="排除行业">
        <el-select
          v-model="model.universe.exclude_industries"
          multiple
          filterable
          clearable
          placeholder="无"
          class="wide"
        >
          <el-option v-for="i in industries" :key="i" :label="i" :value="i" />
        </el-select>
      </el-form-item>
      <el-form-item label="只在这些标的中选">
        <el-select
          v-model="model.universe.symbols"
          multiple
          filterable
          allow-create
          default-first-option
          placeholder="不限（输入代码，如 510300.SH）"
          class="wide"
        />
      </el-form-item>
      <el-form-item label="排除标的">
        <el-select
          v-model="model.universe.exclude"
          multiple
          filterable
          allow-create
          default-first-option
          placeholder="无"
          class="wide"
        />
      </el-form-item>

      <!-- 因子 -->
      <h4>
        打分因子（{{ model.factors.length }} / {{ MAX_FACTORS }}）
        <span class="hint">每个因子按排名打分，加权合成总分，选总分最高的</span>
      </h4>
      <div v-for="(f, k) in model.factors" :key="k" class="term">
        <div class="term-row">
          <el-select v-model="f.name" filterable style="width: 180px" @change="onFactorChange(f)">
            <el-option-group label="行情类">
              <el-option v-for="x in priceFactors" :key="x.name" :label="x.title" :value="x.name" />
            </el-option-group>
            <el-option-group v-if="hasStock" label="财报类（仅股票）">
              <el-option v-for="x in fundFactors" :key="x.name" :label="x.title" :value="x.name" />
            </el-option-group>
          </el-select>
          <el-radio-group v-model="f.direction">
            <el-radio-button value="high">越高越好</el-radio-button>
            <el-radio-button value="low">越低越好</el-radio-button>
          </el-radio-group>
          <span class="inline">权重</span>
          <el-input-number v-model="f.weight" :min="0.1" :max="10" :step="0.5" :precision="1" style="width: 90px" />
          <el-checkbox
            v-if="hasStock"
            :model-value="f.neutralize === 'industry'"
            @change="(v: string | number | boolean) => (f.neutralize = v ? 'industry' : 'none')"
          >
            行业内排名
          </el-checkbox>
          <el-button text type="danger" :disabled="model.factors.length <= 1" @click="model.factors.splice(k, 1)"
            >删除</el-button
          >
        </div>
        <div v-if="paramList(f.name).length" class="term-row params">
          <span v-for="[key, p] in paramList(f.name)" :key="key" class="param">
            {{ p.title || key }}
            <el-input-number
              v-model="f.params[key]"
              :min="p.min"
              :max="p.max"
              :step="paramStep(f.name, key)"
              style="width: 100px"
            />
          </span>
        </div>
        <p class="hint">{{ byName[f.name]?.description }}</p>
      </div>
      <el-button size="small" :disabled="model.factors.length >= MAX_FACTORS" @click="addFactor">添加因子</el-button>

      <!-- 过滤 -->
      <h4>
        过滤条件（{{ model.filters.length }} / {{ MAX_FILTERS }}）
        <span class="hint">不满足条件的标的不参与打分</span>
      </h4>
      <div v-for="(r, k) in model.filters" :key="k" class="term">
        <div class="term-row">
          <el-select v-model="r.name" filterable style="width: 180px" @change="onFactorChange(r)">
            <el-option-group label="行情类">
              <el-option v-for="x in priceFactors" :key="x.name" :label="x.title" :value="x.name" />
            </el-option-group>
            <el-option-group v-if="hasStock" label="财报类（仅股票）">
              <el-option v-for="x in fundFactors" :key="x.name" :label="x.title" :value="x.name" />
            </el-option-group>
          </el-select>
          <el-select v-model="r.op" style="width: 70px">
            <el-option v-for="o in OPS" :key="o" :label="o" :value="o" />
          </el-select>
          <el-input-number v-model="r.value" :step="0.1" style="width: 120px" />
          <span v-for="[key, p] in paramList(r.name)" :key="key" class="param">
            {{ p.title || key }}
            <el-input-number
              v-model="r.params[key]"
              :min="p.min"
              :max="p.max"
              :step="paramStep(r.name, key)"
              style="width: 100px"
            />
          </span>
          <el-button text type="danger" @click="model.filters.splice(k, 1)">删除</el-button>
        </div>
      </div>
      <el-button size="small" :disabled="model.filters.length >= MAX_FILTERS" @click="addFilter"
        >添加过滤条件</el-button
      >

      <!-- 持仓与权重 -->
      <h4>持仓与权重</h4>
      <el-form-item label="持有数量">
        <el-input-number v-model="model.selection.top_n" :min="1" :max="50" />
        <span class="inline">只；排名在前</span>
        <el-input-number v-model="model.selection.buffer" :min="0" :max="50" style="width: 90px" />
        <span class="inline">名以内的已持有标的继续持有（降低换手）</span>
      </el-form-item>
      <el-form-item v-if="hasStock" label="同行业上限">
        <el-checkbox
          :model-value="model.selection.max_per_industry !== null"
          @change="(v: string | number | boolean) => toggleNullable(model.selection, 'max_per_industry', !!v, 3)"
        >
          限制
        </el-checkbox>
        <el-input-number
          v-if="model.selection.max_per_industry !== null"
          v-model="model.selection.max_per_industry"
          :min="1"
          :max="50"
          style="width: 90px; margin-left: 8px"
        />
        <span v-if="model.selection.max_per_industry !== null" class="inline">只 / 申万一级行业</span>
      </el-form-item>
      <el-form-item label="权重方式">
        <el-radio-group v-model="model.weighting.method">
          <el-radio-button value="equal">等权</el-radio-button>
          <el-radio-button value="score">按得分</el-radio-button>
          <el-radio-button value="inverse_vol">波动率倒数</el-radio-button>
        </el-radio-group>
        <template v-if="model.weighting.method === 'inverse_vol'">
          <span class="inline">波动率窗口</span>
          <el-input-number v-model="model.weighting.vol_window" :min="10" :max="250" :step="10" style="width: 100px" />
          <span class="inline">日</span>
        </template>
      </el-form-item>
      <el-form-item label="单个标的上限">
        <PctInput v-model="model.weighting.max_weight" :min="2" :max="100" />
      </el-form-item>

      <!-- 调仓 -->
      <h4>调仓</h4>
      <el-form-item label="调仓间隔">
        <el-input-number v-model="model.rebalance.every_days" :min="1" :max="60" />
        <span class="inline">个交易日</span>
      </el-form-item>
      <el-form-item label="最小调整">
        <PctInput v-model="model.rebalance.min_trade_pct" :min="0" :max="10" :step="0.5" />
        <span class="inline">权重变化小于该值时不交易（新建仓与清仓除外）</span>
      </el-form-item>

      <!-- 择时与风控 -->
      <h4>大盘择时</h4>
      <el-form-item label="启用">
        <el-switch v-model="model.timing.enabled" />
        <span class="inline">基准指数满足条件时正常仓位，否则降低仓位</span>
      </el-form-item>
      <template v-if="model.timing.enabled">
        <el-form-item label="条件">
          <el-select v-model="model.timing.name" style="width: 160px" @change="onFactorChange(model.timing)">
            <el-option v-for="x in timingFactors" :key="x.name" :label="x.title" :value="x.name" />
          </el-select>
          <el-select v-model="model.timing.op" style="width: 70px; margin-left: 8px">
            <el-option v-for="o in OPS" :key="o" :label="o" :value="o" />
          </el-select>
          <el-input-number v-model="model.timing.value" :step="0.01" style="width: 110px; margin-left: 8px" />
          <span v-for="[key, p] in paramList(model.timing.name)" :key="key" class="param">
            {{ p.title || key }}
            <el-input-number
              v-model="model.timing.params[key]"
              :min="p.min"
              :max="p.max"
              :step="paramStep(model.timing.name, key)"
              style="width: 100px"
            />
          </span>
        </el-form-item>
        <el-form-item label="不满足时仓位">
          <PctInput v-model="model.timing.exposure_off" :min="0" :max="100" :step="10" />
        </el-form-item>
        <el-form-item label="防守标的">
          <el-select
            v-model="model.timing.defensive"
            filterable
            allow-create
            clearable
            placeholder="无（持有现金）"
            style="width: 220px"
          >
            <el-option label="国债 ETF 511010.SH" value="511010.SH" />
          </el-select>
        </el-form-item>
      </template>

      <h4>风控</h4>
      <el-form-item label="总仓位">
        <PctInput v-model="model.risk.exposure" :min="10" :max="100" />
      </el-form-item>
      <el-form-item label="止损">
        <el-checkbox
          :model-value="model.risk.stop_loss !== null"
          @change="(v: string | number | boolean) => toggleNullable(model.risk, 'stop_loss', !!v, 0.1)"
        >
          相对买入成本亏损
        </el-checkbox>
        <PctInput v-if="model.risk.stop_loss !== null" v-model="model.risk.stop_loss" :min="2" :max="50" class="ml" />
        <span v-if="model.risk.stop_loss !== null" class="inline">时卖出</span>
      </el-form-item>
      <el-form-item label="移动止损">
        <el-checkbox
          :model-value="model.risk.trailing_stop !== null"
          @change="(v: string | number | boolean) => toggleNullable(model.risk, 'trailing_stop', !!v, 0.15)"
        >
          相对持有期最高价回撤
        </el-checkbox>
        <PctInput
          v-if="model.risk.trailing_stop !== null"
          v-model="model.risk.trailing_stop"
          :min="2"
          :max="50"
          class="ml"
        />
        <span v-if="model.risk.trailing_stop !== null" class="inline">时卖出</span>
      </el-form-item>
    </el-form>
  </div>
</template>

<style scoped>
.mode {
  margin-bottom: 8px;
}
h4 {
  margin: 16px 0 8px;
  font-size: 14px;
}
.hint {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  font-weight: normal;
  margin: 4px 0 0;
}
h4 .hint {
  margin-left: 8px;
}
.inline {
  margin: 0 6px;
  color: var(--el-text-color-regular);
}
.wide {
  width: 100%;
  max-width: 460px;
}
.term {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  padding: 8px 10px;
  margin-bottom: 8px;
}
.term-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}
.term-row.params {
  margin-top: 6px;
}
.param {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  color: var(--el-text-color-regular);
}
.ml {
  margin-left: 8px;
}
.mono :deep(textarea) {
  font-family: ui-monospace, Consolas, monospace;
  font-size: 12px;
}
</style>
