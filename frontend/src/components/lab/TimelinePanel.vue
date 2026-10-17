<script setup lang="ts">
/** AI 优化时间线：从初始版本起，每一轮 AI 优化的诊断、每个候选改了什么、检验结果与是否采纳（只含开发期）。 */
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { errorMessage } from '@/api/http'
import { quantApi, type Timeline, type TimelineCandidate } from '@/api/quant'
import { formatDateTime, formatPercent } from '@/utils/format'

const props = defineProps<{ strategyId: number }>()
const data = ref<Timeline | null>(null)
const loading = ref(true)
/** 默认只展开有候选被采纳的轮次 */
const onlyPromoted = ref(false)

onMounted(async () => {
  try {
    data.value = await quantApi.timeline(props.strategyId)
  } catch (err) {
    ElMessage.error(errorMessage(err))
  } finally {
    loading.value = false
  }
})

const events = computed(() =>
  (data.value?.events ?? [])
    .filter((e) => !onlyPromoted.value || e.promoted)
    .slice()
    .reverse(),
)

const num = (v: number | null | undefined, d = 2) => (v === null || v === undefined ? '-' : v.toFixed(d))
const outcomeType = (c: TimelineCandidate) =>
  c.status === 'champion' || c.status === 'retired' ? 'success' : c.status === 'duplicate' ? 'info' : 'warning'
const sourceText: Record<string, string> = { ai: 'AI', manual: '手动', initial: '初始' }
</script>

<template>
  <div v-loading="loading" class="timeline">
    <template v-if="data">
      <div class="summary">
        <span>共 {{ data.summary.versions }} 个版本</span>
        <span
          >AI 优化 {{ data.summary.rounds }} 轮，提出 {{ data.summary.ai_candidates }} 个候选，采纳
          {{ data.summary.ai_promoted }} 个</span
        >
        <span v-if="data.summary.manual">手动修改 {{ data.summary.manual }} 次</span>
        <span v-if="data.summary.champion">当前冠军 v{{ data.summary.champion }}</span>
        <el-checkbox v-model="onlyPromoted" class="filter">只看有版本被采纳的</el-checkbox>
      </div>
      <p class="muted">
        只列开发期的结果：AI 研究和升级检验都只看开发期，留出期从不参与决定。每个候选和当时的冠军逐条对比规则，
        检验的显著性门槛按累计测试次数收紧（p 值需不大于 0.05 ÷ 测试次数）。
      </p>

      <el-timeline>
        <el-timeline-item
          v-for="(e, i) in events"
          :key="i"
          :timestamp="formatDateTime(e.at)"
          placement="top"
          :type="e.promoted ? 'success' : 'info'"
        >
          <el-card shadow="never" class="event">
            <div class="event-head">
              <strong v-if="e.type === 'round'">
                AI 优化第 {{ e.round }} 轮（研究 #{{ e.run_id }}，{{ e.trigger === 'schedule' ? '每晚自动' : '手动' }}）
              </strong>
              <strong v-else>手动修改</strong>
              <el-tag v-if="e.promoted" size="small" type="success">升级到 v{{ e.promoted }}</el-tag>
              <el-tag v-else-if="e.type === 'round'" size="small" type="info">未升级</el-tag>
            </div>
            <p v-if="e.error" class="error">这一轮失败：{{ e.error }}</p>
            <details v-if="e.analysis" class="analysis">
              <summary>AI 的诊断</summary>
              <p>{{ e.analysis }}</p>
              <ul v-if="e.lessons?.length">
                <li v-for="(l, j) in e.lessons" :key="j">记下的经验：{{ l }}</li>
              </ul>
            </details>

            <div v-for="(c, j) in e.candidates" :key="j" class="candidate">
              <div class="cand-head">
                <span v-if="c.number" class="ver">v{{ c.number }}</span>
                <span class="title">{{ c.title }}</span>
                <el-tag size="small" :type="outcomeType(c)">{{ c.outcome }}</el-tag>
                <span v-if="c.source && c.source !== 'ai'" class="muted">{{ sourceText[c.source] ?? c.source }}</span>
              </div>
              <p v-if="c.rationale" class="rationale">为什么改：{{ c.rationale }}</p>
              <div v-if="c.diff && (c.diff.removed.length || c.diff.added.length)" class="diff">
                <div class="muted">与 v{{ c.parent_number }} 相比：</div>
                <div v-for="(r, k) in c.diff.removed" :key="`r${k}`" class="removed">− {{ r }}</div>
                <div v-for="(a, k) in c.diff.added" :key="`a${k}`" class="added">＋ {{ a }}</div>
              </div>
              <div v-if="c.metrics" class="metrics">
                <span>开发期夏普 {{ num(c.metrics.sharpe) }}（当时冠军 {{ num(c.parent_sharpe) }}）</span>
                <span>年化 {{ formatPercent(c.metrics.annualized_return ?? null) }}</span>
                <span>最大回撤 {{ formatPercent(c.metrics.max_drawdown ?? null, 1, false) }}</span>
                <span v-if="c.metrics.p !== null && c.metrics.p !== undefined">
                  月度配对检验 p = {{ num(c.metrics.p, 3) }}（累计测试 {{ c.metrics.n_tests ?? 1 }} 次）
                </span>
                <span v-if="c.metrics.year_wins">逐年跑赢冠军 {{ c.metrics.year_wins }}</span>
              </div>
              <p v-if="c.decision" class="decision">结论：{{ c.decision }}</p>
            </div>
          </el-card>
        </el-timeline-item>

        <el-timeline-item :timestamp="formatDateTime(data.start.created_at)" placement="top" type="primary">
          <el-card shadow="never" class="event">
            <div class="event-head">
              <strong>起点：v{{ data.start.number }} {{ data.start.title }}</strong>
            </div>
            <p v-if="data.start.forked_from" class="muted">
              另存自「{{ data.start.forked_from.strategy_name }}」v{{
                data.start.forked_from.version_number
              }}，继承已检验的候选 {{ data.start.forked_from.prior_tests }} 个
            </p>
            <p v-if="data.start.trials_before_create" class="muted">
              新建前试算过 {{ data.start.trials_before_create }} 次，计入测试次数
            </p>
            <ul class="rules">
              <li v-for="(r, k) in data.start.rules" :key="k">{{ r }}</li>
            </ul>
            <div class="metrics">
              <span>开发期夏普 {{ num(data.start.metrics.sharpe) }}</span>
              <span>年化 {{ formatPercent(data.start.metrics.annualized_return ?? null) }}</span>
              <span>最大回撤 {{ formatPercent(data.start.metrics.max_drawdown ?? null, 1, false) }}</span>
            </div>
          </el-card>
        </el-timeline-item>
      </el-timeline>
      <el-empty v-if="!events.length && onlyPromoted" description="还没有版本被采纳" />
    </template>
  </div>
</template>

<style scoped>
.summary {
  display: flex;
  flex-wrap: wrap;
  gap: 16px;
  align-items: center;
  font-size: 14px;
}

.filter {
  margin-left: auto;
}

.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.event-head {
  display: flex;
  gap: 8px;
  align-items: center;
}

.analysis {
  margin: 8px 0;
  font-size: 13px;
}

.analysis summary {
  cursor: pointer;
  color: var(--el-color-primary);
}

.analysis p {
  white-space: pre-wrap;
  line-height: 1.6;
}

.candidate {
  border-top: 1px dashed var(--el-border-color);
  margin-top: 10px;
  padding-top: 10px;
  font-size: 13px;
}

.cand-head {
  display: flex;
  gap: 8px;
  align-items: center;
}

.ver {
  font-weight: 600;
}

.rationale,
.decision {
  margin: 6px 0;
  line-height: 1.6;
}

.diff {
  margin: 6px 0;
  font-family: var(--el-font-family);
  line-height: 1.7;
}

.removed {
  color: var(--el-color-danger);
  text-decoration: line-through;
}

.added {
  color: var(--el-color-success);
}

.metrics {
  display: flex;
  flex-wrap: wrap;
  gap: 14px;
  color: var(--el-text-color-regular);
  margin: 6px 0;
}

.rules {
  margin: 6px 0;
  padding-left: 18px;
  font-size: 13px;
  line-height: 1.7;
}

.error {
  color: var(--el-color-danger);
}
</style>
