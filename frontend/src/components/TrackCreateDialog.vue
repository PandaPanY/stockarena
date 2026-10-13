<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { errorMessage } from '@/api/http'
import { agentsApi, metaApi, tracksApi, type Agent, type MarketInfo, type RuleSet, type Universe } from '@/api/arena'

const visible = defineModel<boolean>({ required: true })
const emit = defineEmits<{ (e: 'created', id: number): void }>()

const markets = ref<MarketInfo[]>([])
const market = ref<MarketInfo['code']>('CN')
const universes = ref<Universe[]>([])
const rulesets = ref<RuleSet[]>([])
const agents = ref<Agent[]>([])
const saving = ref(false)
const form = reactive({
  name: '',
  universe_name: 'cn_sse50_etf',
  rule_set_id: null as number | null,
  initial_cash: 1000000,
  output_mode: 'targets' as 'targets' | 'orders',
  decision_timeout_s: 900,
  end_date: null as string | null,
  description: '',
  agent_ids: [] as number[],
  mode: 'live' as 'live' | 'replay',
  replay_start: null as string | null,
  frequency: 'daily' as 'daily' | 'intraday',
  interval_min: 15,
  event_enabled: true,
  event_move_pct: 3,
  event_max_per_day: 5,
  decision_mode: 'snapshot' as 'snapshot' | 'tools',
  max_tool_calls: 10,
  data_news: false,
  data_fundamentals: false,
  anonymize: false,
})

// 分钟级只支持实时模式；决策时限不超过间隔的 80%，否则下一轮开始时上一轮还没结束
watch(
  () => [form.frequency, form.interval_min] as const,
  ([f, interval]) => {
    if (f === 'intraday') {
      form.mode = 'live'
      form.decision_timeout_s = Math.min(900, Math.floor(interval * 60 * 0.8))
    } else {
      form.decision_timeout_s = 900
    }
  },
)

/** 回放开始日期只能选今天之前 */
const disabledReplayDate = (d: Date) => d.getTime() >= new Date(new Date().toDateString()).getTime()

const marketInfo = computed(() => markets.value.find((m) => m.code === market.value))
const marketUniverses = computed(() => universes.value.filter((u) => u.market === market.value))
const marketRulesets = computed(() => rulesets.value.filter((r) => r.market === market.value))

/** 切换市场时，股票池、规则集、本金都换成该市场的默认值 */
function applyMarketDefaults() {
  const pools = marketUniverses.value
  // 默认池为「合并」类型（如 上证50 + 核心ETF）
  form.universe_name = (pools.find((u) => u.include.length) ?? pools[0])?.name ?? ''
  form.rule_set_id = marketRulesets.value[0]?.id ?? null
  form.initial_cash = marketInfo.value?.default_capital ?? 1000000
  // 自动生成的名称跟随市场；用户改过的名称保持不变
  if (!form.name || form.name === autoName.value) form.name = defaultName()
  autoName.value = form.name
}

const autoName = ref('')
const defaultName = () => `${marketInfo.value?.name ?? ''}竞技 ${new Date().toLocaleDateString('zh-CN')}`
watch(market, applyMarketDefaults)

const llmAgents = computed(() => agents.value.filter((a) => a.kind === 'llm'))
const baselineAgents = computed(() => agents.value.filter((a) => a.kind === 'baseline'))

watch(visible, async (v) => {
  if (!v) return
  try {
    ;[markets.value, universes.value, rulesets.value, agents.value] = await Promise.all([
      metaApi.markets(),
      metaApi.universes(),
      metaApi.rulesets(),
      agentsApi.list(),
    ])
    if (!form.rule_set_id) applyMarketDefaults()
    // 默认勾选所有 AI 选手 + 买入持有基准 + 随机交易
    form.agent_ids = [
      ...llmAgents.value.map((a) => a.id),
      ...baselineAgents.value.filter((a) => a.baseline_type !== 'equal_weight').map((a) => a.id),
    ]
    if (!form.name) {
      form.name = defaultName()
      autoName.value = form.name
    }
  } catch (err) {
    ElMessage.error(errorMessage(err))
  }
})

async function submit() {
  if (!form.rule_set_id) return ElMessage.warning('请选择规则集')
  if (!form.agent_ids.length) return ElMessage.warning('请至少选择一个选手')
  if (form.mode === 'replay' && !form.replay_start) return ElMessage.warning('请选择回放开始日期')
  saving.value = true
  try {
    const { event_enabled, event_move_pct, data_news, data_fundamentals, ...rest } = form
    const track = await tracksApi.create({
      ...rest,
      rule_set_id: form.rule_set_id,
      initial_cash: String(form.initial_cash),
      event_move_pct: form.frequency === 'intraday' && event_enabled ? String(event_move_pct / 100) : null,
      data_options: { news: data_news, fundamentals: data_fundamentals },
      anonymize: form.mode === 'replay' && form.anonymize,
    })
    ElMessage.success('赛道已创建，确认选手后点击「启动」开始比赛')
    visible.value = false
    emit('created', track.id)
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <el-dialog v-model="visible" title="创建赛道" width="640px">
    <el-alert
      type="info"
      :closable="false"
      show-icon
      class="mb"
      title="赛道启动后，股票池、规则和选手都会冻结，保证所有选手在完全相同的条件下比较。"
    />
    <el-form label-width="110px" @submit.prevent>
      <el-form-item label="名称" required><el-input v-model="form.name" maxlength="128" /></el-form-item>
      <el-form-item label="决策频率">
        <el-radio-group v-model="form.frequency">
          <el-radio value="daily">日频（收盘后）</el-radio>
          <el-radio value="intraday">分钟级（盘中）</el-radio>
        </el-radio-group>
      </el-form-item>
      <template v-if="form.frequency === 'intraday'">
        <el-form-item label="决策间隔">
          <el-radio-group v-model="form.interval_min">
            <el-radio-button v-for="m in [5, 15, 30, 60]" :key="m" :value="m">{{ m }} 分钟</el-radio-button>
          </el-radio-group>
          <div class="hint block">
            交易时段内每分钟拉取实时行情、合成分钟 K 线并撮合订单；开盘 2 分钟后开始，每隔所选时间请所有选手决策一次。
            本地模型每次决策约 1 分钟，间隔太短可能来不及。
          </div>
        </el-form-item>
        <el-form-item label="异动触发">
          <el-switch v-model="form.event_enabled" />
          <template v-if="form.event_enabled">
            <span class="hint">持仓或基准较上一轮变动超过</span>
            <el-input-number
              v-model="form.event_move_pct"
              :min="0.5"
              :max="20"
              :step="0.5"
              :precision="1"
              size="small"
            />
            <span class="hint">% 时额外决策，每天最多</span>
            <el-input-number v-model="form.event_max_per_day" :min="1" :max="20" size="small" />
            <span class="hint">次</span>
          </template>
        </el-form-item>
      </template>
      <el-form-item label="运行模式">
        <el-radio-group v-model="form.mode">
          <el-radio value="live">实时模拟</el-radio>
          <el-radio value="replay" :disabled="form.frequency === 'intraday'">历史回放</el-radio>
        </el-radio-group>
        <div class="hint block">
          <template v-if="form.mode === 'live'">
            从现在开始{{
              form.frequency === 'intraday' ? '，在交易时段内按所选间隔决策' : '，每个交易日收盘后自动决策'
            }}，结果最可信。
          </template>
          <template v-else>
            从过去某天开始，用真实历史行情逐日快进到最近一个收盘日，之后转为实时继续运行。适合快速测试。
            <b>注意：</b>模型的训练数据可能已包含这段行情（前视偏差），回放收益通常偏乐观，不宜用来评判 AI 的真实水平。
            本地模型每个交易日约需数分钟。
          </template>
        </div>
      </el-form-item>
      <el-form-item v-if="form.mode === 'replay'" label="回放开始" required>
        <el-date-picker
          v-model="form.replay_start"
          type="date"
          value-format="YYYY-MM-DD"
          :disabled-date="disabledReplayDate"
          placeholder="选择过去的日期"
        />
      </el-form-item>
      <el-form-item v-if="form.mode === 'replay'" label="代码匿名化">
        <el-switch v-model="form.anonymize" />
        <div class="hint block">
          开启后，回放时 prompt 中的证券代码和名称换成 S01、E01 等代号，日期换成相对决策日的交易日（T、T-3），
          减少模型凭记忆认出当时行情的可能。价格水平等信息仍可能暴露身份，只能降低、不能消除前视偏差。
        </div>
      </el-form-item>
      <el-form-item label="市场">
        <el-radio-group v-model="market">
          <el-radio-button v-for="m in markets" :key="m.code" :value="m.code">{{ m.name }}</el-radio-button>
        </el-radio-group>
        <div v-if="marketInfo" class="hint block">
          <template v-if="form.frequency === 'intraday'">交易时段内运行（{{ marketInfo.timezone }}）</template>
          <template v-else
            >每个交易日收盘后自动决策（当地时间 {{ marketInfo.run_times[0] }} 起，{{ marketInfo.timezone }}）</template
          >，货币 {{ marketInfo.currency }}
        </div>
      </el-form-item>
      <el-form-item label="股票池">
        <el-select v-model="form.universe_name" style="width: 100%">
          <el-option
            v-for="u in marketUniverses"
            :key="u.name"
            :label="`${u.title}（基准：${u.benchmark_name}）`"
            :value="u.name"
          />
        </el-select>
      </el-form-item>
      <el-form-item label="规则集">
        <el-select v-model="form.rule_set_id" style="width: 100%">
          <el-option v-for="r in marketRulesets" :key="r.id" :label="r.title" :value="r.id" />
        </el-select>
      </el-form-item>
      <el-form-item label="初始本金">
        <el-input-number v-model="form.initial_cash" :min="10000" :step="10000" />
        <span class="hint">{{ marketInfo?.currency ?? '' }}</span>
      </el-form-item>
      <el-form-item label="决策方式">
        <el-radio-group v-model="form.output_mode">
          <el-radio value="targets">目标仓位（推荐）</el-radio>
          <el-radio value="orders">直接下单</el-radio>
        </el-radio-group>
        <div class="hint block">
          目标仓位：AI 给出每个标的占净值的比例，系统自动算数量并按整手取整，对本地模型更友好。直接下单：AI
          自己给出股数，考验规则遵守能力。
        </div>
      </el-form-item>
      <el-form-item label="数据获取">
        <el-radio-group v-model="form.decision_mode">
          <el-radio value="snapshot">快照（全部数据放进 prompt）</el-radio>
          <el-radio value="tools">工具调用（AI 按需查询）</el-radio>
        </el-radio-group>
        <template v-if="form.decision_mode === 'tools'">
          <span class="hint">每轮最多查询</span>
          <el-input-number v-model="form.max_tool_calls" :min="1" :max="30" size="small" />
          <span class="hint">次</span>
        </template>
        <div class="hint block">
          工具调用：prompt 只列出股票池，AI 自己调用工具查询行情、K 线、技术指标、财报、新闻和账户，最后提交决策。
          需要模型支持函数调用；每次查询都是一次模型请求，耗时和 token 会明显增加。所有工具都只返回决策时点之前的数据。
        </div>
      </el-form-item>
      <el-form-item v-if="form.decision_mode === 'snapshot'" label="附带数据">
        <el-checkbox v-model="form.data_fundamentals">财报指标</el-checkbox>
        <el-checkbox v-model="form.data_news">新闻（仅实时运行）</el-checkbox>
        <div class="hint block">
          财报：每只股票最近一期已披露的营收、净利润、ROE 等，按法定披露截止日判断能否使用，回放时不会看到未来的财报。
          新闻：持仓股与当日涨跌幅较大标的的近 3 日新闻；新闻接口只有最近几条，历史回放时不附带。两项都会增加 prompt
          长度。
        </div>
      </el-form-item>
      <el-form-item label="决策时限">
        <el-input-number v-model="form.decision_timeout_s" :min="60" :max="7200" :step="60" />
        <span class="hint">秒，超时视为本轮不操作</span>
      </el-form-item>
      <el-form-item label="结束日期">
        <el-date-picker v-model="form.end_date" type="date" value-format="YYYY-MM-DD" placeholder="不设置则一直运行" />
      </el-form-item>
      <el-form-item label="AI 选手">
        <el-checkbox-group v-model="form.agent_ids">
          <el-checkbox v-for="a in llmAgents" :key="a.id" :value="a.id">{{ a.name }}</el-checkbox>
        </el-checkbox-group>
        <span v-if="!llmAgents.length" class="hint">还没有 AI 选手，请先到配置中心添加</span>
      </el-form-item>
      <el-form-item label="基线选手">
        <el-checkbox-group v-model="form.agent_ids">
          <el-checkbox v-for="a in baselineAgents" :key="a.id" :value="a.id">{{ a.name }}</el-checkbox>
        </el-checkbox-group>
      </el-form-item>
      <el-form-item label="说明"><el-input v-model="form.description" type="textarea" :rows="2" /></el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button type="primary" :loading="saving" @click="submit">创建</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.mb {
  margin-bottom: 16px;
}

.hint {
  margin-left: 8px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.hint.block {
  display: block;
  margin: 4px 0 0;
  line-height: 1.5;
}
</style>
