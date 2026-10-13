<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { agentsApi, decisionsApi, providersApi, tracksApi, type DecisionSummary, type Track } from '@/api/arena'
import { useSystemStore } from '@/stores/system'
import { formatPercent } from '@/utils/format'

const router = useRouter()
const system = useSystemStore()

const tracks = ref<Track[]>([])
const decisions = ref<DecisionSummary[]>([])
const providerCount = ref(0)
const llmAgentCount = ref(0)
const loaded = ref(false)

const activeTracks = computed(() => tracks.value.filter((t) => t.status === 'running' || t.status === 'paused'))
const step = computed(() => {
  if (!providerCount.value) return 0
  if (!llmAgentCount.value) return 1
  if (!tracks.value.length) return 2
  if (!activeTracks.value.length) return 3
  return 4
})

const steps = [
  { title: '接入模型', desc: '在配置中心添加云端或本地 AI 模型' },
  { title: '创建选手', desc: '为模型选择参数与投资风格' },
  { title: '创建赛道', desc: '选择股票池、规则与本金' },
  { title: '开始竞技', desc: '启动赛道，AI 按真实行情自动交易' },
]

const STATUS_LABEL: Record<string, string> = {
  ok: '有效',
  baseline: '基线',
  invalid: '无法解析',
  timeout: '超时',
  error: '出错',
}

onMounted(async () => {
  try {
    const [t, d, p, a] = await Promise.all([
      tracksApi.list(),
      decisionsApi.list({ limit: 8 }),
      providersApi.list(),
      agentsApi.list(),
    ])
    tracks.value = t
    decisions.value = d.items.filter((x) => x.agent_kind === 'llm')
    providerCount.value = p.length
    llmAgentCount.value = a.filter((x) => x.kind === 'llm').length
  } catch {
    /* 后端未连接时由顶部提示说明 */
  } finally {
    loaded.value = true
  }
})

function goStep(i: number) {
  const targets = [
    { name: 'settings', query: { tab: 'providers' } },
    { name: 'settings', query: { tab: 'agents' } },
    { name: 'tracks' },
    { name: 'tracks' },
  ]
  router.push(targets[i])
}
</script>

<template>
  <div>
    <h2 class="sa-page-title">总览</h2>

    <el-alert
      v-if="system.error"
      type="error"
      :title="system.error"
      description="请在 backend 目录运行：uv run uvicorn app.main:app --reload --port 18080"
      show-icon
      :closable="false"
      class="mb"
    />

    <el-card v-if="loaded && step < 4" shadow="never" header="快速开始" class="mb">
      <el-steps :active="step" align-center finish-status="success">
        <el-step
          v-for="(s, i) in steps"
          :key="s.title"
          :title="s.title"
          :description="s.desc"
          class="step"
          @click="goStep(i)"
        />
      </el-steps>
    </el-card>

    <el-row :gutter="16">
      <el-col :xs="24" :lg="15">
        <el-card shadow="never" header="进行中的赛道" class="mb">
          <el-empty v-if="!activeTracks.length" description="没有进行中的赛道" :image-size="60">
            <el-button type="primary" @click="router.push({ name: 'tracks' })">去创建赛道</el-button>
          </el-empty>
          <div
            v-for="t in activeTracks"
            :key="t.id"
            class="track"
            @click="router.push({ name: 'track', params: { id: t.id } })"
          >
            <div class="track-head">
              <b>{{ t.name }}</b>
              <el-tag size="small" :type="t.status === 'running' ? 'success' : 'warning'">{{ t.status_label }}</el-tag>
              <span class="muted small">最近结算 {{ t.last_settled_date ?? '尚未结算' }}</span>
            </div>
            <ol class="ranking">
              <li v-for="r in t.ranking?.slice(0, 5)" :key="r.name">
                <span class="name">{{ r.name }}</span>
                <span class="num" :class="(r.cumulative_return ?? 0) >= 0 ? 'sa-up' : 'sa-down'">{{
                  formatPercent(r.cumulative_return)
                }}</span>
              </li>
            </ol>
          </div>
        </el-card>
      </el-col>
      <el-col :xs="24" :lg="9">
        <el-card shadow="never" class="mb">
          <template #header>
            <div class="card-header">
              <span>最近的 AI 决策</span>
              <el-button link type="primary" @click="router.push({ name: 'decisions' })">全部</el-button>
            </div>
          </template>
          <el-empty v-if="!decisions.length" description="暂无" :image-size="50" />
          <div
            v-for="d in decisions"
            :key="d.id"
            class="decision"
            @click="router.push({ name: 'decisions', query: { decision: d.id } })"
          >
            <div class="d-head">
              <b>{{ d.agent_name }}</b
              ><span class="muted small">{{ d.data_date }} · {{ STATUS_LABEL[d.status] }}</span>
            </div>
            <div class="d-text">{{ d.analysis || d.error }}</div>
          </div>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<style scoped>
.mb {
  margin-bottom: 16px;
}

.step {
  cursor: pointer;
}

.track {
  padding: 12px;
  border-radius: 8px;
  cursor: pointer;
}

.track + .track {
  border-top: 1px solid var(--el-border-color-lighter);
}

.track:hover {
  background: var(--el-fill-color-light);
}

.track-head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.ranking {
  margin: 8px 0 0;
  padding-left: 20px;
  columns: 2;
  column-gap: 32px;
}

.ranking li {
  display: flex;
  justify-content: space-between;
  line-height: 24px;
}

.ranking .name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  margin-right: 8px;
}

.num {
  font-variant-numeric: tabular-nums;
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.decision {
  padding: 8px 0;
  cursor: pointer;
}

.decision + .decision {
  border-top: 1px solid var(--el-border-color-lighter);
}

.d-head {
  display: flex;
  justify-content: space-between;
}

.d-text {
  margin-top: 4px;
  font-size: 13px;
  color: var(--el-text-color-regular);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.muted {
  color: var(--el-text-color-secondary);
}

.small {
  font-size: 12px;
}
</style>
