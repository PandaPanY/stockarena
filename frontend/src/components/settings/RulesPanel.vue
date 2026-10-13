<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { errorMessage } from '@/api/http'
import { metaApi, type RuleSet, type Universe } from '@/api/arena'

const rulesets = ref<RuleSet[]>([])
const universes = ref<Universe[]>([])

onMounted(async () => {
  try {
    ;[rulesets.value, universes.value] = await Promise.all([metaApi.rulesets(), metaApi.universes()])
  } catch (err) {
    ElMessage.error(errorMessage(err))
  }
})

const SOURCES: Record<string, { label: string; type: 'info' | 'warning' | 'primary' }> = {
  system: { label: '系统默认', type: 'info' },
  global: { label: '全局配置', type: 'warning' },
  ruleset: { label: '规则集', type: 'primary' },
}

function formatValue(v: string | number | boolean): string {
  if (typeof v === 'boolean') return v ? '是' : '否'
  return String(v)
}
</script>

<template>
  <div>
    <p class="hint">
      规则集与股票池由 <code>config/rulesets</code>、<code>config/universes</code>
      下的配置文件定义，修改文件后重启后端生效。已启动的赛道使用启动时冻结的副本，不受影响。
    </p>
    <p class="hint">
      参数三级继承：系统默认 → 全局配置（<code>config.yaml</code> 的 <code>rule_defaults</code>）→
      规则集文件，后者覆盖前者；
      每个参数旁的标签表示生效值的来源。做空与融资的保证金比例、利率均为示例值，请按券商条款调整。
    </p>

    <h4 class="section">规则集</h4>
    <el-collapse>
      <el-collapse-item v-for="r in rulesets" :key="r.id" :name="r.id">
        <template #title>
          <b>{{ r.title }}</b>
          <el-tag v-if="r.params.allow_short" size="small" type="danger" class="chip">做空</el-tag>
          <el-tag v-if="r.params.allow_margin" size="small" type="danger" class="chip">融资</el-tag>
          <span class="desc">{{ r.description }}</span>
        </template>
        <el-descriptions :column="2" border size="small">
          <el-descriptions-item v-for="(v, k) in r.params" :key="k" :label="r.param_descriptions[k] || String(k)">
            {{ formatValue(v) }}
            <el-tag
              v-if="r.param_sources[k]"
              size="small"
              :type="SOURCES[r.param_sources[k]].type"
              effect="plain"
              class="chip"
              >{{ SOURCES[r.param_sources[k]].label }}</el-tag
            >
          </el-descriptions-item>
        </el-descriptions>
      </el-collapse-item>
    </el-collapse>

    <h4 class="section">股票池</h4>
    <el-table :data="universes">
      <el-table-column prop="title" label="名称" width="180" />
      <el-table-column prop="description" label="说明" />
      <el-table-column label="构成" width="240">
        <template #default="{ row }">
          <span v-if="row.index">指数 {{ row.index }} 成分股</span>
          <span v-if="row.include.length">合并：{{ row.include.join('、') }}</span>
          <span v-if="row.member_count">{{ row.member_count }} 个指定标的</span>
        </template>
      </el-table-column>
      <el-table-column prop="benchmark_name" label="基准" width="100" />
    </el-table>
  </div>
</template>

<style scoped>
.hint {
  color: var(--el-text-color-secondary);
  font-size: 13px;
  margin: 0 0 6px;
}

.section {
  margin: 20px 0 8px;
  font-size: 14px;
}

.chip {
  margin-left: 6px;
}

.desc {
  margin-left: 12px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>
